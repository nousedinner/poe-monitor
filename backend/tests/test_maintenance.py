import json
from app import db
from app.models import Event, ItemSnapshot
from app.items import item_values
from conftest import raw_item, snapshot


def test_fingerprint_repair_defaults_to_dry_run_and_skips_uncertain_history(
    app, target, monkeypatch
):
    import app.fill_fingerprint as maintenance

    monkeypatch.setattr(maintenance, "create_app", lambda: app)
    raw = raw_item()
    snapshot(target, raw)
    verified = Event(
        target_id=target.id, **item_values(raw), event_type="listed", fingerprint_hash=None
    )
    unknown = Event(
        target_id=target.id, item_id="no-source", price=10, event_type="sold", fingerprint_hash=None
    )
    db.session.add_all([verified, unknown])
    db.session.commit()
    valid_id, unknown_id = verified.id, unknown.id
    maintenance.main()
    db.session.remove()
    assert db.session.get(Event, valid_id).fingerprint_hash is None
    maintenance.main(apply=True)
    db.session.remove()
    assert db.session.get(Event, valid_id).fingerprint_hash is not None
    assert db.session.get(Event, unknown_id).fingerprint_hash is None


def test_snapshot_repair_keeps_properties_and_never_rewrites_events(app, target, monkeypatch):
    import app.fix_mods as maintenance

    monkeypatch.setattr(maintenance, "create_app", lambda: app)
    raw = raw_item()
    raw["item"]["properties"] = [{"name": "等级", "values": [["20", 0]]}]
    row = snapshot(target, raw)
    row.explicit_mods = "[]"
    event = Event(
        target_id=target.id,
        item_id="A",
        price=10,
        event_type="listed",
        explicit_mods='["historical"]',
    )
    db.session.add(event)
    db.session.commit()
    sid, eid = row.id, event.id
    maintenance.fix()
    db.session.remove()
    assert db.session.get(ItemSnapshot, sid).explicit_mods == "[]"
    maintenance.fix(apply=True)
    db.session.remove()
    assert "等级: 20" in json.loads(db.session.get(ItemSnapshot, sid).explicit_mods)
    assert db.session.get(Event, eid).explicit_mods == '["historical"]'


def test_fingerprint_repair_pages_without_losing_mysql_cursor(app, target, monkeypatch):
    import app.fill_fingerprint as maintenance

    monkeypatch.setattr(maintenance, "create_app", lambda: app)
    raw = raw_item()
    snapshot(target, raw)
    for _ in range(205):
        db.session.add(Event(target_id=target.id, **item_values(raw), event_type="listed"))
    db.session.commit()
    maintenance.main(apply=True)
    db.session.expire_all()
    assert Event.query.filter(Event.fingerprint_hash.is_(None)).count() == 0
