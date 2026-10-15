"""Relisting behavior is intentional: neither upstream ID is assumed stable."""

import ast
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
import subprocess

import pytest
import requests
from sqlalchemy import Column, MetaData, Table
from sqlalchemy.orm import registry

from app import db
from app.items import fingerprint, item_values
from app.models import Event, ItemSnapshot, MonitorTarget, UserCredential
from app.scan import Scanner
from app.utils import decrypt_poesessid
from conftest import raw_item


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = datetime(2026, 10, 15, 0, 0)

    monkeypatch.setattr("app.scan.utcnow", lambda: Clock.now)
    return Clock


def run_round(app, target_id, api, items):
    api.items = items
    assert Scanner(app, api_factory=api).scan(target_id)


def relisted(key, frame=2, price=10, currency="divine"):
    return raw_item(key, "Same item", price, currency, frame, stable="physical-" + key)


def sold(target_id, raw, when):
    value = Event(
        target_id=target_id,
        **item_values(raw),
        event_type="sold",
        is_gem=raw["item"]["frameType"] == 4,
        fingerprint_hash=fingerprint(raw["item"]),
        created_at=when,
    )
    db.session.add(value)
    db.session.commit()
    return value.id


@pytest.mark.parametrize("frame", [2, 4], ids=["equipment-24h", "gem-10min"])
def test_same_price_relisting_after_gap_retracts_disappearance(app, target, fake_api, clock, frame):
    tid = target.id
    run_round(app, tid, fake_api, [relisted("old", frame)])
    clock.now += timedelta(minutes=1)
    run_round(app, tid, fake_api, [])
    sold_id = Event.query.filter_by(event_type="sold").one().id
    clock.now += timedelta(minutes=1)
    run_round(app, tid, fake_api, [relisted("new", frame)])
    assert db.session.get(Event, sold_id) is None
    assert [(e.event_type, e.item_id) for e in Event.query.all()] == [("listed", "old")]
    current = ItemSnapshot.query.one()
    assert current.item_id == "new" and current.item_data["item"]["id"] == "physical-new"


@pytest.mark.parametrize("frame", [2, 4], ids=["equipment", "gem"])
@pytest.mark.parametrize("price,currency", [(12, "divine"), (10, "mirror")])
def test_relisting_rewrites_sold_as_price_change(
    app, target, fake_api, clock, frame, price, currency
):
    tid = target.id
    run_round(app, tid, fake_api, [relisted("old", frame)])
    clock.now += timedelta(minutes=1)
    run_round(app, tid, fake_api, [])
    original = Event.query.filter_by(event_type="sold").one()
    sold_id, disappeared_at = original.id, original.created_at
    clock.now += timedelta(minutes=1)
    run_round(app, tid, fake_api, [relisted("new", frame, price, currency)])
    corrected = db.session.get(Event, sold_id)
    assert corrected.event_type == "price_change"
    assert (corrected.price_old, corrected.price) == (10, price)
    assert (corrected.price_currency_old, corrected.price_currency) == ("divine", currency)
    assert corrected.created_at == disappeared_at
    assert corrected.item_id == "old"  # Original backend retains the prior event's identity/time.
    assert corrected.is_gem == (frame == 4)
    assert Event.query.filter_by(event_type="listed").count() == 1


@pytest.mark.parametrize(
    "frame,age,corrected",
    [
        (2, 86399, True),
        (2, 86400, True),
        (2, 86401, False),
        (4, 599, True),
        (4, 600, True),
        (4, 601, False),
    ],
)
def test_original_window_including_cutoff(app, target, fake_api, clock, frame, age, corrected):
    tid = target.id
    sold_id = sold(tid, relisted("old", frame), clock.now - timedelta(seconds=age))
    run_round(app, tid, fake_api, [relisted("new", frame)])
    assert (db.session.get(Event, sold_id) is None) == corrected
    assert Event.query.filter_by(event_type="listed").count() == (0 if corrected else 1)


def test_no_backtracking_across_targets(app, target, fake_api, clock):
    tid = target.id
    other = MonitorTarget(user_id=target.user_id, poe_account_name="other-account")
    db.session.add(other)
    db.session.commit()
    other_sold = sold(other.id, relisted("old"), clock.now - timedelta(minutes=1))
    run_round(app, tid, fake_api, [relisted("new")])
    assert db.session.get(Event, other_sold).event_type == "sold"
    assert Event.query.filter_by(target_id=tid, event_type="listed").count() == 1


def test_global_monitoring_keeps_id_comparison(app, target, fake_api, clock):
    tid = target.id
    target.target_type = "global"
    target.query_params = {"type": "Sword"}
    db.session.commit()
    sold_id = sold(tid, relisted("old"), clock.now - timedelta(minutes=1))
    run_round(app, tid, fake_api, [relisted("new")])
    assert db.session.get(Event, sold_id).event_type == "sold"
    assert Event.query.filter_by(event_type="listed").count() == 1


def test_same_fingerprint_quantities_and_prices_survive_id_changes(app, target, fake_api):
    tid = target.id
    run_round(
        app,
        tid,
        fake_api,
        [relisted("A", price=10), relisted("B", price=20), relisted("C", price=30)],
    )
    assert Event.query.filter_by(event_type="listed").count() == 3
    run_round(
        app,
        tid,
        fake_api,
        [relisted("D", price=20), relisted("E", price=10), relisted("F", price=31)],
    )
    assert ItemSnapshot.query.count() == 3
    assert Event.query.filter_by(event_type="sold").count() == 0
    change = Event.query.filter_by(event_type="price_change").one()
    assert (change.item_id, change.price_old, change.price) == ("F", 30, 31)


@pytest.mark.parametrize("frame", [2, 4], ids=["equipment", "gem"])
def test_each_relisting_corrects_only_one_sold(app, target, fake_api, clock, frame):
    tid = target.id
    ids = [sold(tid, relisted(key, frame), clock.now - timedelta(minutes=1)) for key in ["A", "B"]]
    run_round(app, tid, fake_api, [relisted("C", frame), relisted("D", frame)])
    assert all(db.session.get(Event, key) is None for key in ids)
    assert Event.query.count() == 0
    assert ItemSnapshot.query.count() == 2


def test_identical_gems_stay_separate_before_any_backtracking(app, target, fake_api):
    tid = target.id
    run_round(app, tid, fake_api, [relisted(key, 4) for key in "ABCD"])
    assert ItemSnapshot.query.count() == Event.query.filter_by(event_type="listed").count() == 4
    run_round(app, tid, fake_api, [relisted(key, 4) for key in "BD"])
    assert {e.item_id for e in Event.query.filter_by(event_type="sold")} == {"A", "C"}


@pytest.mark.parametrize("frame", [2, 4], ids=["equipment", "gem"])
def test_unique_item_event_behavior_matches_original_scanner(app, target, fake_api, frame):
    """Execute original scanner code as a reference, rather than assuming its rules."""
    repo = Path(__file__).resolve().parents[2]
    source = subprocess.check_output(
        ["git", "show", "eaf14feeec79bab8b95b04dcd889baff4bbf6968:backend/app/scheduler.py"],
        cwd=repo,
    ).decode("utf8")
    wanted = {
        "_extract_mods_by_type",
        "_collect_all_mods",
        "_compute_fingerprint_hash",
        "_scan_target",
    }
    methods = [
        m
        for c in ast.parse(source).body
        if isinstance(c, ast.ClassDef)
        for m in c.body
        if isinstance(m, ast.FunctionDef) and m.name in wanted
    ]
    module = ast.Module(
        body=[ast.ClassDef(name="Legacy", bases=[], keywords=[], body=methods, decorator_list=[])],
        type_ignores=[],
    )
    # The original scanner deletes/reinserts snapshots and predates the new
    # unique constraint. Give its untouched code an equivalent test-only table.
    legacy_table = Table(
        "legacy_item_snapshots",
        MetaData(),
        *[
            Column(
                c.name, c.type, primary_key=c.primary_key, nullable=c.nullable, default=c.default
            )
            for c in ItemSnapshot.__table__.columns
        ],
    )
    legacy_table.create(db.engine)

    class LegacySnapshot:
        query = db.session.query_property()

    mapping = registry()
    mapping.map_imperatively(LegacySnapshot, legacy_table)
    namespace = dict(
        db=db,
        Event=Event,
        ItemSnapshot=LegacySnapshot,
        UserCredential=UserCredential,
        PoeAPI=fake_api,
        datetime=datetime,
        timedelta=timedelta,
        hashlib=hashlib,
        json=json,
        requests=requests,
        logger=logging.getLogger(__name__),
        decrypt_poesessid=decrypt_poesessid,
    )
    exec(compile(ast.fix_missing_locations(module), "<original-scanner>", "exec"), namespace)
    legacy = namespace["Legacy"]()
    legacy._add_warning = lambda message: pytest.fail("Original scanner warning: " + message)
    original_target = MonitorTarget(user_id=target.user_id, poe_account_name="original-account")
    db.session.add(original_target)
    db.session.commit()
    restored_id, legacy_id = target.id, original_target.id

    def events(tid):
        columns = [
            "item_id",
            "item_name",
            "base_type",
            "event_type",
            "price",
            "price_old",
            "price_currency",
            "is_gem",
            "fingerprint_hash",
        ]
        return Counter(
            tuple(getattr(e, column) for column in columns)
            for e in Event.query.filter_by(target_id=tid).order_by(Event.id)
        )

    # Keep an unrelated listing throughout: the original all-empty early-return
    # bug was fixed separately and must not be restored with fingerprint policy.
    anchor = raw_item("anchor", "Unrelated anchor")
    # Continuous ID replacement, a visible gap, same-price return, then changed-price return.
    try:
        for items in [
            [relisted("A", frame)],
            [relisted("B", frame)],
            [],
            [relisted("C", frame)],
            [],
            [relisted("D", frame, 12)],
        ]:
            items = [*items, anchor]
            fake_api.items = items
            legacy._scan_target(db.session.get(MonitorTarget, legacy_id))
            run_round(app, restored_id, fake_api, items)
            assert events(restored_id) == events(legacy_id)
    finally:
        db.session.remove()
        legacy_table.drop(db.engine)
        mapping.dispose()
