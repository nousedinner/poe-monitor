from datetime import datetime
from . import db

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)   # 新增
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    credentials = db.relationship('UserCredential', backref='user', uselist=False, cascade='all, delete-orphan')
    targets = db.relationship('MonitorTarget', backref='user', lazy='dynamic', cascade='all, delete-orphan')

class UserCredential(db.Model):
    __tablename__ = 'user_credentials'
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    encrypted_poesessid = db.Column(db.Text, nullable=False)
    encrypted_pt2gguin = db.Column(db.Text, nullable=True)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class MonitorTarget(db.Model):
    __tablename__ = 'monitor_targets'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    poe_account_name = db.Column(db.String(100), nullable=True)  # 全服监控时为 NULL
    league = db.Column(db.String(50), nullable=False, default='S29赛季')
    scan_interval = db.Column(db.Integer, default=120)
    min_price = db.Column(db.Integer, nullable=True)
    enabled = db.Column(db.Boolean, default=True)  # 新增
    sort_order = db.Column(db.Integer, default=0, nullable=False)  # 新增排序字段
    target_type = db.Column(db.String(20), nullable=False, default='account')  # 'account' or 'global'
    query_params = db.Column(db.JSON, nullable=True)  # 全服筛选条件
    last_scan = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'poe_account_name', name='unique_user_target'),)

class ItemSnapshot(db.Model):
    __tablename__ = 'item_snapshots'
    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(db.Integer, db.ForeignKey('monitor_targets.id', ondelete='CASCADE'), nullable=False)
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
    price = db.Column(db.Integer, nullable=False)
    price_currency = db.Column(db.String(20), default='divine')
    item_data = db.Column(db.JSON, nullable=False)
    seller_account = db.Column(db.String(100), nullable=True)  # 全服监控记录卖家
    scan_timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    target = db.relationship('MonitorTarget', backref='snapshots')

class Event(db.Model):
    __tablename__ = 'events'
    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(db.Integer, db.ForeignKey('monitor_targets.id', ondelete='CASCADE'), nullable=False)
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
    price = db.Column(db.Integer, nullable=False)
    price_old = db.Column(db.Integer, nullable=True)
    price_currency = db.Column(db.String(20), default='divine')
    event_type = db.Column(db.Enum('listed', 'sold', 'price_change'), nullable=False)
    is_redundant = db.Column(db.Boolean, default=False)   # 新增
    is_gem = db.Column(db.Boolean, default=False)         # 新增
    fingerprint_hash = db.Column(db.String(255), index=True, nullable=True)  # 新增
    seller_account = db.Column(db.String(100), nullable=True)  # 全服监控记录卖家
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    target = db.relationship('MonitorTarget', backref='events')

class DailyStats(db.Model):
    __tablename__ = 'daily_stats'
    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(db.Integer, db.ForeignKey('monitor_targets.id', ondelete='CASCADE'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    total_listed_price = db.Column(db.Integer, default=0)
    total_sold_price = db.Column(db.Integer, default=0)

    target = db.relationship('MonitorTarget', backref='daily_stats')
    __table_args__ = (db.UniqueConstraint('target_id', 'date', name='unique_target_date'),)

class PriceTrend(db.Model):
    __tablename__ = 'price_trend'
    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(db.Integer, db.ForeignKey('monitor_targets.id', ondelete='CASCADE'), nullable=False)
    scan_time = db.Column(db.DateTime, default=datetime.utcnow)
    min_price = db.Column(db.Integer)  # 保持 Integer 与现有 price 列一致
    top10_avg_price = db.Column(db.Integer)
    item_count = db.Column(db.Integer)

    target = db.relationship('MonitorTarget', backref='price_trends')