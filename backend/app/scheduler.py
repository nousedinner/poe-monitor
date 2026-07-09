import time
import logging
import threading
import queue
from collections import deque
from datetime import datetime, timedelta, date
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from . import db
from .models import MonitorTarget, UserCredential, ItemSnapshot, Event, DailyStats, PriceTrend
from .poe_api import PoeAPI
from .utils import decrypt_poesessid
import json
import hashlib
import requests
from sqlalchemy import text

logger = logging.getLogger(__name__)

class MonitorScheduler:
    def __init__(self, app=None):
        self.app = app
        self.scheduler = BackgroundScheduler()
        self.job_handles = {}
        self.request_queue = queue.Queue()
        self.rate_limit_interval = 6
        self.consumer_thread = threading.Thread(target=self._consumer_worker, daemon=True)
        self.consumer_thread.start()
        self.warning_queue = queue.Queue(maxsize=100)
    
    def _extract_mods_by_type(self, item_info):
        """提取不同类型的词缀"""
        mods = {}
        # 7种词缀类型
        mod_types = ['explicitMods', 'implicitMods', 'fracturedMods', 'craftedMods', 'enchantMods', 'veiledMods', 'mutatedMods']
        
        for mod_type in mod_types:
            if mod_type in item_info and item_info[mod_type]:
                mods[mod_type] = item_info[mod_type]
            else:
                mods[mod_type] = []
        
        # 新增：把 properties、notableProperties、builtInSupport 合并到 explicitMods
        # 修复品质、等级、内置辅助、强辅宝石词缀丢失问题 (2026-05-01)
        extra = []
        if 'notableProperties' in item_info:
            for np in item_info['notableProperties']:
                if 'name' in np:
                    extra.append(np['name'])
        if 'properties' in item_info:
            for prop in item_info['properties']:
                name = prop.get('name', '')
                values = prop.get('values', [])
                if values and values[0] and values[0][0]:
                    value_str = values[0][0]
                    extra.append(f"{name}: {value_str}")
        if 'builtInSupport' in item_info and item_info['builtInSupport']:
            extra.append(f"内置辅助: {item_info['builtInSupport']}")
        
        if extra:
            mods['explicitMods'] = mods.get('explicitMods', []) + extra
        
        return mods
    
    def _collect_all_mods(self, item_info):
        mods = []
        for key in ['explicitMods', 'implicitMods', 'fracturedMods', 'craftedMods', 'enchantMods', 'veiledMods', 'mutatedMods']:
            if key in item_info and item_info[key]:
                mods.extend(item_info[key])
        if 'notableProperties' in item_info:
            for np in item_info['notableProperties']:
                if 'name' in np:
                    mods.append(np['name'])
        if 'properties' in item_info:
            for prop in item_info['properties']:
                name = prop.get('name', '')
                values = prop.get('values', [])
                if values and values[0] and values[0][0]:
                    value_str = values[0][0]
                    mods.append(f"{name}: {value_str}")
        if 'builtInSupport' in item_info and item_info['builtInSupport']:
            mods.append(f"内置辅助: {item_info['builtInSupport']}")
        return mods
    
    def _compute_fingerprint_hash(self, item_info):
        name = item_info.get('name', '')
        base_type = item_info.get('typeLine', '')
        mods = self._collect_all_mods(item_info)
        mods_sorted = sorted(mods)
        fingerprint_str = f"{name}|{base_type}|{json.dumps(mods_sorted)}"
        return hashlib.sha256(fingerprint_str.encode()).hexdigest()

    def init_app(self, app, skip_load=False):
        self.app = app
        if not skip_load:
            with app.app_context():
                self._load_targets()
        self.scheduler.start()
        self.scheduler.add_job(
            self._sync_targets,
            trigger=IntervalTrigger(seconds=30),
            id='sync_targets',
            replace_existing=True
        )
        self.scheduler.add_job(
            self._daily_stats_job,
            trigger=CronTrigger(hour=0, minute=1),
            id='daily_stats',
            replace_existing=True
        )
        self.scheduler.add_job(
            self._clean_old_data,
            trigger=CronTrigger(hour=2, minute=0, timezone='Asia/Shanghai'),
            id='clean_old_data',
            replace_existing=True
        )

    def _consumer_worker(self):
        while True:
            print(f"[心跳] {datetime.utcnow()} 消费者线程活跃")   # 新增
            try:
                target_id = self.request_queue.get(timeout=1)
                self._execute_scan(target_id)
            except queue.Empty:
                continue
            except Exception as e:
                logger.exception("Consumer worker error")
            finally:
                time.sleep(self.rate_limit_interval)

    def _execute_scan(self, target_id):
        with self.app.app_context():
            target = MonitorTarget.query.get(target_id)
            if not target:
                return
            self._scan_target(target)

    def _scan_target(self, target):
        print(f"开始扫描目标 {target.id}...")
        scan_start = datetime.utcnow()
        print(f"[扫描开始] 目标 {target.id} 时间 {scan_start}")
        if not target.enabled:
            print(f"目标 {target.id} 已停用，跳过扫描")
            return

        # 全服监控走独立逻辑
        if target.target_type == 'global':
            self._scan_global_target(target)
            return

        user_cred = UserCredential.query.get(target.user_id)
        if not user_cred or not user_cred.encrypted_poesessid:
            msg = f"用户 {target.user_id} 未设置 POESESSID"
            self._add_warning(msg)
            print(msg)
            return
        try:
            poesessid = decrypt_poesessid(user_cred.encrypted_poesessid)
            pt2gguin = None
            if user_cred.encrypted_pt2gguin:
                pt2gguin = decrypt_poesessid(user_cred.encrypted_pt2gguin)
            print(f"解密后的 POESESSID 前10位: {poesessid[:10]}...")
        except Exception as e:
            msg = f"解密 POESESSID 失败 (用户 {target.user_id}): {e}"
            self._add_warning(msg)
            print(msg)
            return

        api = PoeAPI(poesessid, pt2gguin)

        try:
            search_result = api.search_account_items(
                league=target.league,
                account_name=target.poe_account_name,
                min_price=target.min_price
            )
            item_ids = search_result['item_ids']
            query_id = search_result['query_id']
            total = search_result['total']
            print(f"找到 {len(item_ids)} 件物品 (总数: {total})")

            if not item_ids:
                msg = f"目标 {target.id} (账号: {target.poe_account_name}) 未找到任何物品，请检查账号名是否正确或当前无上架"
                self._add_warning(msg)
                print(msg)
                target.last_scan = datetime.utcnow()
                db.session.commit()
                scan_end = datetime.utcnow()        # 添加
                print(f"[扫描结束] 目标 {target.id} 耗时 {(scan_end - scan_start).total_seconds():.1f} 秒")   # 添加
                print("没有物品，扫描结束")
                return

            all_items = api.fetch_items_batch(item_ids, query_id, batch_size=10)
            print(f"批量获取成功，共 {len(all_items)} 件物品")

            # ... 从原代码中复制 1-5 部分，保持不变 ...
            # 注意：这里需要将原文件中的 1-5 部分完整复制过来，因为内容未变。

            # 下面是用 ... 省略的部分，实际替换时请将原代码中从 "1. 加载旧快照..." 到 "5. 更新快照" 之间的所有内容原样粘贴过来。
            # 由于原代码已正确，只需保持原样

            # ======================= 1. 加载旧快照并分类 =======================
            old_snapshots = ItemSnapshot.query.filter_by(target_id=target.id).all()
            old_gem_dict = {}      # key: item_id
            old_nongem_dict = {}   # key: fingerprint_hash

            for s in old_snapshots:
                if s.item_data:
                    frame_type = s.item_data.get('item', {}).get('frameType')
                    is_gem = (frame_type == 4)
                    if is_gem:
                        old_gem_dict[s.item_id] = s
                    else:
                        item_info = s.item_data.get('item', {})
                        fp_hash = self._compute_fingerprint_hash(item_info)
                        old_nongem_dict[fp_hash] = s

            # ======================= 2. 构建新快照并分类 =======================
            new_gem_dict = {}        # key: item_id
            new_nongem_dict = {}     # key: fingerprint_hash
            new_items_by_id = {}

            for item_data in all_items:
                item_id = item_data.get('id')
                if not item_id:
                    continue
                frame_type = item_data.get('item', {}).get('frameType')
                is_gem = (frame_type == 4)
                if is_gem:
                    new_gem_dict[item_id] = item_data
                else:
                    item_info = item_data.get('item', {})
                    fp_hash = self._compute_fingerprint_hash(item_info)
                    new_nongem_dict[fp_hash] = item_data
                new_items_by_id[item_id] = item_data

            # ======================= 3. 处理技能石 =======================
            old_ids = set(old_gem_dict.keys())
            new_ids = set(new_gem_dict.keys())

            # 3.1 消失 -> sold
            for item_id in old_ids - new_ids:
                snapshot = old_gem_dict[item_id]
                # 计算指纹（用于后续回溯）
                item_info = snapshot.item_data.get('item', {})
                fp_hash = self._compute_fingerprint_hash(item_info)
                frame_type = snapshot.item_data.get('item', {}).get('frameType') if snapshot.item_data else None
                is_gem = (frame_type == 4)
                event = Event(
                    target_id=target.id,
                    item_id=snapshot.item_id,
                    item_name=snapshot.item_name,
                    base_type=snapshot.base_type,
                    explicit_mods=snapshot.explicit_mods,
                    implicit_mods=snapshot.implicit_mods,
                    fractured_mods=snapshot.fractured_mods,
                    crafted_mods=snapshot.crafted_mods,
                    enchant_mods=snapshot.enchant_mods,
                    mutated_mods=snapshot.mutated_mods,
                    veiled_mods=snapshot.veiled_mods,
                    price=snapshot.price,
                    price_currency=snapshot.price_currency,
                    event_type='sold',
                    is_gem=is_gem,
                    fingerprint_hash=fp_hash   # 关键：填充指纹
                )
                db.session.add(event)

            # 3.2 新增 -> 回溯修正（窗口10分钟）或 listed
            gem_cutoff = datetime.utcnow() - timedelta(minutes=10)
            for item_id in new_ids - old_ids:
                item_data = new_gem_dict[item_id]
                item_info = item_data.get('item', {})
                listing = item_data.get('listing', {})
                price_info = listing.get('price', {})
                new_price = price_info.get('amount', 0)
                currency = price_info.get('currency', 'divine')
                frame_type = item_data.get('item', {}).get('frameType')
                is_gem = (frame_type == 4)

                fp_hash = self._compute_fingerprint_hash(item_info)

                recent_sold = Event.query.filter(
                    Event.target_id == target.id,
                    Event.fingerprint_hash == fp_hash,
                    Event.event_type == 'sold',
                    Event.created_at >= gem_cutoff
                ).order_by(Event.created_at.desc()).first()

                if recent_sold:
                    time_diff = (datetime.utcnow() - recent_sold.created_at).total_seconds() / 60
                    print(f"[技能石] 回溯匹配到 sold id={recent_sold.id} 价格={recent_sold.price}, 新价格={new_price}, 时间差={time_diff:.1f}分钟")
                    if recent_sold.price == new_price:
                        db.session.delete(recent_sold)
                    else:
                        recent_sold.event_type = 'price_change'
                        recent_sold.price_old = recent_sold.price
                        recent_sold.price = new_price
                        recent_sold.price_currency = currency
                        recent_sold.is_gem = is_gem
                        recent_sold.fingerprint_hash = fp_hash
                else:
                    mods_by_type = self._extract_mods_by_type(item_info)
                    event = Event(
                        target_id=target.id,
                        item_id=item_data.get('id'),
                        item_name=item_info.get('name'),
                        base_type=item_info.get('typeLine'),
                        explicit_mods=json.dumps(mods_by_type.get('explicitMods', [])),
                        implicit_mods=json.dumps(mods_by_type.get('implicitMods', [])),
                        fractured_mods=json.dumps(mods_by_type.get('fracturedMods', [])),
                        crafted_mods=json.dumps(mods_by_type.get('craftedMods', [])),
                        enchant_mods=json.dumps(mods_by_type.get('enchantMods', [])),
                        mutated_mods=json.dumps(mods_by_type.get('mutatedMods', [])),
                        veiled_mods=json.dumps(mods_by_type.get('veiledMods', [])),
                        price=new_price,
                        price_currency=currency,
                        event_type='listed',
                        is_gem=is_gem,
                        fingerprint_hash=fp_hash
                    )
                    db.session.add(event)

            # 3.3 共有 -> 价格变化生成 price_change
            for item_id in old_ids & new_ids:
                old_snap = old_gem_dict[item_id]
                new_item_data = new_gem_dict[item_id]
                new_price = new_item_data.get('listing', {}).get('price', {}).get('amount', 0)
                currency = new_item_data.get('listing', {}).get('price', {}).get('currency', 'divine')
                if old_snap.price != new_price:
                    item_info = new_item_data.get('item', {})
                    mods_by_type = self._extract_mods_by_type(item_info)
                    frame_type = new_item_data.get('item', {}).get('frameType')
                    is_gem = (frame_type == 4)
                    fp_hash = self._compute_fingerprint_hash(item_info)
                    event = Event(
                        target_id=target.id,
                        item_id=new_item_data.get('id'),
                        item_name=item_info.get('name'),
                        base_type=item_info.get('typeLine'),
                        explicit_mods=json.dumps(mods_by_type.get('explicitMods', [])),
                        implicit_mods=json.dumps(mods_by_type.get('implicitMods', [])),
                        fractured_mods=json.dumps(mods_by_type.get('fracturedMods', [])),
                        crafted_mods=json.dumps(mods_by_type.get('craftedMods', [])),
                        enchant_mods=json.dumps(mods_by_type.get('enchantMods', [])),
                        mutated_mods=json.dumps(mods_by_type.get('mutatedMods', [])),
                        veiled_mods=json.dumps(mods_by_type.get('veiledMods', [])),
                        price=new_price,
                        price_old=old_snap.price,
                        price_currency=currency,
                        event_type='price_change',
                        is_gem=is_gem,
                        fingerprint_hash=fp_hash
                    )
                    db.session.add(event)

            # ======================= 4. 处理非技能石 =======================
            old_fps = set(old_nongem_dict.keys())
            new_fps = set(new_nongem_dict.keys())

            # 4.1 消失 -> sold
            for fp in old_fps - new_fps:
                snapshot = old_nongem_dict[fp]
                frame_type = snapshot.item_data.get('item', {}).get('frameType') if snapshot.item_data else None
                is_gem = (frame_type == 4)
                event = Event(
                    target_id=target.id,
                    item_id=snapshot.item_id,
                    item_name=snapshot.item_name,
                    base_type=snapshot.base_type,
                    explicit_mods=snapshot.explicit_mods,
                    implicit_mods=snapshot.implicit_mods,
                    fractured_mods=snapshot.fractured_mods,
                    crafted_mods=snapshot.crafted_mods,
                    enchant_mods=snapshot.enchant_mods,
                    mutated_mods=snapshot.mutated_mods,
                    veiled_mods=snapshot.veiled_mods,
                    price=snapshot.price,
                    price_currency=snapshot.price_currency,
                    event_type='sold',
                    is_gem=is_gem,
                    fingerprint_hash=fp
                )
                db.session.add(event)

            # 4.2 新增 -> 回溯修正（窗口24小时）或 listed
            nongem_cutoff = datetime.utcnow() - timedelta(hours=24)
            for fp in new_fps - old_fps:
                item_data = new_nongem_dict[fp]
                item_info = item_data.get('item', {})
                listing = item_data.get('listing', {})
                price_info = listing.get('price', {})
                new_price = price_info.get('amount', 0)
                currency = price_info.get('currency', 'divine')
                frame_type = item_data.get('item', {}).get('frameType')
                is_gem = (frame_type == 4)

                recent_sold = Event.query.filter(
                    Event.target_id == target.id,
                    Event.fingerprint_hash == fp,
                    Event.event_type == 'sold',
                    Event.created_at >= nongem_cutoff
                ).order_by(Event.created_at.desc()).first()

                if recent_sold:
                    if recent_sold.price == new_price:
                        db.session.delete(recent_sold)
                    else:
                        recent_sold.event_type = 'price_change'
                        recent_sold.price_old = recent_sold.price
                        recent_sold.price = new_price
                        recent_sold.price_currency = currency
                        recent_sold.is_gem = is_gem
                        recent_sold.fingerprint_hash = fp
                else:
                    mods_by_type = self._extract_mods_by_type(item_info)
                    event = Event(
                        target_id=target.id,
                        item_id=item_data.get('id'),
                        item_name=item_info.get('name'),
                        base_type=item_info.get('typeLine'),
                        explicit_mods=json.dumps(mods_by_type.get('explicitMods', [])),
                        implicit_mods=json.dumps(mods_by_type.get('implicitMods', [])),
                        fractured_mods=json.dumps(mods_by_type.get('fracturedMods', [])),
                        crafted_mods=json.dumps(mods_by_type.get('craftedMods', [])),
                        enchant_mods=json.dumps(mods_by_type.get('enchantMods', [])),
                        mutated_mods=json.dumps(mods_by_type.get('mutatedMods', [])),
                        veiled_mods=json.dumps(mods_by_type.get('veiledMods', [])),
                        price=new_price,
                        price_currency=currency,
                        event_type='listed',
                        is_gem=is_gem,
                        fingerprint_hash=fp
                    )
                    db.session.add(event)

            # 4.3 共有 -> 价格变化生成 price_change
            for fp in old_fps & new_fps:
                old_snap = old_nongem_dict[fp]
                new_item_data = new_nongem_dict[fp]
                new_price = new_item_data.get('listing', {}).get('price', {}).get('amount', 0)
                currency = new_item_data.get('listing', {}).get('price', {}).get('currency', 'divine')
                if old_snap.price != new_price:
                    mods_by_type = self._extract_mods_by_type(item_info)
                    frame_type = new_item_data.get('item', {}).get('frameType')
                    is_gem = (frame_type == 4)
                    fp = self._compute_fingerprint_hash(item_info)
                    event = Event(
                        target_id=target.id,
                        item_id=new_item_data.get('id'),
                        item_name=item_info.get('name'),
                        base_type=item_info.get('typeLine'),
                        explicit_mods=json.dumps(mods_by_type.get('explicitMods', [])),
                        implicit_mods=json.dumps(mods_by_type.get('implicitMods', [])),
                        fractured_mods=json.dumps(mods_by_type.get('fracturedMods', [])),
                        crafted_mods=json.dumps(mods_by_type.get('craftedMods', [])),
                        enchant_mods=json.dumps(mods_by_type.get('enchantMods', [])),
                        mutated_mods=json.dumps(mods_by_type.get('mutatedMods', [])),
                        veiled_mods=json.dumps(mods_by_type.get('veiledMods', [])),
                        price=new_price,
                        price_old=old_snap.price,
                        price_currency=currency,
                        event_type='price_change',
                        is_gem=is_gem,
                        fingerprint_hash=fp
                    )
                    db.session.add(event)

            # ======================= 5. 更新快照 =======================
            for s in old_snapshots:
                db.session.delete(s)
            for item_data in all_items:
                item_info = item_data.get('item', {})
                listing = item_data.get('listing', {})
                price_info = listing.get('price', {})
                mods_by_type = self._extract_mods_by_type(item_info)
                snapshot = ItemSnapshot(
                    target_id=target.id,
                    item_id=item_data.get('id'),
                    item_name=item_info.get('name'),
                    base_type=item_info.get('typeLine'),
                    rarity=item_info.get('rarity'),
                    ilvl=item_info.get('ilvl'),
                    explicit_mods=json.dumps(mods_by_type.get('explicitMods', [])),
                    implicit_mods=json.dumps(mods_by_type.get('implicitMods', [])),
                    fractured_mods=json.dumps(mods_by_type.get('fracturedMods', [])),
                    crafted_mods=json.dumps(mods_by_type.get('craftedMods', [])),
                    enchant_mods=json.dumps(mods_by_type.get('enchantMods', [])),
                    mutated_mods=json.dumps(mods_by_type.get('mutatedMods', [])),
                    veiled_mods=json.dumps(mods_by_type.get('veiledMods', [])),
                    price=price_info.get('amount', 0),
                    price_currency=price_info.get('currency', 'divine'),
                    item_data=item_data,
                    scan_timestamp=datetime.utcnow()
                )
                db.session.add(snapshot)

            target.last_scan = datetime.utcnow()
            db.session.commit()
            print(f"目标 {target.id} 扫描完成，事件处理完成")

        except requests.exceptions.Timeout:
            db.session.rollback()
            msg = f"扫描目标 {target.id} 网络超时（90秒）"
            self._add_warning(msg)
            print(msg)
            logger.warning(msg)
        except requests.exceptions.ConnectionError as e:
            db.session.rollback()
            msg = f"扫描目标 {target.id} 网络连接错误: {e}"
            self._add_warning(msg)
            print(msg)
            logger.warning(msg)
        except Exception as e:
            db.session.rollback()
            msg = f"扫描目标 {target.id} 失败: {e}"
            self._add_warning(msg)
            print(msg)
            logger.exception(e)

    def _scan_global_target(self, target):
        """全服监控扫描逻辑"""
        print(f"[全服扫描] 开始目标 {target.id}")
        if not target.query_params:
            msg = f"全服目标 {target.id} 未设置筛选条件"
            self._add_warning(msg)
            return

        # 获取凭证
        user_cred = UserCredential.query.get(target.user_id)
        if not user_cred or not user_cred.encrypted_poesessid:
            msg = f"用户 {target.user_id} 未设置 POESESSID"
            self._add_warning(msg)
            return
        try:
            poesessid = decrypt_poesessid(user_cred.encrypted_poesessid)
            pt2gguin = None
            if user_cred.encrypted_pt2gguin:
                pt2gguin = decrypt_poesessid(user_cred.encrypted_pt2gguin)
        except Exception as e:
            msg = f"解密 POESESSID 失败 (用户 {target.user_id}): {e}"
            self._add_warning(msg)
            return

        api = PoeAPI(poesessid, pt2gguin)

        try:
            # 1. 搜索
            search_result = api.search_global_items(
                league=target.league,
                query=target.query_params
            )
            item_ids = search_result['item_ids']
            query_id = search_result['query_id']
            total = search_result['total']
            print(f"[全服扫描] 找到 {len(item_ids)} 件物品 (总数: {total})")

            # 更新存储的 query_id（确保后续扫描使用最新短代码）
            if query_id and target.query_params:
                if target.query_params.get('_query_id') != query_id:
                    target.query_params['_query_id'] = query_id

            if not item_ids:
                # 清空快照
                ItemSnapshot.query.filter_by(target_id=target.id).delete()
                target.last_scan = datetime.utcnow()
                db.session.commit()
                print(f"[全服扫描] 无物品，已清空快照")
                return

            # 2. 批量获取详情
            all_items = api.fetch_items_batch(item_ids, query_id, batch_size=10)

            # 3. 解析物品：提取 listing_id, price, currency, seller, name
            new_items = {}  # listing_id -> {price, currency, seller, item_name, base_type, item_data}
            divine_items = []  # 用于价格趋势统计

            for item_data in all_items:
                listing_id = item_data.get('id')
                if not listing_id:
                    continue
                listing = item_data.get('listing', {})
                price_info = listing.get('price', {})
                price = price_info.get('amount', 0)
                currency = price_info.get('currency', 'divine')
                seller = listing.get('account', {}).get('name', '')
                item_info = item_data.get('item', {})
                item_name = item_info.get('name', '')
                base_type = item_info.get('typeLine', '')

                # chaos 完全跳过
                if currency == 'chaos':
                    continue

                new_items[listing_id] = {
                    'price': price,
                    'currency': currency,
                    'seller': seller,
                    'item_name': item_name,
                    'base_type': base_type,
                    'item_data': item_data,
                }
                if currency == 'divine':
                    divine_items.append({'price': price})

            # 4. 加载旧快照
            old_snapshots = ItemSnapshot.query.filter_by(target_id=target.id).all()
            old_map = {s.item_id: s for s in old_snapshots}

            # 5. 对比生成事件
            old_ids = set(old_map.keys())
            new_ids = set(new_items.keys())

            # 5.1 旧有、新无 -> sold
            for lid in old_ids - new_ids:
                old_snap = old_map[lid]
                event = Event(
                    target_id=target.id,
                    item_id=old_snap.item_id,
                    item_name=old_snap.item_name,
                    base_type=old_snap.base_type,
                    price=old_snap.price,
                    price_currency=old_snap.price_currency,
                    event_type='sold',
                    seller_account=old_snap.seller_account,
                )
                db.session.add(event)

            # 5.2 新有、旧无 -> listed（仅 divine/mirror）
            for lid in new_ids - old_ids:
                item = new_items[lid]
                if item['currency'] == 'chaos':
                    continue
                item_data = item.get('item_data', {})
                item_info = item_data.get('item', {})
                mods_by_type = self._extract_mods_by_type(item_info)
                event = Event(
                    target_id=target.id,
                    item_id=lid,
                    item_name=item['item_name'],
                    base_type=item['base_type'],
                    explicit_mods=json.dumps(mods_by_type.get('explicitMods', [])),
                    implicit_mods=json.dumps(mods_by_type.get('implicitMods', [])),
                    fractured_mods=json.dumps(mods_by_type.get('fracturedMods', [])),
                    crafted_mods=json.dumps(mods_by_type.get('craftedMods', [])),
                    enchant_mods=json.dumps(mods_by_type.get('enchantMods', [])),
                    mutated_mods=json.dumps(mods_by_type.get('mutatedMods', [])),
                    veiled_mods=json.dumps(mods_by_type.get('veiledMods', [])),
                    price=item['price'],
                    price_currency=item['currency'],
                    event_type='listed',
                    seller_account=item['seller'],
                )
                db.session.add(event)

            # 5.3 共有、价格变化 -> price_change（仅 divine/mirror）
            for lid in old_ids & new_ids:
                old_snap = old_map[lid]
                new_item = new_items[lid]
                if old_snap.price != new_item['price'] or old_snap.price_currency != new_item['currency']:
                    if new_item['currency'] == 'chaos':
                        continue
                    item_data = new_item.get('item_data', {})
                    item_info = item_data.get('item', {})
                    mods_by_type = self._extract_mods_by_type(item_info)
                    event = Event(
                        target_id=target.id,
                        item_id=lid,
                        item_name=new_item['item_name'],
                        base_type=new_item['base_type'],
                        explicit_mods=json.dumps(mods_by_type.get('explicitMods', [])),
                        implicit_mods=json.dumps(mods_by_type.get('implicitMods', [])),
                        fractured_mods=json.dumps(mods_by_type.get('fracturedMods', [])),
                        crafted_mods=json.dumps(mods_by_type.get('craftedMods', [])),
                        enchant_mods=json.dumps(mods_by_type.get('enchantMods', [])),
                        mutated_mods=json.dumps(mods_by_type.get('mutatedMods', [])),
                        veiled_mods=json.dumps(mods_by_type.get('veiledMods', [])),
                        price=new_item['price'],
                        price_old=old_snap.price,
                        price_currency=new_item['currency'],
                        event_type='price_change',
                        seller_account=new_item['seller'],
                    )
                    db.session.add(event)

            # 6. 价格趋势统计（仅 divine）
            if divine_items:
                divine_items.sort(key=lambda x: x['price'])
                min_price = divine_items[0]['price']
                top10 = divine_items[:10]
                top10_avg = sum(i['price'] for i in top10) // len(top10)
                trend = PriceTrend(
                    target_id=target.id,
                    min_price=min_price,
                    top10_avg_price=top10_avg,
                    item_count=len(divine_items),
                )
                db.session.add(trend)

            # 7. 更新快照：删除旧的，插入新的（只存 divine/mirror）
            for s in old_snapshots:
                db.session.delete(s)
            for lid, item in new_items.items():
                if item['currency'] == 'chaos':
                    continue
                item_data = item.get('item_data', {})
                item_info = item_data.get('item', {})
                mods_by_type = self._extract_mods_by_type(item_info)
                snapshot = ItemSnapshot(
                    target_id=target.id,
                    item_id=lid,
                    item_name=item['item_name'],
                    base_type=item['base_type'],
                    explicit_mods=json.dumps(mods_by_type.get('explicitMods', [])),
                    implicit_mods=json.dumps(mods_by_type.get('implicitMods', [])),
                    fractured_mods=json.dumps(mods_by_type.get('fracturedMods', [])),
                    crafted_mods=json.dumps(mods_by_type.get('craftedMods', [])),
                    enchant_mods=json.dumps(mods_by_type.get('enchantMods', [])),
                    mutated_mods=json.dumps(mods_by_type.get('mutatedMods', [])),
                    veiled_mods=json.dumps(mods_by_type.get('veiledMods', [])),
                    price=item['price'],
                    price_currency=item['currency'],
                    item_data=item['item_data'],
                    seller_account=item['seller'],
                    scan_timestamp=datetime.utcnow(),
                )
                db.session.add(snapshot)

            target.last_scan = datetime.utcnow()
            db.session.commit()
            print(f"[全服扫描] 目标 {target.id} 完成，divine={len(divine_items)}")

        except requests.exceptions.Timeout:
            db.session.rollback()
            msg = f"[全服扫描] 目标 {target.id} 网络超时"
            self._add_warning(msg)
            logger.warning(msg)
        except requests.exceptions.ConnectionError as e:
            db.session.rollback()
            msg = f"[全服扫描] 目标 {target.id} 网络错误: {e}"
            self._add_warning(msg)
            logger.warning(msg)
        except Exception as e:
            db.session.rollback()
            msg = f"[全服扫描] 目标 {target.id} 失败: {e}"
            self._add_warning(msg)
            logger.exception(e)

    def _clean_old_data(self):
        """每天凌晨2点（北京时间）清理90天前的数据"""
        with self.app.app_context():
            cutoff = datetime.utcnow() - timedelta(days=90)
            try:
                deleted_events = db.session.execute(
                    text("DELETE FROM events WHERE created_at < :cutoff"),
                    {'cutoff': cutoff}
                ).rowcount
                deleted_trends = db.session.execute(
                    text("DELETE FROM price_trend WHERE scan_time < :cutoff"),
                    {'cutoff': cutoff}
                ).rowcount
                db.session.commit()
                print(f"[数据清理] 删除 {deleted_events} 条事件, {deleted_trends} 条趋势记录")
            except Exception as e:
                db.session.rollback()
                logger.exception(f"数据清理失败: {e}")

    def _add_warning(self, message):
        timestamp = datetime.utcnow().isoformat()
        entry = f"{timestamp} - {message}"
        try:
            self.warning_queue.put_nowait(entry)
        except queue.Full:
            pass
        logger.warning(message)

    def get_warnings(self, limit=50):
        warnings = []
        temp = []
        while not self.warning_queue.empty():
            try:
                temp.append(self.warning_queue.get_nowait())
            except queue.Empty:
                break
        warnings = temp[-limit:]
        for w in temp:
            try:
                self.warning_queue.put_nowait(w)
            except queue.Full:
                break
        return warnings

    def trigger_immediate_scan(self, target_id):
        self.request_queue.put(target_id)

    def _daily_stats_job(self):
        with self.app.app_context():
            yesterday = date.today() - timedelta(days=1)
            targets = MonitorTarget.query.all()
            for target in targets:
                listed_total = db.session.query(db.func.sum(Event.price)).filter(
                    Event.target_id == target.id,
                    Event.event_type == 'listed',
                    db.func.date(Event.created_at) == yesterday
                ).scalar() or 0
                sold_total = db.session.query(db.func.sum(Event.price)).filter(
                    Event.target_id == target.id,
                    Event.event_type == 'sold',
                    db.func.date(Event.created_at) == yesterday
                ).scalar() or 0
                stats = DailyStats.query.filter_by(target_id=target.id, date=yesterday).first()
                if stats:
                    stats.total_listed_price = listed_total
                    stats.total_sold_price = sold_total
                else:
                    stats = DailyStats(
                        target_id=target.id,
                        date=yesterday,
                        total_listed_price=listed_total,
                        total_sold_price=sold_total
                    )
                    db.session.add(stats)
                db.session.commit()
            print(f"每日统计完成，日期: {yesterday}")

    def _load_targets(self):
        with self.app.app_context():
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            if not inspector.has_table('monitor_targets'):
                print("表 monitor_targets 还未创建，跳过加载目标")
                return
            targets = MonitorTarget.query.all()
            for target in targets:
                self._add_job_for_target(target)

    def _sync_targets(self):
        with self.app.app_context():
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            if not inspector.has_table('monitor_targets'):
                return
            current_target_ids = set(self.job_handles.keys())
            db_targets = MonitorTarget.query.all()
            db_target_ids = {t.id for t in db_targets}
            for target in db_targets:
                if target.id not in current_target_ids:
                    self._add_job_for_target(target)
            for target_id in current_target_ids - db_target_ids:
                self._remove_job(target_id)
            for target in db_targets:
                if target.id in current_target_ids:
                    job = self.job_handles.get(target.id)
                    if job and job.trigger.interval.total_seconds() != target.scan_interval:
                        self._remove_job(target.id)
                        self._add_job_for_target(target)

    def _add_job_for_target(self, target):
        job = self.scheduler.add_job(
            self._enqueue_scan,
            trigger=IntervalTrigger(seconds=target.scan_interval),
            id=f"scan_{target.id}",
            args=[target.id],
            replace_existing=True
        )
        self.job_handles[target.id] = job

    def _enqueue_scan(self, target_id):
        self.request_queue.put(target_id)

    def _remove_job(self, target_id):
        job_id = f"scan_{target_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
        if target_id in self.job_handles:
            del self.job_handles[target_id]