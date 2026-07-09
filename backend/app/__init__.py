import logging
from logging.handlers import RotatingFileHandler
import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager
from flask_cors import CORS
from .config import Config

db = SQLAlchemy()
jwt = JWTManager()

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    jwt.init_app(app)
    CORS(app)

    from .auth import auth_bp
    from .api import api_bp
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(api_bp, url_prefix='/api')

    # 配置文件日志（仅在非调试模式下，避免开发时干扰）
    if not app.debug:
        # 确保日志目录存在
        log_dir = '/var/log/poe-monitor'
        if not os.path.exists(log_dir):
            try:
                os.makedirs(log_dir, mode=0o755, exist_ok=True)
            except Exception:
                # 如果创建失败，则跳过文件日志（不影响应用运行）
                pass
        else:
            log_file = os.path.join(log_dir, 'poe-monitor.log')
            file_handler = RotatingFileHandler(log_file, maxBytes=10240, backupCount=10)
            file_handler.setFormatter(logging.Formatter(
                '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
            ))
            file_handler.setLevel(logging.INFO)
            app.logger.addHandler(file_handler)
            app.logger.setLevel(logging.INFO)
            app.logger.info('POE Monitor startup')

    return app