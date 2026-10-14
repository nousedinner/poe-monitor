from datetime import datetime, timedelta, timezone
import hashlib
import threading
from flask import Blueprint, request, jsonify, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from flask_jwt_extended import create_access_token, jwt_required, get_jwt
from sqlalchemy.exc import IntegrityError
from . import db, jwt
from .models import User, RevokedToken, LoginAttempt
from .timeutils import utcnow
from .validation import body, text

auth_bp = Blueprint("auth", __name__)
_DUMMY_HASH = generate_password_hash("unused-dummy-password-for-login-timing")
_PASSWORD_SLOTS = threading.BoundedSemaphore(2)


@jwt.token_in_blocklist_loader
def token_revoked(header, claims):
    identity = claims.get("sub")
    if (
        not isinstance(identity, str)
        or not identity.isdecimal()
        or len(identity) > 10
        or int(identity) > 2147483647
    ):
        return True
    return (
        db.session.get(User, int(identity)) is None
        or db.session.get(RevokedToken, claims.get("jti")) is not None
    )


@auth_bp.post("/login")
def login():
    data = body()
    username = text(data.get("username"), "username", 50)
    # Passwords must not be stripped; spaces can be intentional.
    password = data.get("password")
    if not isinstance(password, str) or not password or len(password) > 512:
        from werkzeug.exceptions import BadRequest

        raise BadRequest("password 必须为不超过 512 字符的非空文本")
    key = hashlib.sha256(f"{request.remote_addr}|{username.casefold()}".encode()).hexdigest()
    now = utcnow()
    attempt = db.session.get(LoginAttempt, key, with_for_update=True)
    if attempt is None:
        try:
            with db.session.begin_nested():
                db.session.add(LoginAttempt(key=key, failures=0, window_start=now))
                db.session.flush()
        except IntegrityError:
            pass
        attempt = db.session.get(LoginAttempt, key, with_for_update=True)
    window = current_app.config["LOGIN_RATE_WINDOW"]
    if now - attempt.window_start >= timedelta(seconds=window):
        attempt.failures = 0
        attempt.window_start = now
    if attempt.failures >= current_app.config["LOGIN_RATE_LIMIT"]:
        remaining = max(1, int(window - (now - attempt.window_start).total_seconds()))
        db.session.rollback()
        response = jsonify(msg="登录尝试过多，请稍后再试")
        response.headers["Retry-After"] = str(remaining)
        return response, 429
    user = db.session.query(User).filter_by(username=username).first()
    with _PASSWORD_SLOTS:
        valid = check_password_hash(user.password_hash if user else _DUMMY_HASH, password)
    if user is not None and valid:
        attempt.failures = 0
        db.session.commit()
        return jsonify(
            access_token=create_access_token(identity=str(user.id)),
            username=user.username,
            is_admin=user.is_admin,
        )
    attempt.failures += 1
    db.session.commit()
    return jsonify(msg="Invalid credentials"), 401


@auth_bp.post("/logout")
@jwt_required()
def logout():
    token = get_jwt()
    expires = datetime.fromtimestamp(token["exp"], timezone.utc).replace(tzinfo=None)
    db.session.merge(RevokedToken(jti=token["jti"], expires_at=expires))
    db.session.commit()
    return jsonify(msg="已退出登录")
