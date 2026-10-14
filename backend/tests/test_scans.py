from decimal import Decimal
from datetime import timedelta
import pytest
from sqlalchemy import event, update
from app import db
from app.models import MonitorTarget, ItemSnapshot, Event, PriceTrend, ScanJob
from app.scan import Scanner
from app.items import fingerprint
from app.timeutils import utcnow
from conftest import raw_item, snapshot


def scan(app, target, fake_api):
    return Scanner(app, api_factory=fake_api).scan(target.id)


def test_price_change_metadata_currency_and_fraction(app, target, fake_api):
    snapshot(target, raw_item("A", "Alpha", 10))
    snapshot(target, raw_item("B", "Beta", 30))
    fake_api.items = [raw_item("A", "Alpha", 10.125, "mirror"), raw_item("B", "Beta", 30)]
    assert scan(app, target, fake_api)
    row = Event.query.one()
    assert (
        row.item_id,
        row.item_name,
        row.price,
        row.price_old,
        row.price_currency,
        row.price_currency_old,
    ) == ("A", "Alpha", Decimal("10.125"), Decimal(10), "mirror", "divine")
    assert row.fingerprint_hash == fingerprint(raw_item()["item"])
    assert fake_api.closed


def test_identical_instances_and_legitimate_repeated_sales(app, target, fake_api):
    target_id = target.id
    fake_api.items = [raw_item("A", "Same"), raw_item("B", "Same")]
    scan(app, target, fake_api)
    assert Event.query.filter_by(event_type="listed").count() == 2
    fake_api.items = [raw_item("B", "Same")]
    scan(app, db.session.get(MonitorTarget, target_id), fake_api)
    assert Event.query.filter_by(event_type="sold", item_id="A").count() == 1
    fake_api.items = [raw_item("B", "Same"), raw_item("C", "Same")]
    scan(app, db.session.get(MonitorTarget, target_id), fake_api)
    assert Event.query.filter_by(event_type="listed", item_id="C").count() == 1
    assert Event.query.filter_by(event_type="sold", item_id="A").count() == 1


@pytest.mark.parametrize("kind", ["account", "global"])
def test_successful_empty_result_runs_normal_diff(app, target, fake_api, kind):
    target.target_type = kind
    target.query_params = {"type": "Sword"} if kind == "global" else None
    db.session.commit()
    snapshot(target, raw_item())
    assert scan(app, target, fake_api)
    assert ItemSnapshot.query.count() == 0
    assert Event.query.filter_by(event_type="sold").count() == 1
    assert db.session.get(MonitorTarget, target.id).last_scan is not None


@pytest.mark.parametrize(
    "details", [[raw_item()], [], [raw_item(), None], [raw_item(), raw_item()]]
)
def test_partial_or_duplicate_details_preserve_baseline(app, target, fake_api, details):
    snapshot(target, raw_item("A"))
    snapshot(target, raw_item("B", "Beta"))
    fake_api.ids = ["A", "B"]
    fake_api.items = details
    with pytest.raises(ValueError, match="详情"):
        scan(app, target, fake_api)
    assert ItemSnapshot.query.count() == 2
    assert Event.query.count() == 0
    assert db.session.get(MonitorTarget, target.id).last_scan is None


def test_currency_only_change_is_recorded(app, target, fake_api):
    snapshot(target, raw_item())
    fake_api.items = [raw_item(currency="mirror")]
    scan(app, target, fake_api)
    assert Event.query.one().price_currency_old == "divine"


def test_stable_physical_id_can_match_changed_listing(app, target, fake_api):
    snapshot(target, raw_item("old", "Alpha", 10, stable="physical-A"))
    fake_api.items = [raw_item("new", "Alpha", 12, stable="physical-A")]
    scan(app, target, fake_api)
    assert Event.query.one().event_type == "price_change"
    assert ItemSnapshot.query.one().item_id == "new"


def test_same_fingerprint_different_id_does_not_erase_history(app, target, fake_api):
    snapshot(target, raw_item("old", "Same"))
    fake_api.items = [raw_item("new", "Same")]
    scan(app, target, fake_api)
    assert sorted(row.event_type for row in Event.query.all()) == ["listed", "sold"]


def test_query_id_persists_and_mean_keeps_fraction(app, target, fake_api):
    tid = target.id
    target.target_type = "global"
    target.query_params = {"type": "Sword", "_query_id": "old"}
    db.session.commit()
    fake_api.items = [raw_item("A", price=1), raw_item("B", price=2)]
    scan(app, target, fake_api)
    db.session.expire_all()
    assert db.session.get(MonitorTarget, tid).query_params["_query_id"] == "new-query"
    assert PriceTrend.query.one().top10_avg_price == Decimal("1.5")


def test_unchanged_json_is_not_deleted_or_rewritten(app, target, fake_api):
    snapshot(target, raw_item())
    fake_api.items = [raw_item()]
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement.lower())

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        scan(app, target, fake_api)
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    writes = [
        s
        for s in statements
        if s.startswith(
            ("update item_snapshots", "delete from item_snapshots", "insert into item_snapshots")
        )
    ]
    assert writes and all(
        s.startswith("update item_snapshots") and "item_data=" not in s.replace(" ", "")
        for s in writes
    )
    assert Event.query.count() == 0


def test_changed_filter_rejects_inflight_scan(app, target, fake_api):
    tid = target.id
    snapshot(target, raw_item())

    def change():
        with db.engine.begin() as conn:
            conn.execute(
                update(MonitorTarget)
                .where(MonitorTarget.id == tid)
                .values(revision=1, baseline_reset=True)
            )

    fake_api.before_fetch = staticmethod(change)
    fake_api.items = [raw_item(price=20)]
    assert not scan(app, target, fake_api)
    assert Event.query.count() == 0
    assert ItemSnapshot.query.one().price == 10


def test_filter_reset_establishes_baseline_without_fake_sales(app, target, fake_api):
    snapshot(target, raw_item())
    target.baseline_reset = True
    db.session.commit()
    fake_api.items = [raw_item("B", "New filter")]
    scan(app, target, fake_api)
    assert Event.query.count() == 0
    assert ItemSnapshot.query.one().item_id == "B"


def test_stale_job_lease_cannot_write(app, target, fake_api):
    tid = target.id
    db.session.add(
        ScanJob(
            target_id=tid,
            state="running",
            lease_token="current",
            lease_until=utcnow() + timedelta(minutes=10),
        )
    )
    db.session.commit()
    fake_api.items = [raw_item()]
    assert not Scanner(app, api_factory=fake_api).scan(tid, lease_token="old")
    assert ItemSnapshot.query.count() == 0 and Event.query.count() == 0


def test_accepted_top100_window_behavior_is_unchanged(app, target, fake_api):
    target.target_type = "global"
    target.query_params = {"type": "Sword"}
    db.session.commit()
    snapshot(target, raw_item("outside", "Outside window"))
    fake_api.total = 101
    fake_api.items = [raw_item(str(n), str(n)) for n in range(100)]
    scan(app, target, fake_api)
    # Explicitly deferred by the user: no attempt to infer ranks outside 100.
    assert Event.query.filter_by(item_id="outside", event_type="sold").count() == 1


def test_expired_upstream_cookie_is_not_auto_handled(app, target, fake_api):
    tid = target.id
    snapshot(target, raw_item())
    fake_api.error = RuntimeError("Search failed: 401")
    with pytest.raises(RuntimeError, match="401"):
        scan(app, target, fake_api)
    assert ItemSnapshot.query.count() == 1 and Event.query.count() == 0
    assert db.session.get(MonitorTarget, tid).enabled
