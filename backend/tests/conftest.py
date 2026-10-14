from copy import deepcopy
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

import pytest
from werkzeug.security import generate_password_hash

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
# Tests never inherit real application credentials or database settings.
os.environ.update(
    SECRET_KEY="a" * 64,
    JWT_SECRET_KEY="b" * 64,
    POESESSID_ENCRYPTION_KEY="c" * 64,
    DATABASE_URL="sqlite:///:memory:",
    LOG_DIR="",
)
from app import create_app, db
from app.models import User, MonitorTarget, UserCredential, ItemSnapshot
from app.items import item_values
from app.utils import encrypt_poesessid

PASSWORD = "synthetic-test-password"
HASH = generate_password_hash(PASSWORD)


@pytest.fixture
def app(tmp_path, monkeypatch):
    uri = os.environ.get("POE_TEST_DATABASE", "sqlite:///" + str(tmp_path / "test.sqlite"))
    if uri.startswith("mysql"):
        parsed = urlparse(uri.replace("mysql+pymysql", "mysql", 1))
        assert (
            parsed.hostname in ["127.0.0.1", "localhost"]
            and parsed.port == 13307
            and parsed.path == "/poe_test"
        ), "Refusing a non-isolated MySQL test target"
    else:
        assert uri.startswith("sqlite://"), (
            "Tests support isolated SQLite or the explicit local MySQL container only"
        )
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": uri,
            "LOG_DIR": "",
            "POE_REQUEST_INTERVAL": 0,
            "TRADE_CACHE_DIR": str(tmp_path / "trade-cache"),
        }
    )

    def no_network(*args, **kwargs):
        raise AssertionError("External HTTP is forbidden in backend tests")

    monkeypatch.setattr("requests.sessions.Session.request", no_network)
    with app.app_context():
        db.drop_all()
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def context(app):
    with app.app_context():
        yield
        db.session.remove()


@pytest.fixture
def user(context):
    value = User(username="test-user", password_hash=HASH, is_admin=False)
    db.session.add(value)
    db.session.flush()
    db.session.add(
        UserCredential(user_id=value.id, encrypted_poesessid=encrypt_poesessid("SYNTHETIC-COOKIE"))
    )
    db.session.commit()
    return value


@pytest.fixture
def headers(user):
    from flask_jwt_extended import create_access_token

    return {"Authorization": "Bearer " + create_access_token(identity=str(user.id))}


@pytest.fixture
def target(user):
    value = MonitorTarget(
        user_id=user.id,
        poe_account_name="test-account",
        league="S30赛季",
        target_type="account",
        scan_interval=300,
        enabled=True,
    )
    db.session.add(value)
    db.session.commit()
    return value


def raw_item(key="A", name="Alpha", price=10, currency="divine", frame=2, stable=None):
    info = {
        "name": name,
        "typeLine": "Synthetic Base",
        "frameType": frame,
        "explicitMods": ["synthetic mod"],
        "ilvl": 80,
    }
    if stable:
        info["id"] = stable
    return {
        "id": key,
        "item": info,
        "listing": {
            "price": {"amount": price, "currency": currency},
            "account": {"name": "synthetic-seller"},
        },
    }


def snapshot(target, raw):
    row = ItemSnapshot(target_id=target.id, **item_values(raw), item_data=deepcopy(raw))
    db.session.add(row)
    db.session.commit()
    return row


class FakeAPI:
    items = []
    ids = None
    total = None
    query_id = "new-query"
    error = None
    before_fetch = None
    closed = False

    def __init__(self, *args):
        pass

    def search_account_items(self, *args, **kwargs):
        if self.error:
            raise self.error
        ids = self.ids if self.ids is not None else [row["id"] for row in self.items if row]
        return {
            "item_ids": ids,
            "query_id": self.query_id,
            "total": len(ids) if self.total is None else self.total,
        }

    search_global_items = search_account_items

    def fetch_items_batch(self, ids, *args, **kwargs):
        if self.before_fetch:
            self.before_fetch()
        return deepcopy(self.items)

    def close(self):
        type(self).closed = True


@pytest.fixture
def fake_api():
    return type(
        "ThisTestAPI",
        (FakeAPI,),
        {
            "items": [],
            "ids": None,
            "total": None,
            "error": None,
            "before_fetch": None,
            "closed": False,
        },
    )
