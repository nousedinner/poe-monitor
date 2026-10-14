"""Local browser fixture: real auth/API/DB, synthetic upstream, no production I/O.

Run from repo root: python backend/tests/browser_server.py
"""

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "output" / "playwright"
OUTPUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "backend"))
os.environ.update(
    SECRET_KEY="a" * 64,
    JWT_SECRET_KEY="b" * 64,
    POESESSID_ENCRYPTION_KEY="c" * 64,
    DATABASE_URL="sqlite:///" + str(OUTPUT / "browser.sqlite"),
    LOG_DIR="",
)
from flask import send_from_directory
from werkzeug.security import generate_password_hash
from waitress import serve
from app import create_app, db
from app.models import User, UserCredential, MonitorTarget, Event, PriceTrend, ItemSnapshot
from app.schema import upgrade
from app.utils import encrypt_poesessid
from app.timeutils import utcnow
from app.items import item_values
import app.api as routes

app = create_app({"POE_REQUEST_INTERVAL": 0})
REQUESTS = []


class FakeAPI:
    def __init__(self, *args):
        pass

    def search_global_items(self, *args, **kwargs):
        return {"item_ids": ["A", "B"], "query_id": "fixture-query", "total": 200}

    search_account_items = search_global_items

    def fetch_items_batch(self, ids, *args, **kwargs):
        return [
            dict(
                id=key,
                item={
                    "name": "测试物品 " + key,
                    "typeLine": "测试基底",
                    "frameType": 2,
                    "explicitMods": ["+10 力量"],
                },
                listing={
                    "price": {"amount": 10.125 if key == "A" else 20, "currency": "divine"},
                    "account": {"name": "fixture-seller"},
                },
            )
            for key in ids
        ]

    def close(self):
        pass


routes.PoeAPI = FakeAPI


class MetadataResponse:
    def __init__(self, path):
        self.path = path

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.path.read_text(encoding="utf8"))


def static_metadata(url, **kwargs):
    name = url.rsplit("/", 1)[-1] + ".json"
    path = (
        (ROOT / "frontend" / name) if name == "filters.json" else (ROOT / "backend" / "data" / name)
    )
    return MetadataResponse(path)


routes.requests.get = static_metadata


@app.before_request
def record():
    from flask import request

    REQUESTS.append((request.method, request.path))


@app.get("/_fixture/requests")
def requests_seen():
    return {"requests": REQUESTS}


@app.get("/")
def homepage():
    return send_from_directory(ROOT / "frontend", "index.html")


@app.get("/<path:name>")
def asset(name):
    return send_from_directory(ROOT / "frontend", name)


with app.app_context():
    db.drop_all()
    upgrade()
    user = User(
        username="browser-user",
        password_hash=generate_password_hash("browser-test-password"),
        is_admin=True,
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(
        UserCredential(user_id=user.id, encrypted_poesessid=encrypt_poesessid("SYNTHETIC-ONLY"))
    )
    account = MonitorTarget(
        user_id=user.id,
        poe_account_name="测试账号",
        league="S30赛季",
        scan_interval=300,
        target_type="account",
    )
    global_target = MonitorTarget(
        user_id=user.id,
        league="S30赛季",
        scan_interval=300,
        target_type="global",
        query_params={"type": "Sword", "_query_id": "fixture-query"},
    )
    db.session.add_all([account, global_target])
    db.session.commit()
    for target in [account, global_target]:
        for raw in FakeAPI().fetch_items_batch(["A", "B"]):
            db.session.add(ItemSnapshot(target_id=target.id, **item_values(raw), item_data=raw))
            db.session.add(Event(target_id=target.id, **item_values(raw), event_type="listed"))
        target.last_scan = utcnow()
    db.session.add(
        PriceTrend(
            target_id=global_target.id, min_price=10.125, top10_avg_price=15.0625, item_count=2
        )
    )
    db.session.commit()

print("Local browser fixture at http://127.0.0.1:15080", flush=True)
serve(app, host="127.0.0.1", port=15080, threads=8)
