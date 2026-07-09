import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        raise RuntimeError('环境变量 SECRET_KEY 未设置')

    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL')
    if not SQLALCHEMY_DATABASE_URI:
        raise RuntimeError('环境变量 DATABASE_URL 未设置')

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY')
    if not JWT_SECRET_KEY:
        raise RuntimeError('环境变量 JWT_SECRET_KEY 未设置')

    JWT_ACCESS_TOKEN_EXPIRES = 30 * 24 * 3600

    POESESSID_ENCRYPTION_KEY = os.environ.get('POESESSID_ENCRYPTION_KEY')
    if not POESESSID_ENCRYPTION_KEY:
        raise RuntimeError('环境变量 POESESSID_ENCRYPTION_KEY 未设置')
