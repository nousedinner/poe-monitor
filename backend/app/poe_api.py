import time
import re
from email.utils import parsedate_to_datetime
from urllib.parse import quote
import requests
from flask import has_app_context, current_app
from .tasks import RequestPacer
from .trade_query import normalize_query


class PoeAPI:
    def __init__(self, poesessid, pt2gguin=None):
        self.cookie = f"POESESSID={poesessid}"
        if pt2gguin:
            self.cookie += f"; pt2gguin={pt2gguin}"
        self.session = requests.Session()
        self.headers = {
            "Content-Type": "application/json",
            "Cookie": self.cookie,
            "X-Requested-With": "XMLHttpRequest",
        }
        self.pacer = RequestPacer(self.cookie) if has_app_context() else None
        self.base_url = (
            current_app.config["POE_API_BASE_URL"]
            if has_app_context()
            else "https://poe.game.qq.com/api/trade"
        ).rstrip("/")
        self.deadline = time.monotonic() + 600
        self.cancel = None

    def close(self):
        self.session.close()

    @staticmethod
    def retry_delay(response):
        value = response.headers.get("Retry-After", "")
        try:
            return max(0, min(float(value), 600))
        except (ValueError, TypeError):
            try:
                return max(0, min(parsedate_to_datetime(value).timestamp() - time.time(), 600))
            except (ValueError, TypeError):
                try:
                    message = response.json().get("error", {}).get("message", "")
                    match = re.search(r"等待\s*(\d+)\s*秒", message)
                    return min(int(match[1]), 600) if match else 10
                except (ValueError, TypeError, AttributeError):
                    return 10

    def wait(self, seconds):
        remaining = self.deadline - time.monotonic()
        if remaining <= seconds or (self.cancel and self.cancel.is_set()):
            raise TimeoutError("请求已取消或超过总时间预算")
        if self.cancel:
            if self.cancel.wait(seconds):
                raise TimeoutError("请求已取消")
        else:
            time.sleep(seconds)

    def request(self, method, url, label, **kwargs):
        for attempt in range(3):
            if self.pacer:
                self.pacer.wait(self.deadline, self.cancel)
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or (self.cancel and self.cancel.is_set()):
                raise TimeoutError("请求已取消或超过总时间预算")
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=self.headers,
                    timeout=(min(10, remaining), min(30, remaining)),
                    **kwargs,
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise
                self.wait(2**attempt)
                continue
            if response.status_code == 429:
                wait = self.retry_delay(response)
                if self.pacer:
                    self.pacer.postpone(wait)
                if attempt == 2:
                    raise RuntimeError(f"{label} failed: 429 请求限流")
                self.wait(wait)
                continue
            if response.status_code >= 500 and attempt < 2:
                self.wait(2**attempt)
                continue
            if response.status_code != 200:
                # 401 is deliberately not refreshed, suspended, or retried.
                # Do not echo arbitrary upstream HTML, cookies, or full bodies.
                raise RuntimeError(f"{label} failed: {response.status_code}")
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("result"), list):
                raise ValueError("上游响应结构无效")
            return data
        raise RuntimeError("请求重试耗尽")

    def search_account_items(self, league, account_name, min_price=None):
        filters = {"account": {"input": account_name}}
        if min_price is not None:
            filters["price"] = {"option": "divine", "min": float(min_price)}
        payload = {
            "query": {
                "status": {"option": "securable"},
                "filters": {"trade_filters": {"filters": filters}},
            },
            "sort": {"price": "asc"},
        }
        data = self.request(
            "POST", f"{self.base_url}/search/{quote(league, safe='')}", "Search", json=payload
        )
        return {
            "item_ids": data["result"],
            "query_id": data.get("id"),
            "total": data.get("total", 0),
        }

    def search_global_items(self, league, query):
        payload = {"query": normalize_query(query), "sort": {"price": "asc"}}
        data = self.request(
            "POST",
            f"{self.base_url}/search/{quote(league, safe='')}",
            "Global search",
            json=payload,
        )
        # Accepted Tencent limit: unchanged. No attempt to track outside it.
        return {
            "item_ids": data["result"][:100],
            "query_id": data.get("id"),
            "total": data.get("total", 0),
        }

    def fetch_items_batch(self, item_ids, query_id, batch_size=10):
        if not isinstance(batch_size, int) or not 1 <= batch_size <= 10:
            raise ValueError("batch_size 必须为 1 到 10")
        rows = []
        for offset in range(0, len(item_ids), batch_size):
            ids = ",".join(
                quote(value, safe="") for value in item_ids[offset : offset + batch_size]
            )
            data = self.request(
                "GET",
                f"{self.base_url}/fetch/{ids}?query={quote(query_id or '', safe='')}",
                "Fetch",
            )
            rows.extend(data["result"])
        return rows
