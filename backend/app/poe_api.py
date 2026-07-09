import requests
import time
import re
from typing import List, Dict, Any

class PoeAPI:
    def __init__(self, poesessid: str, pt2gguin: str = None):
        self.cookie = f"POESESSID={poesessid}"
        if pt2gguin:
            self.cookie += f"; pt2gguin={pt2gguin}"
        self.headers = {
            "Content-Type": "application/json",
            "Cookie": self.cookie,
            "X-Requested-With": "XMLHttpRequest"
        }

    def search_account_items(self, league: str, account_name: str, min_price: int = None) -> Dict[str, Any]:
        payload = {
            "query": {
                "status": {"option": "securable"},
                "filters": {
                    "trade_filters": {
                        "filters": {
                            "account": {"input": account_name}
                        }
                    }
                }
            },
            "sort": {"price": "asc"}
        }
        if min_price is not None:
            payload["query"]["filters"]["trade_filters"]["filters"]["price"] = {
                "option": "divine",
                "min": min_price
            }
        url = f"https://poe.game.qq.com/api/trade/search/{league}"
        # 添加超时 90 秒
        resp = requests.post(url, headers=self.headers, json=payload, timeout=90)
        if resp.status_code != 200:
            raise Exception(f"Search failed: {resp.status_code} {resp.text}")
        data = resp.json()
        return {
            "item_ids": data.get("result", []),
            "query_id": data.get("id"),
            "total": data.get("total", 0)
        }

    def search_global_items(self, league: str, query: dict) -> Dict[str, Any]:
        """全服搜索，query 结构与腾讯 /api/trade/search 的 query 字段一致"""
        # 过滤掉内部字段（以下划线开头的）
        clean_query = {k: v for k, v in query.items() if not k.startswith('_')}
        # 从 items.json 分离 name 和 type（如 "法师之血 重革腰带" -> name="法师之血", type="重革腰带"）
        if 'name' in clean_query and 'type' not in clean_query:
            _full_name = clean_query['name']
            _found = False
            # 始终在items.json中查找匹配的text字段
            try:
                import os, json as _json
                items_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'items.json')
                if os.path.exists(items_path):
                    with open(items_path) as _f:
                        _items = _json.load(_f)
                    for _cat in _items.get('result', []):
                        for _entry in _cat.get('entries', []):
                            if _entry.get('text') == _full_name:
                                # 技能宝石变体（有disc字段）使用type和name字段
                                if _entry.get('disc'):
                                    # 保持type字段不变，设置name为完整名称
                                    clean_query['type'] = _entry['type']
                                    clean_query['name'] = _full_name
                                # 传奇物品或其他有name字段的物品
                                elif _entry.get('name'):
                                    clean_query['type'] = _entry['type']
                                    clean_query['name'] = _entry['name']
                                else:
                                    # 普通物品，只有type字段
                                    clean_query['type'] = _entry['type']
                                    if 'name' in clean_query:
                                        del clean_query['name']
                                _found = True
                                break
                        if _found:
                            break
            except Exception:
                pass
            if not _found:
                # name 未匹配到，当作 type 处理（如宝石名 "忠诚伙伴（辅）"）
                clean_query['type'] = clean_query.pop('name')
        payload = {
            "query": clean_query,
            "sort": {"price": "asc"},
        }
        url = f"https://poe.game.qq.com/api/trade/search/{league}"
        resp = requests.post(url, headers=self.headers, json=payload, timeout=90)
        if resp.status_code != 200:
            raise Exception(f"Global search failed: {resp.status_code} {resp.text[:500]}")
        data = resp.json()
        return {
            "item_ids": data.get("result", [])[:100],  # 最多100个
            "query_id": data.get("id"),
            "total": data.get("total", 0)
        }

    def fetch_items_batch(self, item_ids: List[str], query_id: str, batch_size: int = 20) -> List[Dict[str, Any]]:
        all_items = []
        for i in range(0, len(item_ids), batch_size):
            batch = item_ids[i:i+batch_size]
            ids_str = ','.join(batch)
            url = f"https://poe.game.qq.com/api/trade/fetch/{ids_str}?query={query_id}"
            print(f"DEBUG: 批量请求 URL: {url}")
            print(f"DEBUG: 使用的 query_id: {query_id}")
            print(f"DEBUG: 批次物品数量: {len(batch)}")
            retries = 3
            while retries > 0:
                try:
                    # 添加超时 90 秒
                    resp = requests.get(url, headers=self.headers, timeout=90)
                except requests.exceptions.Timeout:
                    print(f"请求超时，剩余重试次数 {retries-1}")
                    retries -= 1
                    if retries == 0:
                        raise Exception(f"Fetch batch timed out after retries: {batch}")
                    time.sleep(2)  # 等待 2 秒再重试
                    continue
                except requests.exceptions.ConnectionError as e:
                    print(f"连接错误: {e}，剩余重试次数 {retries-1}")
                    retries -= 1
                    if retries == 0:
                        raise Exception(f"Fetch batch connection error after retries: {batch}")
                    time.sleep(2)
                    continue
                if resp.status_code == 200:
                    result = resp.json().get("result", [])
                    all_items.extend(result)
                    break
                elif resp.status_code == 429:
                    try:
                        err = resp.json()
                        msg = err.get("error", {}).get("message", "")
                        match = re.search(r"等待 (\d+) 秒", msg)
                        wait = int(match.group(1)) if match else 10
                    except:
                        wait = 10
                    print(f"限流，等待 {wait} 秒后重试...")
                    time.sleep(wait)
                    retries -= 1
                else:
                    print(f"DEBUG: 响应状态码 {resp.status_code}, 内容: {resp.text}")
                    raise Exception(f"Fetch failed: {resp.status_code} {resp.text}")
            if retries == 0:
                raise Exception(f"Failed to fetch batch after retries: {batch}")
            time.sleep(0.5)
        return all_items