"""Durable scan dispatch and shared request pacing. All times are UTC."""

from datetime import timedelta
import hashlib
import time
import uuid
from flask import current_app
from sqlalchemy import select, update, or_
from sqlalchemy.exc import IntegrityError
from . import db
from .models import MonitorTarget, ScanJob, MonitorWarning, RequestGate, WorkerLease
from .timeutils import utcnow, iso_utc


class ScanRejected(ValueError):
    def __init__(self, message, status=429, cooldown=60):
        super().__init__(message)
        self.status, self.cooldown = status, cooldown


class ScanDispatcher:
    def __init__(self, app=None):
        self.app = app

    def trigger_immediate_scan(self, target_id, manual=False, commit=True):
        now = utcnow()
        target = db.session.get(
            MonitorTarget, target_id, with_for_update=True, populate_existing=True
        )
        if target is None or not target.enabled:
            raise ScanRejected("监控目标不存在或已停用", 400, 0)
        job = db.session.get(ScanJob, target_id, with_for_update=True, populate_existing=True)
        if job and manual:
            if job.state == "running" and job.lease_until and job.lease_until > now:
                raise ScanRejected("扫描正在执行，请稍后刷新")
            if job.state == "queued":
                raise ScanRejected("扫描已在队列中，请勿重复刷新")
            if job.last_manual_at and (now - job.last_manual_at).total_seconds() < 60:
                raise ScanRejected(
                    "请等待后再刷新",
                    cooldown=max(1, int(60 - (now - job.last_manual_at).total_seconds())),
                )
        if job is None:
            job = ScanJob(target_id=target_id)
            db.session.add(job)
        elif job.state == "running" and job.lease_until and job.lease_until > now:
            return False
        job.state = "queued"
        job.next_run_at = now
        job.lease_token = None
        job.lease_until = None
        if manual:
            job.last_manual_at = now
        if commit:
            db.session.commit()
        return True

    def get_warnings(self, limit=100, user_id=None, is_admin=False):
        query = MonitorWarning.query
        if not is_admin:
            query = query.filter(MonitorWarning.user_id == user_id)
        rows = query.order_by(MonitorWarning.id.desc()).limit(min(limit, 100)).all()
        return [f"{iso_utc(row.created_at)} - {row.message}" for row in rows]

    def add_warning(self, message, user_id=None, target_id=None):
        # Never call this with raw request headers or credential values.
        db.session.add(
            MonitorWarning(user_id=user_id, target_id=target_id, message=str(message)[:1000])
        )
        db.session.flush()
        cutoff = (
            db.session.query(MonitorWarning.id)
            .filter(MonitorWarning.user_id == user_id)
            .order_by(MonitorWarning.id.desc())
            .offset(100)
            .limit(1)
            .scalar()
        )
        if cutoff is not None:
            MonitorWarning.query.filter(
                MonitorWarning.user_id == user_id, MonitorWarning.id <= cutoff
            ).delete(synchronize_session=False)
        db.session.commit()


class RequestPacer:
    """One shared DB slot per request, including searches in API processes.

    No cookie is persisted here. Dynamic 429 delays also affect other processes.
    """

    def __init__(self, cookie):
        self.scopes = ["poe-origin", "credential-" + hashlib.sha256(cookie.encode()).hexdigest()]

    def wait(self, deadline, cancel=None):
        interval = current_app.config["POE_REQUEST_INTERVAL"]
        if interval <= 0:
            return
        while True:
            if (cancel and cancel.is_set()) or time.monotonic() >= deadline:
                raise TimeoutError("请求已取消或超过总时间预算")
            delay = 0
            with db.engine.begin() as conn:
                for scope in self.scopes:
                    try:
                        with conn.begin_nested():
                            conn.execute(
                                RequestGate.__table__.insert().values(
                                    scope=scope, next_allowed_at=utcnow()
                                )
                            )
                    except IntegrityError:
                        pass
                rows = conn.execute(
                    select(RequestGate.scope, RequestGate.next_allowed_at)
                    .where(RequestGate.scope.in_(self.scopes))
                    .order_by(RequestGate.scope)
                    .with_for_update()
                ).all()
                now = utcnow()
                delay = max(0, max((row.next_allowed_at - now).total_seconds() for row in rows))
                if delay <= 0:
                    conn.execute(
                        update(RequestGate)
                        .where(RequestGate.scope.in_(self.scopes))
                        .values(next_allowed_at=now + timedelta(seconds=interval))
                    )
                    return
            delay = min(delay, max(0, deadline - time.monotonic()), 1)
            if cancel:
                cancel.wait(delay)
            else:
                time.sleep(delay)

    def postpone(self, seconds):
        until = utcnow() + timedelta(seconds=max(0, min(seconds, 600)))
        with db.engine.begin() as conn:
            conn.execute(
                update(RequestGate)
                .where(RequestGate.scope.in_(self.scopes), RequestGate.next_allowed_at < until)
                .values(next_allowed_at=until)
            )


class SingletonLease:
    def __init__(self):
        self.owner = str(uuid.uuid4())

    def renew(self):
        now = utcnow()
        with db.engine.begin() as conn:
            changed = conn.execute(
                update(WorkerLease)
                .where(
                    WorkerLease.id == 1,
                    or_(WorkerLease.expires_at <= now, WorkerLease.owner == self.owner),
                )
                .values(owner=self.owner, heartbeat_at=now, expires_at=now + timedelta(seconds=45))
            ).rowcount
            if changed:
                return True
            try:
                with conn.begin_nested():
                    conn.execute(
                        WorkerLease.__table__.insert().values(
                            id=1,
                            owner=self.owner,
                            heartbeat_at=now,
                            expires_at=now + timedelta(seconds=45),
                        )
                    )
                return True
            except IntegrityError:
                return False

    def release(self):
        with db.engine.begin() as conn:
            conn.execute(
                update(WorkerLease)
                .where(WorkerLease.id == 1, WorkerLease.owner == self.owner)
                .values(expires_at=utcnow())
            )
