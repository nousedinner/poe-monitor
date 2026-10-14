from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import requests
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func, case, and_, delete
from werkzeug.exceptions import BadRequest, NotFound
from . import db
from .models import (
    MonitorTarget,
    UserCredential,
    Event,
    User,
    ItemSnapshot,
    PriceTrend,
    ScanJob,
    DailyStats,
)
from .utils import encrypt_poesessid, decrypt_poesessid
from .items import MOD_FIELDS, validate_details, money, PRECISION
from .timeutils import utcnow, iso_utc, day_bounds, parse_utc
from .validation import body, integer, query_int, text, price
from .trade_query import normalize_query
from .poe_api import PoeAPI
from .tasks import ScanRejected

api_bp = Blueprint("api", __name__)
TRADE_CACHE_DIR = os.path.join(tempfile.gettempdir(), "poe_trade_cache")
TRADE_CACHE_TTL = 6 * 3600
_cache_locks = {name: threading.Lock() for name in ["static.json", "stats.json", "filters.json"]}


@api_bp.before_request
def validate_json_body():
    if request.method in ["POST", "PUT", "PATCH"]:
        if request.endpoint == "api.trigger_scan" and not request.get_data():
            return
        body()


def _cache_path(name):
    return Path(current_app.config.get("TRADE_CACHE_DIR", TRADE_CACHE_DIR)) / name


def _read_trade_cache(name, allow_stale=False):
    path = _cache_path(name)
    try:
        if not allow_stale and time.time() - path.stat().st_mtime > TRADE_CACHE_TTL:
            return None, None
        value = path.read_text(encoding="utf8")
        parsed = json.loads(value)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("result"), list):
            return None, None
        return value, hashlib.sha256(value.encode()).hexdigest()
    except (OSError, ValueError):
        return None, None


def _write_trade_cache(name, data):
    path = _cache_path(name)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf8", dir=path.parent, delete=False
        ) as stream:
            temporary = stream.name
            stream.write(data)
        os.replace(temporary, path)
    except OSError:
        pass
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _cached_trade_response(name, upstream_url, label):
    stale = False
    data, etag = _read_trade_cache(name)
    if data is None:
        with _cache_locks[name]:
            data, etag = _read_trade_cache(name)
            if data is None:
                try:
                    upstream = requests.get(
                        upstream_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=(5, 20)
                    )
                    upstream.raise_for_status()
                    parsed = upstream.json()
                    if not isinstance(parsed, dict) or not isinstance(parsed.get("result"), list):
                        raise ValueError("Invalid trade metadata")
                    data = json.dumps(parsed, ensure_ascii=False)
                    etag = hashlib.sha256(data.encode()).hexdigest()
                    _write_trade_cache(name, data)
                except (requests.RequestException, ValueError):
                    data, etag = _read_trade_cache(name, allow_stale=True)
                    if data is None:
                        return jsonify(msg=f"{label} 数据暂不可用"), 502
                    stale = True
    response = current_app.response_class(data, mimetype="application/json")
    response.set_etag(etag)
    response.cache_control.no_cache = True
    if stale:
        response.headers["X-Cache-Stale"] = "true"
    return response.make_conditional(request)


def _user():
    return db.session.get(User, int(get_jwt_identity()))


def _target(target_id, lock=False):
    query = MonitorTarget.query.filter_by(id=target_id, user_id=int(get_jwt_identity()))
    if lock:
        query = query.with_for_update()
    result = query.first()
    if result is None:
        raise NotFound("Target not found")
    return result


def _qp(target):
    value = target.query_params
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    return value if isinstance(value, dict) else {}


def _target_json(target):
    params = _qp(target)
    return dict(
        id=target.id,
        target_type=target.target_type,
        poe_account_name=target.poe_account_name,
        query_params=params,
        query_id=params.get("_query_id", ""),
        league=target.league,
        scan_interval=target.scan_interval,
        min_price=target.min_price,
        enabled=target.enabled,
        last_scan=iso_utc(target.last_scan),
        baseline_reset=target.baseline_reset,
    )


def _sum_when(condition, value):
    return func.sum(case((condition, value), else_=0))


@api_bp.get("/user")
@jwt_required()
def get_user_info():
    user = _user()
    return jsonify(id=user.id, username=user.username, is_admin=user.is_admin)


@api_bp.route("/targets", methods=["GET", "POST"])
@jwt_required()
def manage_targets():
    user = _user()
    if request.method == "GET":
        targets = (
            MonitorTarget.query.filter_by(user_id=user.id)
            .order_by(MonitorTarget.sort_order, MonitorTarget.id)
            .all()
        )
        if not targets:
            return jsonify([])
        ids = [t.id for t in targets]
        yesterday, today, tomorrow = day_bounds()
        today_cond = and_(Event.created_at >= today, Event.created_at < tomorrow)
        yesterday_cond = and_(Event.created_at >= yesterday, Event.created_at < today)
        columns = [
            Event.target_id,
            _sum_when(and_(today_cond, Event.event_type == "listed"), 1).label("today_new"),
            _sum_when(and_(today_cond, Event.event_type == "sold"), 1).label("today_sold_count"),
        ]
        for prefix, condition in [("today", today_cond), ("yesterday", yesterday_cond)]:
            for currency in ["divine", "chaos"]:
                columns.append(
                    _sum_when(
                        and_(
                            condition, Event.event_type == "sold", Event.price_currency == currency
                        ),
                        Event.price,
                    ).label(f"{prefix}_{currency}")
                )
        event_rows = (
            db.session.query(*columns)
            .filter(
                Event.target_id.in_(ids),
                Event.created_at >= yesterday,
                Event.created_at < tomorrow,
                Event.is_redundant.is_(False),
            )
            .group_by(Event.target_id)
            .all()
        )
        events = {row.target_id: dict(row._mapping) for row in event_rows}
        columns = [ItemSnapshot.target_id, func.count(ItemSnapshot.id).label("item_count")]
        for currency in ["divine", "chaos", "mirror"]:
            columns.append(
                _sum_when(ItemSnapshot.price_currency == currency, ItemSnapshot.price).label(
                    "current_" + currency
                )
            )
        snapshot_rows = (
            db.session.query(*columns)
            .filter(ItemSnapshot.target_id.in_(ids))
            .group_by(ItemSnapshot.target_id)
            .all()
        )
        snapshots = {row.target_id: dict(row._mapping) for row in snapshot_rows}
        output = []
        for target in targets:
            value = _target_json(target)
            for name in [
                "today_new",
                "today_sold_count",
                "today_divine",
                "today_chaos",
                "yesterday_divine",
                "yesterday_chaos",
            ]:
                value[name] = events.get(target.id, {}).get(name, 0) or 0
            for name in ["item_count", "current_divine", "current_chaos", "current_mirror"]:
                value[name] = snapshots.get(target.id, {}).get(name, 0) or 0
            output.append(value)
        return jsonify(output)
    data = body()
    kind = data.get("target_type", "account")
    if kind not in ["account", "global"]:
        raise BadRequest("target_type 必须为 account 或 global")
    account = text(data.get("poe_account_name"), "账号名") if kind == "account" else None
    params = normalize_query(data.get("query_params")) if kind == "global" else None
    minimum = 60 if user.is_admin else 300
    interval = integer(data.get("scan_interval", 300), "scan_interval", minimum, 86400)
    league = text(data.get("league", "S30赛季"), "league", 50)
    if account and MonitorTarget.query.filter_by(user_id=user.id, poe_account_name=account).first():
        raise BadRequest("Target already exists")
    target = MonitorTarget(
        user_id=user.id,
        target_type=kind,
        poe_account_name=account,
        query_params=params,
        league=league,
        scan_interval=interval,
        min_price=price(data.get("min_price")),
    )
    db.session.add(target)
    db.session.flush()
    current_app.scheduler.trigger_immediate_scan(target.id, commit=False)
    db.session.commit()
    return jsonify(id=target.id), 201


@api_bp.route("/targets/<int:target_id>", methods=["PUT", "DELETE"])
@jwt_required()
def target_detail(target_id):
    target = _target(target_id, True)
    if request.method == "DELETE":
        for model in [ScanJob, Event, ItemSnapshot, DailyStats, PriceTrend]:
            db.session.execute(delete(model).where(model.target_id == target_id))
        db.session.delete(target)
        db.session.commit()
        return jsonify(msg="Deleted")
    data = body()
    interval = integer(
        data.get("scan_interval", target.scan_interval),
        "scan_interval",
        60 if _user().is_admin else 300,
        86400,
    )
    minimum = price(data.get("min_price", target.min_price))
    params = (
        normalize_query(data["query_params"]) if "query_params" in data else target.query_params
    )
    if "enabled" in data and not isinstance(data["enabled"], bool):
        raise BadRequest("enabled 必须为布尔值")
    changed_filter = minimum != target.min_price or params != target.query_params
    changed_enabled = data.get("enabled", target.enabled) != target.enabled
    if changed_filter or changed_enabled:
        target.revision += 1
    if changed_filter:
        target.baseline_reset = True
    target.scan_interval, target.min_price, target.query_params = interval, minimum, params
    target.enabled = data.get("enabled", target.enabled)
    if target.enabled and (changed_filter or changed_enabled):
        job = db.session.get(ScanJob, target_id)
        if job:
            job.state, job.lease_token, job.lease_until = "queued", None, None
            job.next_run_at = utcnow()
        else:
            current_app.scheduler.trigger_immediate_scan(target_id, commit=False)
    db.session.commit()
    return jsonify(msg="Updated")


@api_bp.put("/targets/sort")
@jwt_required()
def update_targets_order():
    ids = body().get("order")
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) > 1000
        or any(isinstance(v, bool) or not isinstance(v, int) for v in ids)
        or len(ids) != len(set(ids))
    ):
        raise BadRequest("order 必须为不重复的目标 ID 列表")
    targets = MonitorTarget.query.filter(
        MonitorTarget.user_id == int(get_jwt_identity()), MonitorTarget.id.in_(ids)
    ).all()
    if len(targets) != len(ids):
        return jsonify(msg="Some targets not found or not owned by user"), 403
    positions = {value: index for index, value in enumerate(ids)}
    for target in targets:
        target.sort_order = positions[target.id]
    db.session.commit()
    return jsonify(msg="Order updated")


@api_bp.post("/targets/<int:target_id>/trigger")
@jwt_required()
def trigger_scan(target_id):
    _target(target_id)
    try:
        current_app.scheduler.trigger_immediate_scan(target_id, manual=True)
    except ScanRejected as exc:
        db.session.rollback()
        response = jsonify(msg=str(exc), cooldown=exc.cooldown)
        if exc.cooldown:
            response.headers["Retry-After"] = str(exc.cooldown)
        return response, exc.status
    return jsonify(msg="已触发刷新", cooldown=60)


def _parse_mods(value):
    try:
        parsed = json.loads(value) if value else []
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


@api_bp.get("/targets/<int:target_id>/snapshots")
@jwt_required()
def get_snapshots(target_id):
    target = _target(target_id)
    rows = (
        ItemSnapshot.query.filter_by(target_id=target_id)
        .order_by(ItemSnapshot.price, ItemSnapshot.id)
        .limit(query_int("limit", 20, 1, 2000))
        .all()
    )
    output = []
    for row in rows:
        mods = [mod for field in MOD_FIELDS.values() for mod in _parse_mods(getattr(row, field))]
        output.append(
            dict(
                item_id=row.item_id,
                item_name=row.item_name or "",
                base_type=row.base_type or "",
                rarity=row.rarity or "",
                ilvl=row.ilvl,
                explicit_mods=json.dumps(mods, ensure_ascii=False),
                price=row.price,
                price_currency=row.price_currency,
                seller_account=row.seller_account or "",
                item_data=row.item_data,
                scan_timestamp=iso_utc(row.scan_timestamp),
            )
        )
    response = jsonify(output)
    response.headers["X-Last-Scan"] = iso_utc(target.last_scan) or ""
    return response


@api_bp.get("/targets/<int:target_id>/sale_stats")
@jwt_required()
def get_sale_stats(target_id):
    _target(target_id)
    days = query_int("days", 1, 1, 7)
    if days not in [1, 3, 5, 7]:
        raise BadRequest("days 必须为 1/3/5/7")
    columns = [_sum_when(Event.event_type == "listed", 1), _sum_when(Event.event_type == "sold", 1)]
    columns += [
        _sum_when(and_(Event.event_type == "sold", Event.price_currency == currency), Event.price)
        for currency in ["divine", "chaos", "mirror"]
    ]
    row = (
        db.session.query(*columns)
        .filter(
            Event.target_id == target_id,
            Event.created_at >= utcnow() - timedelta(days=days),
            Event.is_redundant.is_(False),
        )
        .one()
    )
    return jsonify(
        dict(
            zip(
                ["listed", "sold", "sold_divine", "sold_chaos", "sold_mirror"],
                [v or 0 for v in row],
            ),
            days=days,
        )
    )


@api_bp.route("/credentials", methods=["GET", "POST"])
@jwt_required()
def manage_credentials():
    user_id = int(get_jwt_identity())
    credential = db.session.get(UserCredential, user_id)
    if request.method == "GET":
        return jsonify(last_updated=iso_utc(credential.last_updated) if credential else None)
    data = body()
    cookie = data.get("cookie")
    poesessid, pt = data.get("poesessid"), data.get("pt2gguin")
    if cookie:
        cookie = text(cookie, "cookie", 32768)
        match = re.search(r"(?:^|;\s*)POESESSID=([^;]+)", cookie)
        ptmatch = re.search(r"(?:^|;\s*)pt2gguin=([^;]+)", cookie)
        if match:
            poesessid = match[1].strip()
        if ptmatch:
            pt = ptmatch[1].strip()
    poesessid = text(poesessid, "poesessid", 4096)
    pt = text(pt, "pt2gguin", 4096, optional=True)
    if credential is None:
        credential = UserCredential(user_id=user_id)
        db.session.add(credential)
    credential.encrypted_poesessid = encrypt_poesessid(poesessid)
    credential.encrypted_pt2gguin = encrypt_poesessid(pt) if pt else None
    credential.last_updated = utcnow()
    db.session.commit()
    return jsonify(msg="Credentials saved")


def _event_json(event, target=None):
    value = {
        field: getattr(event, field)
        for field in [
            "id",
            "target_id",
            "item_id",
            "item_name",
            "base_type",
            "price",
            "price_old",
            "price_currency",
            "price_currency_old",
            "event_type",
            "is_redundant",
            "is_gem",
            "seller_account",
        ]
    }
    value.update({field: _parse_mods(getattr(event, field)) for field in MOD_FIELDS.values()})
    value["created_at"] = iso_utc(event.created_at)
    if target:
        value.update(target_name=target.poe_account_name, target_type=target.target_type)
    return value


@api_bp.get("/targets/<int:target_id>/events")
@jwt_required()
def get_events(target_id):
    target = _target(target_id)
    query = Event.query.filter_by(target_id=target_id)
    kind = request.args.get("event_type")
    if kind:
        if kind not in ["listed", "sold", "price_change"]:
            raise BadRequest("event_type 无效")
        query = query.filter_by(event_type=kind)
    if "is_redundant" in request.args:
        query = query.filter_by(is_redundant=bool(query_int("is_redundant", 0, 0, 1)))
    before = request.args.get("before_id")
    if before is not None:
        query = query.filter(Event.id < query_int("before_id", 0, 1, 2147483647))
    rows = (
        query.order_by(Event.created_at.desc(), Event.id.desc())
        .limit(query_int("limit", 50, 1, 2000))
        .offset(query_int("offset", 0, 0, 10000000))
        .all()
    )
    return jsonify([_event_json(row, target) for row in rows])


@api_bp.get("/events/all")
@jwt_required()
def get_all_events():
    query = (
        db.session.query(Event, MonitorTarget)
        .join(MonitorTarget)
        .filter(MonitorTarget.user_id == int(get_jwt_identity()))
    )
    for name in ["is_redundant", "is_gem"]:
        if name in request.args:
            query = query.filter(getattr(Event, name) == bool(query_int(name, 0, 0, 1)))
    if request.args.get("since"):
        try:
            query = query.filter(Event.created_at >= parse_utc(request.args["since"]))
        except ValueError as exc:
            raise BadRequest(str(exc)) from None
    total = query.count()
    rows = (
        query.order_by(Event.created_at.desc(), Event.id.desc())
        .limit(query_int("limit", 1000, 1, 2000))
        .offset(query_int("offset", 0, 0, 10000000))
        .all()
    )
    return jsonify(total=total, events=[_event_json(event, target) for event, target in rows])


@api_bp.post("/events/mark_redundant")
@jwt_required()
def mark_redundant():
    # Older open pages must also be unable to execute the destructive cleaner.
    return jsonify(msg="自动批量去重已停用，历史事件保留原样"), 410


@api_bp.get("/logs")
@jwt_required()
def get_logs():
    user = _user()
    return jsonify(
        current_app.scheduler.get_warnings(limit=100, user_id=user.id, is_admin=user.is_admin)
    )


@api_bp.get("/stats")
@jwt_required()
def get_stats():
    yesterday, today, tomorrow = day_bounds()
    columns = []
    for start, end in [(yesterday, today), (today, tomorrow)]:
        for currency in ["divine", "chaos"]:
            columns.append(
                _sum_when(
                    and_(
                        Event.created_at >= start,
                        Event.created_at < end,
                        Event.price_currency == currency,
                    ),
                    Event.price,
                )
            )
    values = (
        db.session.query(*columns)
        .join(MonitorTarget)
        .filter(
            MonitorTarget.user_id == int(get_jwt_identity()),
            Event.event_type == "sold",
            Event.is_redundant.is_(False),
            Event.created_at >= yesterday,
            Event.created_at < tomorrow,
        )
        .one()
    )
    current = (
        db.session.query(
            *[
                _sum_when(ItemSnapshot.price_currency == c, ItemSnapshot.price)
                for c in ["divine", "chaos"]
            ]
        )
        .join(MonitorTarget)
        .filter(MonitorTarget.user_id == int(get_jwt_identity()))
        .one()
    )
    return jsonify(
        dict(
            zip(
                [
                    "yesterday_divine",
                    "yesterday_chaos",
                    "today_divine",
                    "today_chaos",
                    "current_divine",
                    "current_chaos",
                ],
                [v or 0 for v in [*values, *current]],
            )
        )
    )


@api_bp.get("/targets/<int:target_id>/price_trend")
@jwt_required()
def get_price_trend(target_id):
    _target(target_id)
    days = query_int("days", 30, 1, 30)
    now = utcnow()
    cutoff = now - timedelta(days=days)
    maximum = db.session.query(func.max(PriceTrend.id)).filter_by(target_id=target_id).scalar() or 0
    start = cutoff
    cursor = query_int("cursor_id", 0, 0, 2147483647)
    if cursor:
        changed = (
            db.session.query(func.min(PriceTrend.scan_time))
            .filter(
                PriceTrend.target_id == target_id,
                PriceTrend.id > cursor,
                PriceTrend.id <= maximum,
                PriceTrend.scan_time >= cutoff,
            )
            .scalar()
        )
        if changed is None:
            return jsonify(
                cursor_id=maximum,
                window_start=iso_utc(cutoff),
                points=[],
                scan_times=[],
                hours=[],
                min_prices=[],
                top10_avg_prices=[],
                item_counts=[],
            )
        start = (
            changed
            if days == 1
            else max(cutoff, changed.replace(minute=0, second=0, microsecond=0))
        )
    elif request.args.get("since"):
        try:
            since = parse_utc(request.args["since"])
            start = max(
                cutoff, since if days == 1 else since.replace(minute=0, second=0, microsecond=0)
            )
        except ValueError as exc:
            raise BadRequest(str(exc)) from None
    rows = (
        PriceTrend.query.filter(
            PriceTrend.target_id == target_id,
            PriceTrend.id <= maximum,
            PriceTrend.scan_time >= start,
            PriceTrend.scan_time <= now,
        )
        .order_by(PriceTrend.scan_time, PriceTrend.id)
        .all()
    )
    points = []
    if days == 1:
        points = [
            dict(
                key=row.id,
                time=iso_utc(row.scan_time),
                min_price=row.min_price,
                value=row.top10_avg_price,
                count=row.item_count,
            )
            for row in rows
        ]
    else:
        hourly = defaultdict(list)
        for row in rows:
            hourly[row.scan_time.replace(minute=0, second=0, microsecond=0)].append(row)
        for hour, group in sorted(hourly.items()):

            def average(name):
                numbers = [getattr(row, name) for row in group if getattr(row, name) is not None]
                return (
                    (sum(numbers, Decimal(0)) / len(numbers)).quantize(PRECISION)
                    if numbers
                    else None
                )

            points.append(
                dict(
                    key=iso_utc(hour),
                    time=iso_utc(hour),
                    min_price=average("min_price"),
                    value=average("top10_avg_price"),
                    count=average("item_count"),
                )
            )
    return jsonify(
        cursor_id=maximum,
        window_start=iso_utc(cutoff),
        points=points,
        **(
            {"scan_times": [p["time"] for p in points]}
            if days == 1
            else {"hours": [p["time"] for p in points]}
        ),
        min_prices=[p["min_price"] for p in points],
        top10_avg_prices=[p["value"] for p in points],
        item_counts=[p["count"] for p in points],
    )


def _credentials():
    credential = db.session.get(UserCredential, int(get_jwt_identity()))
    if credential is None or not credential.encrypted_poesessid:
        raise BadRequest("请先设置 POESESSID 凭证")
    result = (
        decrypt_poesessid(credential.encrypted_poesessid),
        decrypt_poesessid(credential.encrypted_pt2gguin) if credential.encrypted_pt2gguin else None,
    )
    db.session.rollback()
    return result


def _search_query(data):
    if data.get("query") is not None:
        return normalize_query(data["query"])
    raw = {key: data[key] for key in ["type", "name", "stats"] if data.get(key)}
    values = {
        key.removeprefix("price_"): price(data[key])
        for key in ["price_min", "price_max"]
        if data.get(key) is not None
    }
    if values:
        raw["filters"] = {
            "trade_filters": {
                "filters": {"price": {key: float(value) for key, value in values.items()}}
            }
        }
    if data.get("online_only"):
        raw["status"] = {"option": "online"}
    return normalize_query(raw)


@api_bp.post("/search")
@jwt_required()
def global_search():
    data = body()
    page = integer(data.get("page", 0), "page", 0, 4)
    league = text(data.get("league", "S30赛季"), "league", 50)
    query = _search_query(data)
    api = PoeAPI(*_credentials())
    api.deadline = time.monotonic() + 90
    try:
        result = api.search_global_items(league, query)
        ids = result["item_ids"][page * 20 : (page + 1) * 20]
        items = api.fetch_items_batch(ids, result["query_id"], batch_size=10) if ids else []
        return jsonify(
            total=result["total"],
            accessible_total=len(result["item_ids"]),
            items=items,
            page=page,
            per_page=20,
            query_id=result["query_id"],
        )
    except Exception:
        current_app.logger.warning("Upstream search failed", exc_info=True)
        return jsonify(msg="搜索失败，请检查筛选条件或凭证后重试"), 502
    finally:
        api.close()


@api_bp.post("/search/monitor")
@jwt_required()
def create_global_monitor():
    data = body()
    minimum = 60 if _user().is_admin else 300
    interval = integer(data.get("scan_interval", 300), "scan_interval", minimum, 86400)
    league = text(data.get("league", "S30赛季"), "league", 50)
    if not data.get("query"):
        raise BadRequest("缺少筛选条件 query")
    query = normalize_query(data["query"])
    api = PoeAPI(*_credentials())
    api.deadline = time.monotonic() + 90
    try:
        result = api.search_global_items(league, query)
        if not result["item_ids"] or result["total"] == 0:
            return jsonify(msg="未找到匹配物品，请调整筛选条件", total=0), 400
        ids = result["item_ids"][:10]
        preview = api.fetch_items_batch(ids, result["query_id"], batch_size=10)
        validate_details(ids, preview)
    except Exception:
        current_app.logger.warning("Monitor preview failed", exc_info=True)
        return jsonify(msg="创建监控失败，请稍后重试"), 502
    finally:
        api.close()
    target = MonitorTarget(
        user_id=int(get_jwt_identity()),
        poe_account_name=None,
        target_type="global",
        league=league,
        scan_interval=interval,
        query_params={**query, "_query_id": result["query_id"]},
    )
    db.session.add(target)
    db.session.flush()
    current_app.scheduler.trigger_immediate_scan(target.id, commit=False)
    db.session.commit()
    prices = [
        money(row["listing"]["price"]["amount"])
        for row in preview
        if row["listing"]["price"]["currency"] == "divine"
    ]
    return jsonify(
        id=target.id,
        query_id=result["query_id"],
        total=result["total"],
        preview_items=preview,
        top10_avg=(sum(prices, Decimal(0)) / len(prices)).quantize(PRECISION) if prices else 0,
        msg="监控任务创建成功",
    ), 201


@api_bp.get("/trade/static")
def trade_static():
    return _cached_trade_response(
        "static.json", "https://poe.game.qq.com/api/trade/data/static", "static"
    )


@api_bp.get("/trade/stats")
def trade_stats():
    return _cached_trade_response(
        "stats.json", "https://poe.game.qq.com/api/trade/data/stats", "stats"
    )


@api_bp.get("/trade/filters")
def trade_filters():
    return _cached_trade_response(
        "filters.json", "https://poe.game.qq.com/api/trade/data/filters", "filters"
    )
