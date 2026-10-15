"""Fetch first, then atomically diff a complete response against its own target."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from collections import defaultdict
from . import db
from .models import MonitorTarget, UserCredential, ItemSnapshot, Event, PriceTrend, ScanJob
from .items import item_values, fingerprint, validate_details, PRECISION
from .poe_api import PoeAPI
from .timeutils import utcnow
from .utils import decrypt_poesessid


def match_instances(old, new):
    """Keep instances separate while recognizing non-gem relistings.

    Both IDs can change when an item is relisted. Non-gems therefore retain
    fingerprint matching; gems use listing IDs, as in the original scanner.
    Lists preserve quantities when several non-gems share one fingerprint.
    """
    pairs = {key: key for key in old.keys() & new.keys()}
    old_fingerprints, new_fingerprints = defaultdict(list), defaultdict(list)
    for key in old.keys() - pairs.keys():
        info = old[key].item_data["item"]
        if info.get("frameType") != 4:
            old_fingerprints[fingerprint(info)].append(key)
    for key in new.keys() - set(pairs.values()):
        info = new[key]["item"]
        if info.get("frameType") != 4:
            new_fingerprints[fingerprint(info)].append(key)
    for fp in old_fingerprints.keys() & new_fingerprints.keys():
        remaining_old = sorted(old_fingerprints[fp])
        remaining_new = sorted(new_fingerprints[fp])
        # Preserve unchanged quotes first, so reordering identical instances
        # does not manufacture price changes. Pair each instance at most once.
        for old_id in remaining_old[:]:
            previous = old[old_id]
            match = next(
                (
                    key
                    for key in remaining_new
                    if item_values(new[key])["price"] == previous.price
                    and new[key]["listing"]["price"]["currency"] == previous.price_currency
                ),
                None,
            )
            if match is not None:
                pairs[old_id] = match
                remaining_old.remove(old_id)
                remaining_new.remove(match)
        pairs.update(zip(remaining_old, remaining_new))
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
            for new_id in sorted(new.keys() - set(pairs.values())):
                raw = new[new_id]
                if target.target_type != "account" or not self.correct_relisting(
                    target.id, raw, now
                ):
                    self.add_event(target.id, raw, "listed", now)
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
    def correct_relisting(target_id, raw, now):
        """Restore the original account backtracking policy, including its windows.

        Relisting can change both listing ID and item.id. The fingerprint is
        deliberately used across IDs to retract the disappearance observation.
        Original backend policy: non-gems 24 hours, gems 10 minutes. The old
        frontend's gem exclusion was a separate historical-cleanup mechanism.
        """
        is_gem = raw["item"].get("frameType") == 4
        cutoff = now - (timedelta(minutes=10) if is_gem else timedelta(hours=24))
        fp = fingerprint(raw["item"])
        recent_sold = (
            Event.query.filter(
                Event.target_id == target_id,
                Event.fingerprint_hash == fp,
                Event.event_type == "sold",
                Event.created_at >= cutoff,
            )
            .order_by(Event.created_at.desc(), Event.id.desc())
            .first()
        )
        if recent_sold is None:
            return False
        values = item_values(raw)
        if (
            recent_sold.price == values["price"]
            and recent_sold.price_currency == values["price_currency"]
        ):
            db.session.delete(recent_sold)
        else:
            recent_sold.event_type = "price_change"
            recent_sold.price_old = recent_sold.price
            recent_sold.price_currency_old = recent_sold.price_currency
            recent_sold.price = values["price"]
            recent_sold.price_currency = values["price_currency"]
            recent_sold.is_gem = is_gem
            recent_sold.fingerprint_hash = fp
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
