from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import ast
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import threading
import time
import pytest
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from cryptography.fernet import Fernet
import base64
from app import db
from app.models import MonitorTarget, ScanJob, Event, RequestGate, User
from app.schema import upgrade
from app.scheduler import MonitorScheduler
from app.scan import Scanner
from app.tasks import SingletonLease, RequestPacer
from app.items import fingerprint, money
from app.timeutils import utcnow
from app.utils import encrypt_poesessid, decrypt_poesessid
from conftest import raw_item, HASH

REPO = Path(__file__).resolve().parents[2]
BASELINE = "eaf14feeec79bab8b95b04dcd889baff4bbf6968"


def baseline_file(path):
    return subprocess.check_output(["git", "show", BASELINE + ":" + path], cwd=REPO).decode("utf8")


def test_legacy_schema_upgrade_is_repeatable_and_preserves_history(app, context):
    db.drop_all()
    legacy_db = SQLAlchemy()
    legacy_app = Flask("legacy-schema")
    legacy_app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DATABASE_URI"]
    legacy_db.init_app(legacy_app)
    namespace = {"db": legacy_db, "__name__": "legacy_models"}
    source = baseline_file("backend/app/models.py").replace("from . import db", "")
    exec(compile(source, "legacy_models.py", "exec"), namespace)
    with legacy_app.app_context():
        legacy_db.create_all()
        u = namespace["User"](username="legacy", password_hash=HASH)
        legacy_db.session.add(u)
        legacy_db.session.flush()
        t = namespace["MonitorTarget"](
            user_id=u.id, poe_account_name="legacy-account", league="S30赛季"
        )
        legacy_db.session.add(t)
        legacy_db.session.flush()
        e = namespace["Event"](
            target_id=t.id, item_id="legacy-item", price=25, event_type="sold", is_redundant=True
        )
        legacy_db.session.add(e)
        legacy_db.session.commit()
        uid, tid, eid = u.id, t.id, e.id
        legacy_db.session.remove()
        legacy_db.engine.dispose()
    upgrade()
    upgrade()
    db.session.expire_all()
    assert db.session.get(User, uid).password_hash == HASH
    assert db.session.get(Event, eid).price == 25 and db.session.get(Event, eid).is_redundant
    assert db.session.get(MonitorTarget, tid).revision == 0
    # New fractional values really round-trip through the migrated database.
    db.session.add(
        Event(
            target_id=tid,
            item_id="fraction",
            price=Decimal("1.125"),
            price_currency_old="chaos",
            event_type="price_change",
        )
    )
    db.session.commit()
    db.session.expire_all()
    assert Event.query.filter_by(item_id="fraction").one().price == Decimal("1.125")
    indexes = inspect(db.engine).get_indexes("events")
    assert any(row["column_names"] == ["target_id", "created_at", "id"] for row in indexes)
    assert app.test_client().get("/readyz").status_code == 200
    if db.engine.dialect.name == "mysql":
        columns = {row["name"]: row["type"] for row in inspect(db.engine).get_columns("events")}
        assert columns["price"].precision == 20 and columns["price"].scale == 6


def test_duplicate_snapshot_preflight_does_not_discard_rows(app, context):
    db.drop_all()
    # Minimal legacy tables intentionally have no unique snapshot index.
    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE monitor_targets (id INTEGER PRIMARY KEY, user_id INTEGER)"))
        conn.execute(
            text(
                "CREATE TABLE item_snapshots (id INTEGER PRIMARY KEY, target_id INTEGER, item_id VARCHAR(255))"
            )
        )
        conn.execute(text("INSERT INTO item_snapshots VALUES (1,1,'A'),(2,1,'A')"))
    with pytest.raises(RuntimeError, match="重复快照"):
        upgrade()
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM item_snapshots")).scalar() == 2
    assert "revision" not in {
        row["name"] for row in inspect(db.engine).get_columns("monitor_targets")
    }


def test_wsgi_import_has_dispatcher_without_worker_threads(app, context, monkeypatch):
    before = {t.ident for t in threading.enumerate()}
    monkeypatch.setattr("app.create_app", lambda: app)
    module = runpy.run_path(str(REPO / "backend" / "main.py"), run_name="wsgi-import-test")
    assert module["app"].scheduler is not None
    assert before == {t.ident for t in threading.enumerate()}


def test_singleton_worker_and_recover_expired_scan_lease(app, target):
    one, two = SingletonLease(), SingletonLease()
    assert one.renew() and not two.renew()
    one.release()
    assert two.renew()
    worker = MonitorScheduler(app)
    db.session.add(
        ScanJob(
            target_id=target.id,
            state="running",
            lease_token="dead",
            lease_until=utcnow() - timedelta(seconds=1),
            next_run_at=utcnow() - timedelta(minutes=1),
        )
    )
    db.session.commit()
    result = worker.claim()
    assert result[0] == target.id and result[2] != "dead"
    assert worker.claim() is None


def test_disabled_targets_not_scheduled(app, target):
    target.enabled = False
    db.session.commit()
    worker = MonitorScheduler(app)
    worker.ensure_jobs()
    assert ScanJob.query.count() == 0 and worker.claim() is None


def test_graceful_stop_releases_claimed_but_not_started_jobs(app, target, monkeypatch):
    from concurrent.futures import Future
    import app.scheduler as module

    worker = MonitorScheduler(app)
    tid = target.id

    class Pool:
        def __init__(self, **kwargs):
            self.pending = []

        def submit(self, *args):
            future = Future()
            self.pending.append(future)
            worker.shutdown()
            return future

        def shutdown(self, **kwargs):
            assert worker.stop_event.is_set()
            for future in self.pending:
                future.cancel()

    monkeypatch.setattr(module, "ThreadPoolExecutor", Pool)
    worker.run()
    db.session.remove()
    job = db.session.get(ScanJob, tid)
    assert job.state == "idle" and job.lease_token is None


def test_worker_execute_commits_and_reschedules(app, target, fake_api):
    tid = target.id
    worker = MonitorScheduler(app)
    worker.ensure_jobs()
    claimed = worker.claim()
    fake_api.items = [raw_item()]
    worker.scanner = Scanner(app, api_factory=fake_api)
    worker.execute(*claimed)
    db.session.remove()
    assert Event.query.filter_by(target_id=tid, event_type="listed").count() == 1
    assert db.session.get(ScanJob, tid).state == "idle"
    assert db.session.get(ScanJob, tid).next_run_at > utcnow() + timedelta(seconds=290)


def test_mysql_request_pacing_is_shared_and_fractional(app, context):
    if db.engine.dialect.name != "mysql":
        pytest.skip("Real fractional DATETIME and row locks verified in MySQL pass")
    app.config["POE_REQUEST_INTERVAL"] = 0.04
    db.session.remove()

    def request(_):
        with app.app_context():
            RequestPacer("synthetic-shared-cookie").wait(time.monotonic() + 10)
            return time.monotonic()

    with ThreadPoolExecutor(max_workers=5) as pool:
        times = sorted(pool.map(request, range(5)))
    assert all(b - a >= 0.025 for a, b in zip(times, times[1:])), times
    assert RequestGate.query.count() == 2


def test_current_fernet_compatible_with_existing_key_derivation(app, context):
    key = app.config["POESESSID_ENCRYPTION_KEY"]
    old = Fernet(base64.urlsafe_b64encode(key[:32].encode()))
    value = "SYNTHETIC-NOT-AN-ACCOUNT-COOKIE"
    assert old.decrypt(encrypt_poesessid(value).encode()).decode() == value
    assert decrypt_poesessid(old.encrypt(value.encode()).decode()) == value


def test_fingerprint_compatible_with_old_normalized_item_rules(context):
    source = baseline_file("backend/app/scheduler.py")
    tree = ast.parse(source)
    methods = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for method in node.body:
                if isinstance(method, ast.FunctionDef) and method.name in [
                    "_collect_all_mods",
                    "_compute_fingerprint_hash",
                ]:
                    methods.append(
                        "    " + ast.get_source_segment(source, method).replace("\n", "\n    ")
                    )
    namespace = {"hashlib": hashlib, "json": json}
    # AST source segments include the original continuation indentation.
    import textwrap

    class_source = "class Legacy:\n" + "".join(
        textwrap.indent(textwrap.dedent(ast.get_source_segment(source, m)), "    ") + "\n"
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        for m in node.body
        if isinstance(m, ast.FunctionDef)
        and m.name in ["_collect_all_mods", "_compute_fingerprint_hash"]
    )
    exec(class_source, namespace)
    info = {
        "name": "测试名称",
        "typeLine": "戒指",
        "explicitMods": [{"description": "+10 力量"}, "+20 敏捷"],
        "mutatedMods": ["词缀"],
        "properties": [{"name": "品质", "values": [["20%", 1]]}],
    }
    assert fingerprint(info) == namespace["Legacy"]()._compute_fingerprint_hash(info)


@pytest.mark.parametrize("value", ["NaN", "Infinity", -1, "99999999999999.9999999", True])
def test_invalid_or_overflow_prices_rejected(value):
    with pytest.raises(ValueError):
        money(value)
