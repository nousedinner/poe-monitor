from .timeutils import utcnow
from sqlalchemy.dialects.mysql import DATETIME
from . import db

UTCDateTime = db.DateTime().with_variant(DATETIME(fsp=6), "mysql")


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)  # 新增
    created_at = db.Column(UTCDateTime, default=utcnow)

    credentials = db.relationship(
        "UserCredential", backref="user", uselist=False, cascade="all, delete-orphan"
    )
    targets = db.relationship(
        "MonitorTarget", backref="user", lazy="dynamic", cascade="all, delete-orphan"
    )


class UserCredential(db.Model):
    __tablename__ = "user_credentials"
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    encrypted_poesessid = db.Column(db.Text, nullable=False)
    encrypted_pt2gguin = db.Column(db.Text, nullable=True)
    last_updated = db.Column(UTCDateTime, default=utcnow, onupdate=utcnow)


class MonitorTarget(db.Model):
    __tablename__ = "monitor_targets"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    poe_account_name = db.Column(db.String(100), nullable=True)  # 全服监控时为 NULL
    league = db.Column(db.String(50), nullable=False, default="S30赛季")
    scan_interval = db.Column(db.Integer, default=120)
    min_price = db.Column(db.Numeric(20, 6), nullable=True)
    enabled = db.Column(db.Boolean, default=True)  # 新增
    sort_order = db.Column(db.Integer, default=0, nullable=False)  # 新增排序字段
    target_type = db.Column(
        db.String(20), nullable=False, default="account"
    )  # 'account' or 'global'
    query_params = db.Column(db.JSON, nullable=True)  # 全服筛选条件
    last_scan = db.Column(UTCDateTime, nullable=True)
    revision = db.Column(db.Integer, nullable=False, default=0)
    baseline_reset = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(UTCDateTime, default=utcnow)
    updated_at = db.Column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        db.UniqueConstraint("user_id", "poe_account_name", name="unique_user_target"),
    )


class ItemSnapshot(db.Model):
    __tablename__ = "item_snapshots"
    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(
        db.Integer, db.ForeignKey("monitor_targets.id", ondelete="CASCADE"), nullable=False
    )
    item_id = db.Column(db.String(255), nullable=False)
    item_name = db.Column(db.String(255))
    base_type = db.Column(db.String(255))
    rarity = db.Column(db.String(50))
    ilvl = db.Column(db.Integer)
    explicit_mods = db.Column(db.Text)  # 显式词缀
    implicit_mods = db.Column(db.Text)  # 隐式词缀
    fractured_mods = db.Column(db.Text)  # 裂痕词缀
    crafted_mods = db.Column(db.Text)  # 工艺词缀
    enchant_mods = db.Column(db.Text)  # 附魔词缀
    mutated_mods = db.Column(db.Text)  # 秽生词缀
    veiled_mods = db.Column(db.Text)  # 被匿名单词缀
    price = db.Column(db.Numeric(20, 6), nullable=False)
    price_currency = db.Column(db.String(20), default="divine")
    item_data = db.Column(db.JSON, nullable=False)
    seller_account = db.Column(db.String(100), nullable=True)  # 全服监控记录卖家
    scan_timestamp = db.Column(UTCDateTime, default=utcnow)

    target = db.relationship("MonitorTarget", backref="snapshots")

    __table_args__ = (db.UniqueConstraint("target_id", "item_id", name="unique_target_item"),)


class Event(db.Model):
    __tablename__ = "events"
    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(
        db.Integer, db.ForeignKey("monitor_targets.id", ondelete="CASCADE"), nullable=False
    )
    item_id = db.Column(db.String(255), nullable=False)
    item_name = db.Column(db.String(255))
    base_type = db.Column(db.String(255))
    explicit_mods = db.Column(db.Text)  # 显式词缀
    implicit_mods = db.Column(db.Text)  # 隐式词缀
    fractured_mods = db.Column(db.Text)  # 裂痕词缀
    crafted_mods = db.Column(db.Text)  # 工艺词缀
    enchant_mods = db.Column(db.Text)  # 附魔词缀
    mutated_mods = db.Column(db.Text)  # 秽生词缀
    veiled_mods = db.Column(db.Text)  # 被匿名单词缀
    price = db.Column(db.Numeric(20, 6), nullable=False)
    price_old = db.Column(db.Numeric(20, 6), nullable=True)
    price_currency = db.Column(db.String(20), default="divine")
    price_currency_old = db.Column(db.String(20), nullable=True)
    event_type = db.Column(db.Enum("listed", "sold", "price_change"), nullable=False)
    is_redundant = db.Column(db.Boolean, default=False)  # 新增
    is_gem = db.Column(db.Boolean, default=False)  # 新增
    fingerprint_hash = db.Column(db.String(255), index=True, nullable=True)  # 新增
    seller_account = db.Column(db.String(100), nullable=True)  # 全服监控记录卖家
    created_at = db.Column(UTCDateTime, default=utcnow)

    target = db.relationship("MonitorTarget", backref="events")

    __table_args__ = (
        db.Index("ix_events_target_created_id", "target_id", "created_at", "id"),
        db.Index("ix_events_created_at", "created_at"),
    )


class DailyStats(db.Model):
    __tablename__ = "daily_stats"
    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(
        db.Integer, db.ForeignKey("monitor_targets.id", ondelete="CASCADE"), nullable=False
    )
    date = db.Column(db.Date, nullable=False)
    total_listed_price = db.Column(db.Integer, default=0)
    total_sold_price = db.Column(db.Integer, default=0)

    target = db.relationship("MonitorTarget", backref="daily_stats")
    __table_args__ = (db.UniqueConstraint("target_id", "date", name="unique_target_date"),)


class PriceTrend(db.Model):
    __tablename__ = "price_trend"
    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(
        db.Integer, db.ForeignKey("monitor_targets.id", ondelete="CASCADE"), nullable=False
    )
    scan_time = db.Column(UTCDateTime, default=utcnow)
    min_price = db.Column(db.Numeric(20, 6))
    top10_avg_price = db.Column(db.Numeric(20, 6))
    item_count = db.Column(db.Integer)

    target = db.relationship("MonitorTarget", backref="price_trends")

    __table_args__ = (
        db.Index("ix_trend_target_time", "target_id", "scan_time"),
        db.Index("ix_trend_scan_time", "scan_time"),
    )


class ScanJob(db.Model):
    """One durable pending/running job per target, shared by API and worker."""

    __tablename__ = "scan_jobs"
    target_id = db.Column(
        db.Integer, db.ForeignKey("monitor_targets.id", ondelete="CASCADE"), primary_key=True
    )
    state = db.Column(db.String(16), nullable=False, default="queued")
    next_run_at = db.Column(UTCDateTime, nullable=False, default=utcnow, index=True)
    lease_token = db.Column(db.String(36))
    lease_until = db.Column(db.DateTime)
    last_manual_at = db.Column(db.DateTime)


class WorkerLease(db.Model):
    __tablename__ = "worker_lease"
    id = db.Column(db.Integer, primary_key=True)
    owner = db.Column(db.String(36), nullable=False)
    heartbeat_at = db.Column(UTCDateTime, nullable=False)
    expires_at = db.Column(UTCDateTime, nullable=False)


class MonitorWarning(db.Model):
    __tablename__ = "monitor_warnings"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    target_id = db.Column(db.Integer, nullable=True)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class RequestGate(db.Model):
    __tablename__ = "request_gates"
    scope = db.Column(db.String(80), primary_key=True)
    next_allowed_at = db.Column(UTCDateTime, nullable=False, default=utcnow)


class RevokedToken(db.Model):
    __tablename__ = "revoked_tokens"
    jti = db.Column(db.String(64), primary_key=True)
    expires_at = db.Column(UTCDateTime, nullable=False, index=True)


class LoginAttempt(db.Model):
    __tablename__ = "login_attempts"
    key = db.Column(db.String(64), primary_key=True)
    failures = db.Column(db.Integer, nullable=False, default=0)
    window_start = db.Column(UTCDateTime, nullable=False, default=utcnow)
