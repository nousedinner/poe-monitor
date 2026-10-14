"""Dedicated bounded worker; importing the API never starts these threads."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import logging
import threading
import uuid
from sqlalchemy import or_, update, delete
from . import db
from .models import MonitorTarget, ScanJob, Event, PriceTrend, RevokedToken, LoginAttempt, User
from .tasks import ScanDispatcher, SingletonLease
from .scan import Scanner
from .items import extract_mods, fingerprint
from .timeutils import utcnow

logger = logging.getLogger(__name__)


class MonitorScheduler(ScanDispatcher):
    def __init__(self, app=None):
        super().__init__(app)
        self.stop_event = threading.Event()
        self.lease = SingletonLease()
        self.scanner = Scanner(app, cancel=self.stop_event)

    _extract_mods_by_type = staticmethod(extract_mods)
    _compute_fingerprint_hash = staticmethod(fingerprint)

    def ensure_jobs(self):
        targets = MonitorTarget.query.filter_by(enabled=True).all()
        known = {row.target_id for row in ScanJob.query.all()}
        for target in targets:
            if target.id not in known:
                due = (
                    target.last_scan + timedelta(seconds=target.scan_interval)
                    if target.last_scan
                    else utcnow()
                )
                db.session.add(ScanJob(target_id=target.id, state="idle", next_run_at=due))
        db.session.commit()

    def claim(self, excluded_users=()):
        now = utcnow()
        available = or_(ScanJob.state != "running", ScanJob.lease_until <= now)
        query = (
            db.session.query(ScanJob.target_id, MonitorTarget.user_id)
            .join(MonitorTarget)
            .filter(MonitorTarget.enabled.is_(True), ScanJob.next_run_at <= now, available)
        )
        if excluded_users:
            query = query.filter(MonitorTarget.user_id.notin_(excluded_users))
        candidate = query.order_by(ScanJob.next_run_at, ScanJob.target_id).first()
        if not candidate:
            db.session.rollback()
            return None
        token = str(uuid.uuid4())
        changed = db.session.execute(
            update(ScanJob)
            .where(ScanJob.target_id == candidate.target_id, ScanJob.next_run_at <= now, available)
            .values(
                state="running",
                lease_token=token,
                lease_until=now + timedelta(seconds=self.app.config["SCAN_LEASE_SECONDS"]),
            )
        ).rowcount
        db.session.commit()
        return (candidate.target_id, candidate.user_id, token) if changed else None

    def execute(self, target_id, user_id, token):
        with self.app.app_context():
            try:
                self.scanner.scan(target_id, token)
            except Exception as exc:
                db.session.rollback()
                # Upstream 401 remains a normal scan failure, as explicitly deferred.
                message = f"扫描目标 {target_id} 失败: {exc}"
                logger.warning(message)
                if db.session.get(User, user_id) is not None:
                    self.add_warning(message, user_id, target_id)
            finally:
                db.session.rollback()
                target = db.session.get(MonitorTarget, target_id)
                if target:
                    next_run = (
                        utcnow()
                        if self.stop_event.is_set()
                        else utcnow() + timedelta(seconds=target.scan_interval)
                    )
                    db.session.execute(
                        update(ScanJob)
                        .where(ScanJob.target_id == target_id, ScanJob.lease_token == token)
                        .values(
                            state="idle", next_run_at=next_run, lease_token=None, lease_until=None
                        )
                    )
                    db.session.commit()

    def clean_old_data(self):
        cutoff = utcnow() - timedelta(days=90)
        # Short batches avoid a large DELETE holding locks through the whole table.
        for model, column in [(Event, Event.created_at), (PriceTrend, PriceTrend.scan_time)]:
            while not self.stop_event.is_set():
                ids = [
                    row[0]
                    for row in db.session.query(model.id)
                    .filter(column < cutoff)
                    .order_by(column)
                    .limit(1000)
                    .all()
                ]
                if not ids:
                    break
                db.session.execute(delete(model).where(model.id.in_(ids)))
                db.session.commit()
        db.session.execute(delete(RevokedToken).where(RevokedToken.expires_at < utcnow()))
        db.session.execute(
            delete(LoginAttempt).where(LoginAttempt.window_start < utcnow() - timedelta(days=1))
        )
        db.session.commit()

    def run(self):
        active = {}
        with self.app.app_context():
            if not self.lease.renew():
                raise RuntimeError("已有扫描 worker 正在运行")
            self.ensure_jobs()
        last_sync = last_heartbeat = last_cleanup = 0
        import time

        pool = ThreadPoolExecutor(
            max_workers=self.app.config["WORKER_CONCURRENCY"], thread_name_prefix="poe-scan"
        )
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                for future in list(active):
                    if future.done():
                        try:
                            future.result()
                        except Exception:
                            logger.exception("Scan task failed outside its normal error handler")
                        del active[future]
                with self.app.app_context():
                    if now - last_heartbeat >= 10:
                        if not self.lease.renew():
                            raise RuntimeError("扫描 worker 租约已丢失")
                        last_heartbeat = now
                    if now - last_sync >= 30:
                        self.ensure_jobs()
                        last_sync = now
                    if now - last_cleanup >= 3600:
                        self.clean_old_data()
                        last_cleanup = now
                    while len(active) < self.app.config["WORKER_CONCURRENCY"]:
                        claimed = self.claim({value[0] for value in active.values()})
                        if claimed is None:
                            break
                        target_id, user_id, token = claimed
                        active[pool.submit(self.execute, target_id, user_id, token)] = (
                            user_id,
                            target_id,
                            token,
                        )
                self.stop_event.wait(0.5)
        finally:
            self.stop_event.set()
            pool.shutdown(wait=True, cancel_futures=True)
            with self.app.app_context():
                cancelled_tokens = [
                    value[2] for future, value in active.items() if future.cancelled()
                ]
                if cancelled_tokens:
                    db.session.execute(
                        update(ScanJob)
                        .where(ScanJob.lease_token.in_(cancelled_tokens))
                        .values(
                            state="idle", next_run_at=utcnow(), lease_token=None, lease_until=None
                        )
                    )
                    db.session.commit()
                self.lease.release()

    def shutdown(self):
        self.stop_event.set()
