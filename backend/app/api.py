from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from datetime import datetime, timedelta
from sqlalchemy import func
from . import db
from .models import MonitorTarget, UserCredential, Event, User, ItemSnapshot, PriceTrend
from .utils import encrypt_poesessid, decrypt_poesessid
import json
import requests
import os
import hashlib
import time

api_bp = Blueprint('api', __name__)

# 腾讯静态数据缓存（赛季内数据不变，避免每个用户每次打开页面都打腾讯API）
TRADE_CACHE_DIR = '/tmp/poe_trade_cache'
TRADE_CACHE_TTL = 6 * 3600  # 6小时

def _read_trade_cache(name, allow_stale=False):
    """读取缓存，返回 (data_str, etag)；TTL 内或 allow_stale 时有效"""
    path = os.path.join(TRADE_CACHE_DIR, name)
    try:
        mtime = os.path.getmtime(path)
        if not allow_stale and time.time() - mtime > TRADE_CACHE_TTL:
            return None, None
        with open(path, 'r', encoding='utf-8') as f:
            data = f.read()
        return data, hashlib.md5(data.encode('utf-8')).hexdigest()
    except (OSError, ValueError):
        return None, None

def _write_trade_cache(name, data):
    try:
        os.makedirs(TRADE_CACHE_DIR, exist_ok=True)
        with open(os.path.join(TRADE_CACHE_DIR, name), 'w', encoding='utf-8') as f:
            f.write(data)
    except OSError:
        pass

def _cached_trade_response(name, upstream_url, label):
    """通用缓存代理：优先缓存，未命中拉腾讯并写缓存；腾讯失败时兜底用旧缓存"""
    cached, etag = _read_trade_cache(name)
    if cached is not None:
        resp = current_app.response_class(cached, mimetype='application/json')
        resp.set_etag(etag)
        resp.cache_control.no_cache = True
        return resp.make_conditional(request)
    try:
        upstream = requests.get(upstream_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        data = upstream.text
        _write_trade_cache(name, data)
        resp = current_app.response_class(data, mimetype='application/json')
        resp.set_etag(hashlib.md5(data.encode('utf-8')).hexdigest())
        resp.cache_control.no_cache = True
        return resp
    except Exception as e:
        stale, stale_etag = _read_trade_cache(name, allow_stale=True)
        if stale is not None:
            resp = current_app.response_class(stale, mimetype='application/json')
            resp.set_etag(stale_etag)
            resp.cache_control.no_cache = True
            return resp
        return jsonify({'msg': f'get {label} failed: {str(e)}'}), 500

def convert_filters(filters):
    """转换前端传来的filters格式为POE API格式
    前端price格式: {"price": {"option": "divine", "value": {"max": 300}}}
    POE API格式:   {"price": {"option": "divine", "max": 300}}
    """
    if not filters:
        return filters
    
    result = {}
    for group_name, group_data in filters.items():
        if not isinstance(group_data, dict) or 'filters' not in group_data:
            result[group_name] = group_data
            continue
        
        new_filters = {}
        for filter_name, filter_value in group_data['filters'].items():
            # 处理 price 等带 value 嵌套的字段
            if isinstance(filter_value, dict) and 'value' in filter_value and isinstance(filter_value['value'], dict):
                # 合并 option 和 value 中的字段
                new_filter = {}
                if 'option' in filter_value:
                    new_filter['option'] = filter_value['option']
                # 把 value 中的 min/max 提取到顶层
                for k, v in filter_value['value'].items():
                    new_filter[k] = v
                new_filters[filter_name] = new_filter
            else:
                new_filters[filter_name] = filter_value
        
        result[group_name] = {'filters': new_filters}
    
    return result


def convert_stats(stats):
    """腾讯POE市集API使用小写类型名(and/not/if/count)，无需转换。
    weight/weight2 不是官方API类型，暂不支持。
    """
    return stats


@api_bp.route('/user', methods=['GET'])
@jwt_required()
def get_user_info():
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)
    if not user:
        return jsonify({'msg': 'User not found'}), 404
    return jsonify({
        'id': user.id,
        'username': user.username,
        'is_admin': user.is_admin
    }), 200

@api_bp.route('/targets', methods=['GET', 'POST'])
@jwt_required()
def manage_targets():
    user_id = int(get_jwt_identity())
    if request.method == 'GET':
        targets = MonitorTarget.query.filter_by(user_id=user_id).order_by(MonitorTarget.sort_order.asc(), MonitorTarget.id.asc()).all()
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = (datetime.utcnow() - timedelta(days=1)).date()
        result = []
        for t in targets:
            # 今日新增/售出次数（只统计非冗余事件）
            today_new = Event.query.filter(
                Event.target_id == t.id,
                Event.event_type == 'listed',
                Event.created_at >= today_start,
                Event.is_redundant == False
            ).count()
            today_sold_count = Event.query.filter(
                Event.target_id == t.id,
                Event.event_type == 'sold',
                Event.created_at >= today_start,
                Event.is_redundant == False
            ).count()
            
            # 昨日售出（分货币，只统计非冗余）
            yesterday_divine = db.session.query(func.sum(Event.price)).filter(
                Event.target_id == t.id,
                Event.event_type == 'sold',
                db.func.date(Event.created_at) == yesterday,
                Event.price_currency == 'divine',
                Event.is_redundant == False
            ).scalar() or 0
            yesterday_chaos = db.session.query(func.sum(Event.price)).filter(
                Event.target_id == t.id,
                Event.event_type == 'sold',
                db.func.date(Event.created_at) == yesterday,
                Event.price_currency == 'chaos',
                Event.is_redundant == False
            ).scalar() or 0
            
            # 今日售出（分货币，只统计非冗余）
            today_divine = db.session.query(func.sum(Event.price)).filter(
                Event.target_id == t.id,
                Event.event_type == 'sold',
                Event.created_at >= today_start,
                Event.price_currency == 'divine',
                Event.is_redundant == False
            ).scalar() or 0
            today_chaos = db.session.query(func.sum(Event.price)).filter(
                Event.target_id == t.id,
                Event.event_type == 'sold',
                Event.created_at >= today_start,
                Event.price_currency == 'chaos',
                Event.is_redundant == False
            ).scalar() or 0
            
            # 当前在售总价（分货币，快照表无冗余字段，所以无需过滤）
            current_divine = db.session.query(func.sum(ItemSnapshot.price)).filter(
                ItemSnapshot.target_id == t.id,
                ItemSnapshot.price_currency == 'divine'
            ).scalar() or 0
            current_chaos = db.session.query(func.sum(ItemSnapshot.price)).filter(
                ItemSnapshot.target_id == t.id,
                ItemSnapshot.price_currency == 'chaos'
            ).scalar() or 0
            current_mirror = db.session.query(func.sum(ItemSnapshot.price)).filter(
                ItemSnapshot.target_id == t.id,
                ItemSnapshot.price_currency == 'mirror'
            ).scalar() or 0

            # 在售物品数
            item_count = ItemSnapshot.query.filter_by(target_id=t.id).count()

            # 提取 query_id
            qp = t.query_params if isinstance(t.query_params, dict) else (json.loads(t.query_params) if t.query_params else {})
            query_id = qp.get('_query_id', '') if qp else ''

            result.append({
                'id': t.id,
                'target_type': t.target_type or 'account',
                'poe_account_name': t.poe_account_name,
                'query_params': qp,
                'query_id': query_id,
                'league': t.league,
                'scan_interval': t.scan_interval,
                'min_price': t.min_price,
                'enabled': t.enabled if hasattr(t, 'enabled') else True,
                'last_scan': t.last_scan.isoformat() if t.last_scan else None,
                'today_new': today_new,
                'today_sold_count': today_sold_count,
                'yesterday_divine': yesterday_divine,
                'yesterday_chaos': yesterday_chaos,
                'today_divine': today_divine,
                'today_chaos': today_chaos,
                'current_divine': current_divine,
                'current_chaos': current_chaos,
                'current_mirror': current_mirror,
                'item_count': item_count,
            })
        return jsonify(result), 200
    elif request.method == 'POST':
        data = request.get_json()
        target_type = data.get('target_type', 'account')
        poe_account_name = data.get('poe_account_name')
        scan_interval = data.get('scan_interval', 120)
        min_price = data.get('min_price')
        league = data.get('league', 'S30赛季')
        query_params = data.get('query_params')
        user = User.query.get(user_id)
        min_interval = 60 if user.is_admin else 300
        if scan_interval < min_interval:
            return jsonify({'msg': f'扫描间隔不能小于 {min_interval} 秒'}), 400
        if target_type == 'account':
            if not poe_account_name:
                return jsonify({'msg': '账号监控必须填写账号名'}), 400
            existing = MonitorTarget.query.filter_by(user_id=user_id, poe_account_name=poe_account_name).first()
            if existing:
                return jsonify({'msg': 'Target already exists'}), 400
        elif target_type == 'global':
            if not query_params:
                return jsonify({'msg': '全服监控必须提供筛选条件 query_params'}), 400
            poe_account_name = None  # 全服监控不需要账号名
        else:
            return jsonify({'msg': 'target_type 必须为 account 或 global'}), 400
        target = MonitorTarget(
            user_id=user_id,
            poe_account_name=poe_account_name,
            league=league,
            scan_interval=scan_interval,
            min_price=min_price,
            target_type=target_type,
            query_params=query_params,
        )
        db.session.add(target)
        db.session.commit()
        # 触发立即扫描
        scheduler = current_app.scheduler
        scheduler.trigger_immediate_scan(target.id)
        return jsonify({'id': target.id}), 201

@api_bp.route('/targets/<int:target_id>', methods=['PUT', 'DELETE'])
@jwt_required()
def target_detail(target_id):
    user_id = int(get_jwt_identity())
    target = MonitorTarget.query.filter_by(id=target_id, user_id=user_id).first()
    if not target:
        return jsonify({'msg': 'Target not found'}), 404
    if request.method == 'PUT':
        data = request.get_json()
        user = User.query.get(user_id)
        min_interval = 60 if user.is_admin else 300
        new_interval = data.get('scan_interval', target.scan_interval)
        if new_interval < min_interval:
            return jsonify({'msg': f'扫描间隔不能小于 {min_interval} 秒'}), 400
        target.scan_interval = new_interval
        target.min_price = data.get('min_price', target.min_price)
        # 更新启用状态（如果提供了）
        if 'enabled' in data:
            target.enabled = data['enabled']
        if 'query_params' in data:
            target.query_params = data['query_params']
        db.session.commit()
        return jsonify({'msg': 'Updated'}), 200
    elif request.method == 'DELETE':
        from sqlalchemy import text
        try:
            db.session.execute(text('DELETE FROM events WHERE target_id = :tid'), {'tid': target_id})
            db.session.execute(text('DELETE FROM item_snapshots WHERE target_id = :tid'), {'tid': target_id})
            db.session.execute(text('DELETE FROM daily_stats WHERE target_id = :tid'), {'tid': target_id})
            db.session.execute(text('DELETE FROM price_trend WHERE target_id = :tid'), {'tid': target_id})
            db.session.execute(text('DELETE FROM monitor_targets WHERE id = :tid'), {'tid': target_id})
            db.session.commit()
            return jsonify({'msg': 'Deleted'}), 200
        except Exception as e:
            db.session.rollback()
            return jsonify({'msg': str(e)}), 500

@api_bp.route('/targets/sort', methods=['PUT'])
@jwt_required()
def update_targets_order():
    user_id = int(get_jwt_identity())
    data = request.get_json()
    order_ids = data.get('order', [])
    if not order_ids:
        return jsonify({'msg': 'No order provided'}), 400
    
    # 验证所有目标都属于当前用户
    targets = MonitorTarget.query.filter(MonitorTarget.id.in_(order_ids), MonitorTarget.user_id == user_id).all()
    if len(targets) != len(order_ids):
        return jsonify({'msg': 'Some targets not found or not owned by user'}), 403
    
    # 批量更新 sort_order
    for idx, target_id in enumerate(order_ids):
        db.session.execute(
            MonitorTarget.__table__.update().where(MonitorTarget.id == target_id).values(sort_order=idx)
        )
    db.session.commit()
    return jsonify({'msg': 'Order updated'}), 200


@api_bp.route('/targets/<int:target_id>/trigger', methods=['POST'])
@jwt_required()
def trigger_scan(target_id):
    """立即刷新扫描（60秒冷却）"""
    from flask import current_app
    user_id = int(get_jwt_identity())
    target = MonitorTarget.query.filter_by(id=target_id, user_id=user_id).first()
    if not target:
        return jsonify({'msg': 'Target not found'}), 404
    # 冷却检查：用 last_scan 判断
    if target.last_scan:
        elapsed = (datetime.utcnow() - target.last_scan).total_seconds()
        if elapsed < 60:
            return jsonify({'msg': f'请等待 {int(60 - elapsed)} 秒后再刷新', 'cooldown': int(60 - elapsed)}), 429
    scheduler = current_app.scheduler
    if scheduler:
        scheduler.trigger_immediate_scan(target_id)
    return jsonify({'msg': '已触发刷新', 'cooldown': 60}), 200


@api_bp.route('/targets/<int:target_id>/snapshots', methods=['GET'])
@jwt_required()
def get_snapshots(target_id):
    """获取当前在售物品快照（含词缀、卖家、价格）"""
    user_id = int(get_jwt_identity())
    target = MonitorTarget.query.filter_by(id=target_id, user_id=user_id).first()
    if not target:
        return jsonify({'msg': 'Target not found'}), 404
    limit = request.args.get('limit', 20, type=int)
    snapshots = ItemSnapshot.query.filter_by(target_id=target_id).order_by(ItemSnapshot.price.asc()).limit(limit).all()
    result = []
    for s in snapshots:
        # 合并所有词缀
        all_mods = []
        for mods_field in [s.explicit_mods, s.implicit_mods, s.fractured_mods, s.crafted_mods, s.enchant_mods, s.mutated_mods, s.veiled_mods]:
            if mods_field:
                try:
                    mods_list = json.loads(mods_field)
                    if isinstance(mods_list, list):
                        all_mods.extend(mods_list)
                except Exception:
                    pass
        
        # 如果新字段没有数据，从 item_data 提取
        if not all_mods and s.item_data:
            try:
                item_info = s.item_data.get('item', {})
                for key in ['explicitMods', 'implicitMods', 'fracturedMods', 'craftedMods', 'enchantMods', 'mutatedMods', 'veiledMods']:
                    mods = item_info.get(key, [])
                    if mods:
                        all_mods.extend(mods)
            except Exception:
                pass
        
        # 从 item_data 提取 ilvl（如果字段为空）
        ilvl = s.ilvl
        if not ilvl and s.item_data:
            try:
                ilvl = s.item_data.get('item', {}).get('ilvl')
            except Exception:
                pass
        
        result.append({
            'item_id': s.item_id,
            'item_name': s.item_name or '',
            'base_type': s.base_type or '',
            'rarity': s.rarity or '',
            'ilvl': ilvl,
            'explicit_mods': json.dumps(all_mods),
            'price': s.price,
            'price_currency': s.price_currency,
            'seller_account': s.seller_account or '',
            'item_data': s.item_data,
            'scan_timestamp': s.scan_timestamp.isoformat() if s.scan_timestamp else None,
        })
    return jsonify(result), 200


@api_bp.route('/targets/<int:target_id>/sale_stats', methods=['GET'])
@jwt_required()
def get_sale_stats(target_id):
    """获取多时段上架/售出统计"""
    user_id = int(get_jwt_identity())
    target = MonitorTarget.query.filter_by(id=target_id, user_id=user_id).first()
    if not target:
        return jsonify({'msg': 'Target not found'}), 404
    days = request.args.get('days', 1, type=int)
    if days not in (1, 3, 5, 7):
        return jsonify({'msg': 'days 必须为 1/3/5/7'}), 400
    start = datetime.utcnow() - timedelta(days=days)
    listed = Event.query.filter(
        Event.target_id == target_id,
        Event.event_type == 'listed',
        Event.created_at >= start,
        Event.is_redundant == False
    ).count()
    sold = Event.query.filter(
        Event.target_id == target_id,
        Event.event_type == 'sold',
        Event.created_at >= start,
        Event.is_redundant == False
    ).count()
    sold_divine = db.session.query(func.sum(Event.price)).filter(
        Event.target_id == target_id,
        Event.event_type == 'sold',
        Event.created_at >= start,
        Event.price_currency == 'divine',
        Event.is_redundant == False
    ).scalar() or 0
    sold_chaos = db.session.query(func.sum(Event.price)).filter(
        Event.target_id == target_id,
        Event.event_type == 'sold',
        Event.created_at >= start,
        Event.price_currency == 'chaos',
        Event.is_redundant == False
    ).scalar() or 0
    return jsonify({
        'days': days,
        'listed': listed,
        'sold': sold,
        'sold_divine': sold_divine,
        'sold_chaos': sold_chaos,
    }), 200


@api_bp.route('/credentials', methods=['GET', 'POST'])
@jwt_required()
def manage_credentials():
    user_id = int(get_jwt_identity())
    if request.method == 'GET':
        cred = UserCredential.query.get(user_id)
        if cred:
            return jsonify({
                'last_updated': cred.last_updated.isoformat() if cred.last_updated else None
            }), 200
        return jsonify({'last_updated': None}), 200
    elif request.method == 'POST':
        data = request.get_json()
        poesessid = data.get('poesessid')
        pt2gguin = data.get('pt2gguin')
        cookie = data.get('cookie')
        if cookie:
            import re
            match_poe = re.search(r'POESESSID=([^;]+)', cookie)
            match_pt = re.search(r'pt2gguin=([^;]+)', cookie)
            if match_poe:
                poesessid = match_poe.group(1)
            if match_pt:
                pt2gguin = match_pt.group(1)
        if not poesessid:
            return jsonify({'msg': 'Missing poesessid'}), 400
        encrypted_poesessid = encrypt_poesessid(poesessid)
        encrypted_pt2gguin = encrypt_poesessid(pt2gguin) if pt2gguin else None
        cred = UserCredential.query.get(user_id)
        if cred:
            cred.encrypted_poesessid = encrypted_poesessid
            cred.encrypted_pt2gguin = encrypted_pt2gguin
            cred.last_updated = datetime.utcnow()
        else:
            cred = UserCredential(
                user_id=user_id,
                encrypted_poesessid=encrypted_poesessid,
                encrypted_pt2gguin=encrypted_pt2gguin
            )
            db.session.add(cred)
        db.session.commit()
        return jsonify({'msg': 'Credentials saved'}), 200

@api_bp.route('/targets/<int:target_id>/events', methods=['GET'])
@jwt_required()
def get_events(target_id):
    user_id = int(get_jwt_identity())
    target = MonitorTarget.query.filter_by(id=target_id, user_id=user_id).first()
    if not target:
        return jsonify({'msg': 'Target not found'}), 404
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    event_type = request.args.get('event_type')
    is_redundant = request.args.get('is_redundant')  # 可选，前端可传 0 或 1
    query = Event.query.filter_by(target_id=target.id)
    if event_type:
        query = query.filter_by(event_type=event_type)
    if is_redundant is not None:
        query = query.filter_by(is_redundant=(is_redundant == '1'))
    events = query.order_by(Event.created_at.desc()).limit(limit).offset(offset).all()
    def _parse_mods(field):
        if field:
            try:
                v = json.loads(field)
                return v if isinstance(v, list) else []
            except Exception:
                return []
        return []

    return jsonify([{
        'id': e.id,
        'item_id': e.item_id,
        'item_name': e.item_name,
        'base_type': e.base_type,
        'explicit_mods': _parse_mods(e.explicit_mods),
        'implicit_mods': _parse_mods(e.implicit_mods),
        'fractured_mods': _parse_mods(e.fractured_mods),
        'crafted_mods': _parse_mods(e.crafted_mods),
        'enchant_mods': _parse_mods(e.enchant_mods),
        'mutated_mods': _parse_mods(e.mutated_mods),
        'veiled_mods': _parse_mods(e.veiled_mods),
        'price': e.price,
        'price_old': e.price_old,
        'price_currency': e.price_currency,
        'event_type': e.event_type,
        'is_redundant': e.is_redundant,
        'is_gem': e.is_gem,
        'seller_account': e.seller_account,
        'created_at': e.created_at.isoformat()
    } for e in events]), 200

@api_bp.route('/stats', methods=['GET'])
@jwt_required()
def get_stats():
    user_id = int(get_jwt_identity())
    yesterday = datetime.utcnow().date() - timedelta(days=1)
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    
    # 昨日售出总价值（分货币，只统计非冗余）
    yesterday_divine = db.session.query(func.sum(Event.price)).filter(
        Event.target.has(user_id=user_id),
        Event.event_type == 'sold',
        db.func.date(Event.created_at) == yesterday,
        Event.price_currency == 'divine',
        Event.is_redundant == False
    ).scalar() or 0
    yesterday_chaos = db.session.query(func.sum(Event.price)).filter(
        Event.target.has(user_id=user_id),
        Event.event_type == 'sold',
        db.func.date(Event.created_at) == yesterday,
        Event.price_currency == 'chaos',
        Event.is_redundant == False
    ).scalar() or 0
    
    # 今日售出总价值（分货币，只统计非冗余）
    today_divine = db.session.query(func.sum(Event.price)).filter(
        Event.target.has(user_id=user_id),
        Event.event_type == 'sold',
        Event.created_at >= today_start,
        Event.price_currency == 'divine',
        Event.is_redundant == False
    ).scalar() or 0
    today_chaos = db.session.query(func.sum(Event.price)).filter(
        Event.target.has(user_id=user_id),
        Event.event_type == 'sold',
        Event.created_at >= today_start,
        Event.price_currency == 'chaos',
        Event.is_redundant == False
    ).scalar() or 0
    
    # 当前在售总价值（分货币，快照表无冗余字段，无需过滤）
    current_divine = db.session.query(func.sum(ItemSnapshot.price)).filter(
        ItemSnapshot.target.has(user_id=user_id),
        ItemSnapshot.price_currency == 'divine'
    ).scalar() or 0
    current_chaos = db.session.query(func.sum(ItemSnapshot.price)).filter(
        ItemSnapshot.target.has(user_id=user_id),
        ItemSnapshot.price_currency == 'chaos'
    ).scalar() or 0
    
    return jsonify({
        'yesterday_divine': yesterday_divine,
        'yesterday_chaos': yesterday_chaos,
        'today_divine': today_divine,
        'today_chaos': today_chaos,
        'current_divine': current_divine,
        'current_chaos': current_chaos
    }), 200

@api_bp.route('/events/mark_redundant', methods=['POST'])
@jwt_required()
def mark_redundant():
    user_id = int(get_jwt_identity())
    data = request.get_json()
    event_ids = data.get('event_ids', [])
    if not event_ids:
        return jsonify({'msg': 'No event IDs provided'}), 400
    # 验证这些事件属于当前用户
    events = Event.query.filter(Event.id.in_(event_ids)).all()
    for e in events:
        target = MonitorTarget.query.get(e.target_id)
        if not target or target.user_id != user_id:
            return jsonify({'msg': 'Unauthorized'}), 403
    # 更新标记
    Event.query.filter(Event.id.in_(event_ids)).update(
        {'is_redundant': True},
        synchronize_session=False
    )
    db.session.commit()
    return jsonify({'msg': 'Marked', 'count': len(event_ids)}), 200

@api_bp.route('/logs', methods=['GET'])
@jwt_required()
def get_logs():
    scheduler = current_app.scheduler
    warnings = scheduler.get_warnings(limit=100)
    return jsonify(warnings), 200


@api_bp.route('/events/all', methods=['GET'])
@jwt_required()
def get_all_events():
    user_id = int(get_jwt_identity())
    # 参数
    is_redundant = request.args.get('is_redundant', type=int)
    is_gem = request.args.get('is_gem', type=int)
    since = request.args.get('since')  # ISO 时间字符串
    limit = request.args.get('limit', 1000, type=int)
    
    # 基础查询：当前用户所有目标的事件
    query = Event.query.join(MonitorTarget).filter(MonitorTarget.user_id == user_id)
    
    if is_redundant is not None:
        query = query.filter(Event.is_redundant == is_redundant)
    if is_gem is not None:
        query = query.filter(Event.is_gem == is_gem)
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            query = query.filter(Event.created_at >= since_dt)
        except ValueError:
            return jsonify({'msg': 'since 参数格式错误，请使用 ISO 时间格式'}), 400
    
    # 分页
    total = query.count()
    events = query.order_by(Event.created_at.desc()).limit(limit).all()
    
    def _parse_mods(field):
        if field:
            try:
                v = json.loads(field)
                return v if isinstance(v, list) else []
            except Exception:
                return []
        return []

    return jsonify({
        'total': total,
        'events': [{
            'id': e.id,
            'target_id': e.target_id,
            'target_name': e.target.poe_account_name,
            'target_type': e.target.target_type,
            'item_id': e.item_id,
            'item_name': e.item_name,
            'base_type': e.base_type,
            'explicit_mods': _parse_mods(e.explicit_mods),
            'implicit_mods': _parse_mods(e.implicit_mods),
            'fractured_mods': _parse_mods(e.fractured_mods),
            'crafted_mods': _parse_mods(e.crafted_mods),
            'enchant_mods': _parse_mods(e.enchant_mods),
            'mutated_mods': _parse_mods(e.mutated_mods),
            'veiled_mods': _parse_mods(e.veiled_mods),
            'price': e.price,
            'price_old': e.price_old,
            'price_currency': e.price_currency,
            'event_type': e.event_type,
            'is_redundant': e.is_redundant,
            'is_gem': e.is_gem,
            'seller_account': e.seller_account,
            'created_at': e.created_at.isoformat()
        } for e in events]
    }), 200

@api_bp.route('/targets/<int:target_id>/price_trend', methods=['GET'])
@jwt_required()
def get_price_trend(target_id):
    user_id = int(get_jwt_identity())
    target = MonitorTarget.query.filter_by(id=target_id, user_id=user_id).first()
    if not target:
        return jsonify({'msg': 'Target not found'}), 404
    
    days = request.args.get('days', 30, type=int)
    since = request.args.get('since', None)
    
    # 构建查询
    query = PriceTrend.query.filter(PriceTrend.target_id == target_id)
    
    if since:
        # 增量查询：只返回since之后的数据
        try:
            since_time = datetime.fromisoformat(since.replace('Z', '+00:00'))
            query = query.filter(PriceTrend.scan_time >= since_time)
        except:
            # 如果时间格式错误，忽略since参数
            pass
    else:
        # 完整查询：返回days天的数据
        cutoff = datetime.utcnow() - timedelta(days=days)
        query = query.filter(PriceTrend.scan_time >= cutoff)
    
    trends = query.order_by(PriceTrend.scan_time.asc()).all()
    
    # 根据时间范围决定聚合方式
    if days <= 1:
        # 24小时：返回原始数据（不聚合）
        result = {
            'scan_times': [t.scan_time.isoformat() for t in trends],
            'min_prices': [t.min_price for t in trends],
            'top10_avg_prices': [t.top10_avg_price for t in trends],
            'item_counts': [t.item_count for t in trends],
        }
    else:
        # 7天及以上：按小时聚合（7天=168个点）
        from collections import defaultdict
        hourly = defaultdict(lambda: {'min_prices': [], 'avg_prices': [], 'counts': []})
        for t in trends:
            hour_str = t.scan_time.strftime('%Y-%m-%d %H:00')
            if t.min_price is not None:
                hourly[hour_str]['min_prices'].append(t.min_price)
            if t.top10_avg_price is not None:
                hourly[hour_str]['avg_prices'].append(t.top10_avg_price)
            if t.item_count is not None:
                hourly[hour_str]['counts'].append(t.item_count)
        
        hours = sorted(hourly.keys())
        result = {
            'hours': hours,
            'min_prices': [sum(hourly[h]['min_prices']) // len(hourly[h]['min_prices']) if hourly[h]['min_prices'] else None for h in hours],
            'top10_avg_prices': [sum(hourly[h]['avg_prices']) // len(hourly[h]['avg_prices']) if hourly[h]['avg_prices'] else None for h in hours],
            'item_counts': [sum(hourly[h]['counts']) // len(hourly[h]['counts']) if hourly[h]['counts'] else None for h in hours],
        }
    
    return jsonify(result), 200

@api_bp.route('/search', methods=['POST'])
@jwt_required()
def global_search():
    """全服搜索: 调用腾讯 POE 市集 API"""
    from .utils import decrypt_poesessid
    from .poe_api import PoeAPI
    from .models import UserCredential

    user_id = int(get_jwt_identity())
    data = request.get_json() or {}

    # 获取用户凭证
    cred = UserCredential.query.get(user_id)
    if not cred or not cred.encrypted_poesessid:
        return jsonify({'msg': '请先设置 POESESSID 凭证'}), 400

    poesessid = decrypt_poesessid(cred.encrypted_poesessid)
    pt2gguin = decrypt_poesessid(cred.encrypted_pt2gguin) if cred.encrypted_pt2gguin else None

    league = data.get('league', 'S30赛季')

    # 前端传完整 query: 转换格式后使用
    # 否则用旧的简化参数构建 query
    if data.get('query'):
        raw_query = data['query']
        query = {}
        # 转换 name: 前端传 {"option": "xxx"} -> POE API 需要 "xxx"
        if raw_query.get('name'):
            if isinstance(raw_query['name'], dict) and raw_query['name'].get('option'):
                query['name'] = raw_query['name']['option']
            else:
                query['name'] = raw_query['name']
            # 从 items.json 分离 name 和 type
            # 技能石变体（有disc字段）需要用 {option, discriminator} 格式
            # 注意：技能石名称可能没有空格（如"大法师之力量抽取"），所以不能用空格判断
            if not raw_query.get('type'):
                try:
                    import os, json as _json
                    items_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'items.json')
                    if os.path.exists(items_path):
                        with open(items_path) as _f:
                            _items = _json.load(_f)
                        for _cat in _items.get('result', []):
                            for _entry in _cat.get('entries', []):
                                if _entry.get('text') == query['name']:
                                    # 技能石变体：有disc字段，用 {option, discriminator} 格式
                                    if _entry.get('disc'):
                                        query['type'] = {
                                            'option': _entry['type'],
                                            'discriminator': _entry['disc']
                                        }
                                        del query['name']  # 变体不需要name字段
                                    # 普通物品且名称含空格：分离name和type
                                    elif ' ' in query['name']:
                                        query['type'] = _entry['type']
                                        query['name'] = _entry.get('name', query['name'])
                                    break
                            if query.get('type'):
                                break
                except Exception:
                    pass
        # 转换 type
        if raw_query.get('type'):
            if isinstance(raw_query['type'], dict) and raw_query['type'].get('option'):
                query['type'] = raw_query['type']['option']
            else:
                query['type'] = raw_query['type']
        # 保留 filters 和 stats（需要转换格式）
        if raw_query.get('filters'):
            query['filters'] = convert_filters(raw_query['filters'])
        if raw_query.get('stats'):
            query['stats'] = convert_stats(raw_query['stats'])
        if raw_query.get('status'):
            query['status'] = raw_query['status']
    else:
        query = {}
        if data.get('type'):
            query['type'] = data['type']
        if data.get('name'):
            query['name'] = data['name']
        trade_filters = {}
        if data.get('price_min') is not None or data.get('price_max') is not None:
            price = {}
            if data.get('price_min') is not None:
                price['min'] = data['price_min']
            if data.get('price_max') is not None:
                price['max'] = data['price_max']
            trade_filters['price'] = price
        filters = {}
        if trade_filters:
            filters['trade_filters'] = trade_filters
        if filters:
            query['filters'] = filters
        if data.get('stats'):
            query['stats'] = data['stats']
        if data.get('online_only'):
            query['status'] = {'option': 'online'}

    # 确保 query 不为空，至少添加一个空的 type
    if not query.get('name') and not query.get('type') and not query.get('stats') and not query.get('filters'):
        query['type'] = ''

    try:
        api = PoeAPI(poesessid, pt2gguin)
        page = data.get('page', 0)
        per_page = 20
        print(f"DEBUG: Query sent to POE API: {json.dumps(query, ensure_ascii=False)}")
        result = api.search_global_items(league, query)

        items = []
        if result['item_ids']:
            all_ids = result['item_ids']  # 最多100个
            total = result['total']
            start = page * per_page
            end = start + per_page
            page_ids = all_ids[start:end]
            if page_ids:
                fetched = api.fetch_items_batch(
                    page_ids,
                    result['query_id'],
                    batch_size=10
                )
                items = fetched

        return jsonify({
            'total': result['total'],
            'items': items,
            'page': page,
            'per_page': per_page,
            'query_id': result['query_id'],
        }), 200
    except Exception as e:
        return jsonify({'msg': f'搜索失败: {str(e)}'}), 500


@api_bp.route('/search/monitor', methods=['POST'])
@jwt_required()
def create_global_monitor():
    """从搜索条件创建全服监控: 搜索→拿query_id→建监控目标"""
    from .utils import decrypt_poesessid
    from .poe_api import PoeAPI
    from .models import UserCredential

    user_id = int(get_jwt_identity())
    data = request.get_json() or {}

    # 获取用户凭证
    cred = UserCredential.query.get(user_id)
    if not cred or not cred.encrypted_poesessid:
        return jsonify({'msg': '请先设置 POESESSID 凭证'}), 400

    poesessid = decrypt_poesessid(cred.encrypted_poesessid)
    pt2gguin = decrypt_poesessid(cred.encrypted_pt2gguin) if cred.encrypted_pt2gguin else None

    league = data.get('league', 'S30赛季')
    raw_query = data.get('query')
    scan_interval = data.get('scan_interval', 300)

    if not raw_query:
        return jsonify({'msg': '缺少筛选条件 query'}), 400

    # 转换 query 格式
    query = {}
    # 转换 name: 前端传 {"option": "xxx"} -> POE API 需要 "xxx"
    if raw_query.get('name'):
        if isinstance(raw_query['name'], dict) and raw_query['name'].get('option'):
            query['name'] = raw_query['name']['option']
        else:
            query['name'] = raw_query['name']
    # 转换 type
    if raw_query.get('type'):
        if isinstance(raw_query['type'], dict) and raw_query['type'].get('option'):
            query['type'] = raw_query['type']['option']
        else:
            query['type'] = raw_query['type']
    # 保留 filters 和 stats（需要转换格式）
    if raw_query.get('filters'):
        query['filters'] = convert_filters(raw_query['filters'])
    if raw_query.get('stats'):
        query['stats'] = convert_stats(raw_query['stats'])
    if raw_query.get('status'):
        query['status'] = raw_query['status']

    # 确保 query 不为空，至少添加一个空的 type
    if not query.get('name') and not query.get('type') and not query.get('stats') and not query.get('filters'):
        query['type'] = ''

    # 校验扫描间隔
    user = User.query.get(user_id)
    min_interval = 60 if user.is_admin else 300
    if scan_interval < min_interval:
        return jsonify({'msg': f'扫描间隔不能小于 {min_interval} 秒'}), 400

    try:
        api = PoeAPI(poesessid, pt2gguin)
        # 1. 搜索获取 query_id
        print(f"DEBUG [monitor]: Query sent to POE API: {json.dumps(query, ensure_ascii=False)}")
        result = api.search_global_items(league, query)
        query_id = result['query_id']
        total = result['total']
        item_ids = result['item_ids']

        if not item_ids or total == 0:
            return jsonify({'msg': '未找到匹配物品，请调整筛选条件', 'total': 0}), 400

        # 2. 获取前10个物品的详情（用于初始显示）
        preview_items = api.fetch_items_batch(item_ids[:10], query_id, batch_size=10)

        # 3. 计算前10均价
        prices = []
        for item_data in preview_items:
            listing = item_data.get('listing', {})
            price_info = listing.get('price', {})
            if price_info.get('currency') == 'divine':
                prices.append(price_info.get('amount', 0))
        top10_avg = sum(prices) // len(prices) if prices else 0

        # 4. 创建监控目标
        query_params = dict(query)
        query_params['_query_id'] = query_id  # 存储短代码供后续复用
        target = MonitorTarget(
            user_id=user_id,
            poe_account_name=None,
            league=league,
            scan_interval=scan_interval,
            min_price=None,
            target_type='global',
            query_params=query_params,
        )
        db.session.add(target)
        db.session.commit()

        # 5. 触发立即扫描
        scheduler = current_app.scheduler
        scheduler.trigger_immediate_scan(target.id)

        return jsonify({
            'id': target.id,
            'query_id': query_id,
            'total': total,
            'top10_avg': top10_avg,
            'preview_items': preview_items[:10],
            'msg': '监控任务创建成功'
        }), 201

    except Exception as e:
        return jsonify({'msg': f'创建监控失败: {str(e)}'}), 500


@api_bp.route('/trade/static', methods=['GET'])
def trade_static():
    """代理腾讯 trade data/static 接口 (物品分类/传奇物品)，带缓存"""
    return _cached_trade_response('static.json', 'https://poe.game.qq.com/api/trade/data/static', 'static')


@api_bp.route('/trade/stats', methods=['GET'])
def trade_stats():
    """代理腾讯 trade data/stats 接口 (词缀筛选配置)，带缓存"""
    return _cached_trade_response('stats.json', 'https://poe.game.qq.com/api/trade/data/stats', 'stats')