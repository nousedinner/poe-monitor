from datetime import datetime
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import event
from flask_jwt_extended import create_access_token
from app import db
from app.models import User, MonitorTarget, Event, ItemSnapshot, ScanJob, PriceTrend, RevokedToken
from app.tasks import ScanDispatcher
from app.timeutils import utcnow
import app.api as api_module
from conftest import raw_item, HASH, PASSWORD


def test_target_crud_validation_and_atomic_enqueue(app, client, user, headers):
    response = client.post(
        "/api/targets", headers=headers, json={"poe_account_name": "Account", "scan_interval": 300}
    )
    assert response.status_code == 201
    tid = response.json["id"]
    assert db.session.get(ScanJob, tid).state == "queued"
    assert (
        client.put(f"/api/targets/{tid}", headers=headers, json={"enabled": "false"}).status_code
        == 400
    )
    assert (
        client.put(
            f"/api/targets/{tid}", headers=headers, json={"enabled": False, "min_price": 1.25}
        ).status_code
        == 200
    )
    target = db.session.get(MonitorTarget, tid)
    assert not target.enabled and target.min_price == Decimal("1.25") and target.baseline_reset
    assert client.post(f"/api/targets/{tid}/trigger", headers=headers, json={}).status_code == 400
    assert client.delete(f"/api/targets/{tid}", headers=headers).status_code == 200
    assert db.session.get(ScanJob, tid) is None


@pytest.mark.parametrize(
    "path,method,value",
    [
        ("/api/targets", "post", []),
        ("/api/targets", "post", None),
        ("/api/targets", "post", {"poe_account_name": "A", "scan_interval": "300"}),
        ("/api/targets", "post", {"poe_account_name": "A", "scan_interval": True}),
        ("/api/targets", "post", {"poe_account_name": "A", "min_price": -1}),
        ("/api/targets", "post", {"poe_account_name": "A", "min_price": "NaN"}),
        ("/api/targets/sort", "put", {"order": [1, 1]}),
        ("/api/search", "post", {"page": 5, "query": {"type": "Sword"}}),
        ("/api/search", "post", {"page": -1, "query": {"type": "Sword"}}),
        ("/api/credentials", "post", {"poesessid": 123}),
    ],
)
def test_malformed_requests_are_400(client, headers, path, method, value):
    assert getattr(client, method)(path, headers=headers, json=value).status_code == 400


def test_manual_enqueue_has_cooldown_without_successful_scan(app, client, target, headers):
    path = f"/api/targets/{target.id}/trigger"
    responses = [client.post(path, headers=headers, json={}) for _ in range(20)]
    assert [r.status_code for r in responses] == [200] + [429] * 19
    assert ScanJob.query.count() == 1
    job = ScanJob.query.one()
    job.state = "idle"
    db.session.commit()
    assert client.post(path, headers=headers, json={}).status_code == 429
    assert db.session.get(MonitorTarget, target.id).last_scan is None


def test_trigger_accepts_existing_browser_empty_post(client, target, headers):
    assert client.post(f"/api/targets/{target.id}/trigger", headers=headers).status_code == 200


def test_latest_warnings_and_user_isolation(app, client, user, headers):
    other = User(username="other", password_hash=HASH)
    db.session.add(other)
    db.session.commit()
    dispatcher = ScanDispatcher(app)
    for i in range(105):
        dispatcher.add_warning(f"warning-{i}", user.id)
    dispatcher.add_warning("other-private-account", other.id)
    data = client.get("/api/logs", headers=headers).json
    assert (
        len(data) == 100
        and data[0].endswith("warning-104")
        and not any("warning-0" == v.split(" - ")[-1] for v in data)
    )
    assert not any("other-private" in v for v in data)


def test_old_client_cleaner_cannot_rewrite_history(client, target, headers):
    row = Event(target_id=target.id, item_id="A", price=10, event_type="sold")
    db.session.add(row)
    db.session.commit()
    assert (
        client.post(
            "/api/events/mark_redundant", headers=headers, json={"event_ids": [row.id]}
        ).status_code
        == 410
    )
    assert not db.session.get(Event, row.id).is_redundant


def test_owned_routes_are_not_readable_or_mutable_by_other_user(app, client, target):
    other = User(username="other", password_hash=HASH)
    db.session.add(other)
    db.session.commit()
    headers = {"Authorization": "Bearer " + create_access_token(identity=str(other.id))}
    for method, suffix in [
        ("get", "/snapshots"),
        ("get", "/events"),
        ("get", "/sale_stats"),
        ("get", "/price_trend"),
        ("post", "/trigger"),
        ("put", ""),
        ("delete", ""),
    ]:
        assert (
            getattr(client, method)(
                f"/api/targets/{target.id}" + suffix,
                headers=headers,
                json={} if method in ["put", "post"] else None,
            ).status_code
            == 404
        )
    assert client.get("/api/targets", headers=headers).json == []


def test_target_aggregate_queries_are_constant_and_totals_correct(app, client, user, headers):
    now = utcnow()
    for n in range(20):
        t = MonitorTarget(
            user_id=user.id, poe_account_name=f"account-{n}", league="S30赛季", scan_interval=300
        )
        db.session.add(t)
        db.session.flush()
        db.session.add(
            Event(
                target_id=t.id,
                item_id=str(n),
                event_type="sold",
                price=Decimal("1.125"),
                price_currency="divine",
                created_at=now,
            )
        )
        db.session.add(
            ItemSnapshot(
                target_id=t.id,
                **__import__("app.items", fromlist=["item_values"]).item_values(raw_item()),
                item_data=raw_item(),
            )
        )
    db.session.commit()
    statements = []

    def count(*args):
        statements.append(args[2])

    event.listen(db.engine, "before_cursor_execute", count)
    try:
        response = client.get("/api/targets", headers=headers)
    finally:
        event.remove(db.engine, "before_cursor_execute", count)
    assert response.status_code == 200 and len(response.json) == 20
    assert all(
        row["today_divine"] == 1.125 and row["current_divine"] == 10 for row in response.json
    )
    assert len(statements) <= 5


def test_beijing_calendar_stats(app, client, target, headers, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "day_bounds",
        lambda: __import__("app.timeutils", fromlist=["day_bounds"]).day_bounds(
            datetime(2026, 10, 14, 17)
        ),
    )
    for when, amount in [(datetime(2026, 10, 14, 1), 10), (datetime(2026, 10, 14, 16, 30), 20)]:
        db.session.add(
            Event(
                target_id=target.id,
                item_id=str(amount),
                price=amount,
                price_currency="divine",
                event_type="sold",
                created_at=when,
            )
        )
    db.session.commit()
    data = client.get("/api/stats", headers=headers).json
    assert data["today_divine"] == 20 and data["yesterday_divine"] == 10
    row = client.get("/api/targets", headers=headers).json[0]
    assert row["today_divine"] == 20 and row["yesterday_divine"] == 10


@pytest.mark.parametrize(
    "suffix",
    [
        "/events?limit=-1",
        "/events?limit=10000000",
        "/snapshots?limit=0",
        "/price_trend?days=100000",
        "/price_trend?since=invalid",
        "/events?is_redundant=3",
    ],
)
def test_invalid_query_bounds(client, target, headers, suffix):
    assert client.get(f"/api/targets/{target.id}" + suffix, headers=headers).status_code == 400


def test_query_normalization_shared_by_search_and_monitor(
    app, client, headers, fake_api, monkeypatch
):
    received = []

    def search(self, league, query):
        received.append(query)
        return {"item_ids": ["A"], "query_id": "q", "total": 1}

    fake_api.search_global_items = search
    fake_api.items = [raw_item()]
    monkeypatch.setattr(api_module, "PoeAPI", fake_api)
    raw = {
        "type": {"option": "Gem", "discriminator": "variant"},
        "filters": {
            "trade_filters": {
                "disabled": False,
                "filters": {"price": {"option": "divine", "value": {"min": 1.5}}},
            }
        },
    }
    assert client.post("/api/search", headers=headers, json={"query": raw}).status_code == 200
    assert (
        client.post("/api/search/monitor", headers=headers, json={"query": raw}).status_code == 201
    )
    assert received[0] == received[1]
    assert received[0]["type"]["discriminator"] == "variant"
    assert received[0]["filters"]["trade_filters"]["filters"]["price"]["min"] == 1.5
    assert ScanJob.query.count() == 1


def test_trend_cursor_replaces_hour_bucket_and_keeps_late_insert(
    app, client, target, headers, monkeypatch
):
    now = datetime(2026, 10, 15, 10, 40)
    monkeypatch.setattr(api_module, "utcnow", lambda: now)
    db.session.add(
        PriceTrend(
            target_id=target.id,
            scan_time=now.replace(minute=0),
            min_price=10,
            top10_avg_price=10,
            item_count=1,
        )
    )
    db.session.commit()
    first = client.get(f"/api/targets/{target.id}/price_trend?days=7", headers=headers).json
    assert len(first["points"]) == 1 and first["points"][0]["value"] == 10
    # Insert after response generation with a scan timestamp before client wall time.
    db.session.add(
        PriceTrend(
            target_id=target.id,
            scan_time=now.replace(minute=30),
            min_price=30,
            top10_avg_price=30,
            item_count=1,
        )
    )
    db.session.commit()
    second = client.get(
        f"/api/targets/{target.id}/price_trend?days=7&cursor_id=" + str(first["cursor_id"]),
        headers=headers,
    ).json
    assert (
        second["points"][0]["key"] == first["points"][0]["key"]
        and second["points"][0]["value"] == 20
    )
    assert second["cursor_id"] > first["cursor_id"]
    third = client.get(
        f"/api/targets/{target.id}/price_trend?days=7&cursor_id=" + str(second["cursor_id"]),
        headers=headers,
    ).json
    assert third["points"] == [] and third["window_start"].endswith("Z")


def test_snapshot_timestamp_is_server_observation(client, target, headers):
    target.last_scan = datetime(2026, 10, 1, 4)
    db.session.commit()
    response = client.get(f"/api/targets/{target.id}/snapshots", headers=headers)
    assert response.headers["X-Last-Scan"] == "2026-10-01T04:00:00.000000Z"


def test_login_logout_revokes_token_and_password_spaces_survive(app, client, user):
    response = client.post("/auth/login", json={"username": user.username, "password": PASSWORD})
    assert response.status_code == 200
    headers = {"Authorization": "Bearer " + response.json["access_token"]}
    assert client.get("/api/user", headers=headers).status_code == 200
    assert client.post("/auth/logout", headers=headers, json={}).status_code == 200
    assert client.get("/api/user", headers=headers).status_code == 401
    assert RevokedToken.query.count() == 1


def test_login_rate_limit_and_malformed_json(client, user):
    assert client.post("/auth/login", json=[]).status_code == 400
    for _ in range(5):
        assert (
            client.post(
                "/auth/login", json={"username": user.username, "password": "wrong"}
            ).status_code
            == 401
        )
    response = client.post("/auth/login", json={"username": user.username, "password": PASSWORD})
    assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0


def test_authenticated_responses_not_shared_cache(client, headers):
    response = client.get("/api/user", headers=headers)
    assert response.headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in response.headers


def test_mysql_manual_enqueue_is_atomic_under_concurrency(app, target, headers):
    if db.engine.dialect.name != "mysql":
        pytest.skip("Real row-lock test runs in the isolated MySQL pass")
    tid = target.id
    # Release the fixture's connection before concurrent clients run.
    db.session.remove()

    def post(_):
        with app.test_client() as client:
            return client.post(f"/api/targets/{tid}/trigger", headers=headers, json={}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(post, range(20)))
    assert statuses.count(200) == 1 and statuses.count(429) == 19
