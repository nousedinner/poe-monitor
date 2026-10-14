"""Fetch first, then atomically diff a complete response against its own target."""

from copy import deepcopy
from decimal import Decimal
from collections import defaultdict
from . import db
from .models import MonitorTarget, UserCredential, ItemSnapshot, Event, PriceTrend, ScanJob
from .items import item_values, fingerprint, validate_details, PRECISION
from .poe_api import PoeAPI
from .timeutils import utcnow
from .utils import decrypt_poesessid


def match_instances(old, new):
    """Match listing IDs first, then only a unique upstream physical item ID.

    Equal fingerprints alone never merge distinct instances or rewrite history.
    """
    pairs = {key: key for key in old.keys() & new.keys()}
    old_stable, new_stable = defaultdict(list), defaultdict(list)
    for key in old.keys() - pairs.keys():
        stable = (old[key].item_data or {}).get("item", {}).get("id")
        if stable:
            old_stable[stable].append(key)
    for key in new.keys() - set(pairs.values()):
        stable = new[key]["item"].get("id")
        if stable:
            new_stable[stable].append(key)
    for stable in old_stable.keys() & new_stable.keys():
        if len(old_stable[stable]) == len(new_stable[stable]) == 1:
            pairs[old_stable[stable][0]] = new_stable[stable][0]
    return pairs


class Scanner:
    def __init__(self, app, api_factory=PoeAPI, cancel=None):
        self.app, self.api_factory, self.cancel = app, api_factory, cancel

    def scan(self, target_id, lease_token=None):
        target = db.session.get(MonitorTarget, target_id)
        if target is None or not target.enabled:
            return False
        credential = db.session.get(UserCredential, target.user_id)
        if credential is None or not credential.encrypted_poesessid:
            raise ValueError("未设置 POESESSID")
        # Detach the HTTP work from the SQLAlchemy transaction/connection.
        config = dict(
            id=target.id,
            user_id=target.user_id,
            revision=target.revision,
            target_type=target.target_type,
            league=target.league,
            account=target.poe_account_name,
            minimum=target.min_price,
            query=deepcopy(target.query_params),
        )
        cookie = decrypt_poesessid(credential.encrypted_poesessid)
        pt = (
            decrypt_poesessid(credential.encrypted_pt2gguin)
            if credential.encrypted_pt2gguin
            else None
        )
        db.session.remove()
        api = self.api_factory(cookie, pt)
        if hasattr(api, "cancel"):
            api.cancel = self.cancel
        try:
            if config["target_type"] == "global":
                if not config["query"]:
                    raise ValueError("全服监控缺少筛选条件")
                result = api.search_global_items(config["league"], config["query"])
            else:
                result = api.search_account_items(
                    config["league"], config["account"], config["minimum"]
                )
            ids = result["item_ids"]
            if (
                not isinstance(ids, list)
                or len(ids) != len(set(ids))
                or (not ids and result.get("total", 0))
            ):
                raise ValueError("搜索结果不完整，本次保留原快照与事件")
            details = api.fetch_items_batch(ids, result["query_id"], batch_size=10) if ids else []
            validate_details(ids, details)
        finally:
            if hasattr(api, "close"):
                api.close()
        if self.cancel and self.cancel.is_set():
            return False
        return self.apply(config, result.get("query_id"), details, lease_token)

    def apply(self, config, query_id, details, lease_token=None):
        target = db.session.get(
            MonitorTarget, config["id"], with_for_update=True, populate_existing=True
        )
        if target is None or not target.enabled or target.revision != config["revision"]:
            db.session.rollback()
            return False
        if lease_token:
            job = db.session.get(ScanJob, target.id, with_for_update=True, populate_existing=True)
            if job is None or job.lease_token != lease_token or job.lease_until <= utcnow():
                db.session.rollback()
                return False
        now = utcnow()
        # The accepted top-100 window and global chaos exclusion are unchanged.
        new = {
            row["id"]: row
            for row in details
            if target.target_type != "global" or row["listing"]["price"]["currency"] != "chaos"
        }
        old = {row.item_id: row for row in ItemSnapshot.query.filter_by(target_id=target.id).all()}
        pairs = (
            match_instances(old, new)
            if target.target_type == "account"
            else {key: key for key in old.keys() & new.keys()}
        )
        if not target.baseline_reset:
            for old_id in old.keys() - pairs.keys():
                previous = old[old_id]
                raw = previous.item_data
                values = item_values(raw)
                values["price"], values["price_currency"] = previous.price, previous.price_currency
                db.session.add(
                    Event(
                        target_id=target.id,
                        **values,
                        event_type="sold",
                        is_gem=raw["item"].get("frameType") == 4,
                        fingerprint_hash=fingerprint(raw["item"]),
                        created_at=now,
                    )
                )
            for new_id in new.keys() - set(pairs.values()):
                self.add_event(target.id, new[new_id], "listed", now)
            for old_id, new_id in pairs.items():
                values = item_values(new[new_id])
                previous = old[old_id]
                if (
                    previous.price != values["price"]
                    or previous.price_currency != values["price_currency"]
                ):
                    self.add_event(target.id, new[new_id], "price_change", now, previous)
        # Update existing rows instead of deleting/reinserting every JSON payload.
        for old_id in old.keys() - pairs.keys():
            db.session.delete(old[old_id])
        db.session.flush()
        by_new_id = {new_id: old[old_id] for old_id, new_id in pairs.items()}
        for key, raw in new.items():
            snapshot = by_new_id.get(key)
            if snapshot is None:
                snapshot = ItemSnapshot(target_id=target.id)
                db.session.add(snapshot)
            for name, value in item_values(raw).items():
                setattr(snapshot, name, value)
            snapshot.rarity = str(raw["item"].get("rarity") or raw["item"].get("frameType", ""))
            snapshot.ilvl = raw["item"].get("ilvl")
            snapshot.item_data = raw
            snapshot.scan_timestamp = now
        if target.target_type == "global":
            prices = sorted(
                item_values(raw)["price"]
                for raw in new.values()
                if raw["listing"]["price"]["currency"] == "divine"
            )
            if prices:
                top = prices[:10]
                db.session.add(
                    PriceTrend(
                        target_id=target.id,
                        scan_time=now,
                        min_price=prices[0],
                        top10_avg_price=(sum(top, Decimal(0)) / len(top)).quantize(PRECISION),
                        item_count=len(prices),
                    )
                )
            if query_id and target.query_params:
                target.query_params = {**target.query_params, "_query_id": query_id}
        target.last_scan = now
        target.baseline_reset = False
        db.session.commit()
        return True

    @staticmethod
    def add_event(target_id, raw, kind, now, previous=None):
        values = item_values(raw)
        db.session.add(
            Event(
                target_id=target_id,
                **values,
                event_type=kind,
                created_at=now,
                price_old=previous.price if previous else None,
                price_currency_old=previous.price_currency if previous else None,
                is_gem=raw["item"].get("frameType") == 4,
                fingerprint_hash=fingerprint(raw["item"]),
            )
        )
