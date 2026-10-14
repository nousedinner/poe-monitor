import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from decimal import Decimal
from datetime import datetime
from flask import Flask, jsonify, request
from flask.json.provider import DefaultJSONProvider
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager
from flask_cors import CORS
from sqlalchemy import text
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from .config import Config, validate_config
from .timeutils import iso_utc

db = SQLAlchemy()
jwt = JWTManager()


class JSONProvider(DefaultJSONProvider):
    def default(self, value):
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, datetime):
            return iso_utc(value)
        return super().default(value)


def create_app(test_config=None):
    app = Flask(__name__)
    app.json = JSONProvider(app)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    validate_config(app)
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = dict(app.config["SQLALCHEMY_ENGINE_OPTIONS"])
    if str(app.config["SQLALCHEMY_DATABASE_URI"]).startswith("mysql"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"].setdefault(
            "connect_args",
            {
                "connect_timeout": 5,
                "read_timeout": 15,
                "write_timeout": 15,
            },
        )
    db.init_app(app)
    jwt.init_app(app)
    if app.config["CORS_ORIGINS"]:
        CORS(app, origins=app.config["CORS_ORIGINS"])
    if app.config["TRUST_PROXY_COUNT"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=app.config["TRUST_PROXY_COUNT"], x_proto=1)

    from .auth import auth_bp
    from .api import api_bp
    from .tasks import ScanDispatcher

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(api_bp, url_prefix="/api")
    # Importing a WSGI app creates a dispatcher, never a scheduler thread.
    app.scheduler = ScanDispatcher(app)

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(msg=exc.description), exc.code

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        db.session.rollback()
        app.logger.exception("Unhandled request failure")
        return jsonify(msg="服务器处理失败，请稍后重试"), 500

    @app.after_request
    def private_responses(response):
        if not request.path.startswith("/api/trade/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    @app.get("/readyz")
    def ready():
        try:
            db.session.execute(text("SELECT 1"))
            from .models import ScanJob, MonitorTarget, Event
            from .schema import VERSION

            if not db.session.execute(
                text("SELECT version FROM schema_version WHERE version=:v"), {"v": VERSION}
            ).first():
                return jsonify(status="unavailable"), 503
            db.session.query(ScanJob.target_id).limit(1).all()
            db.session.query(MonitorTarget.revision).limit(0).all()
            db.session.query(Event.price_currency_old).limit(0).all()
            return jsonify(status="ready")
        except Exception:
            db.session.rollback()
            return jsonify(status="unavailable"), 503

    if app.config["LOG_DIR"]:
        try:
            directory = Path(app.config["LOG_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            destination = str((directory / "poe-monitor.log").resolve())
            if not any(
                getattr(h, "baseFilename", None) == destination for h in app.logger.handlers
            ):
                handler = RotatingFileHandler(
                    destination, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf8"
                )
                handler.setFormatter(
                    logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
                )
                app.logger.addHandler(handler)
            app.logger.setLevel(logging.INFO)
        except OSError:
            app.logger.warning("File logging unavailable; using standard error")
    return app
