import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY")
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
    POESESSID_ENCRYPTION_KEY = os.environ.get("POESESSID_ENCRYPTION_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 3600}
    JWT_ACCESS_TOKEN_EXPIRES = 30 * 24 * 3600
    MAX_CONTENT_LENGTH = 256 * 1024
    LOG_DIR = os.environ.get("LOG_DIR", "")
    TRUST_PROXY_COUNT = int(os.environ.get("TRUST_PROXY_COUNT", "0"))
    CORS_ORIGINS = [v.strip() for v in os.environ.get("CORS_ORIGINS", "").split(",") if v.strip()]
    WORKER_CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", "4"))
    SCAN_LEASE_SECONDS = 900
    POE_REQUEST_INTERVAL = float(os.environ.get("POE_REQUEST_INTERVAL", "0.5"))
    POE_API_BASE_URL = os.environ.get("POE_API_BASE_URL", "https://poe.game.qq.com/api/trade")
    POE_REQUEST_BUDGET = 30
    LOGIN_RATE_LIMIT = 5
    LOGIN_RATE_WINDOW = 900


def validate_config(app):
    for key in ["SECRET_KEY", "JWT_SECRET_KEY", "POESESSID_ENCRYPTION_KEY"]:
        value = app.config.get(key)
        if (
            not isinstance(value, str)
            or len(value) < 32
            or value.startswith(("your_", "change-me"))
        ):
            raise RuntimeError(
                f"{key} 必须设置为至少 32 字符的非占位密钥；使用 python manage.py init-env 生成"
            )
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        raise RuntimeError("环境变量 DATABASE_URL 未设置")
