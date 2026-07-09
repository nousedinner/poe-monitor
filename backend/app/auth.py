from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from flask_jwt_extended import create_access_token
from . import db
from .models import User

auth_bp = Blueprint('auth', __name__)

# 注册接口已关闭（管理员手动添加用户）
# @auth_bp.route('/register', methods=['POST'])
# def register():
#     data = request.get_json()
#     username = data.get('username')
#     password = data.get('password')
#     if not username or not password:
#         return jsonify({'msg': 'Missing username or password'}), 400
#     if User.query.filter_by(username=username).first():
#         return jsonify({'msg': 'Username already exists'}), 400
#     hashed = generate_password_hash(password)
#     user = User(username=username, password_hash=hashed)
#     db.session.add(user)
#     db.session.commit()
#     return jsonify({'msg': 'User created'}), 201

@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    user = User.query.filter_by(username=username).first()
    if user and check_password_hash(user.password_hash, password):
        access_token = create_access_token(identity=str(user.id))
        return jsonify(access_token=access_token), 200
    return jsonify({'msg': 'Invalid credentials'}), 401