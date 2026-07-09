import base64
from cryptography.fernet import Fernet
from flask import current_app

def get_cipher():
    key = current_app.config['POESESSID_ENCRYPTION_KEY']
    # 确保密钥长度为32字节（base64编码后）
    if len(key) < 32:
        key = key.ljust(32, '0')
    key_bytes = base64.urlsafe_b64encode(key[:32].encode())
    return Fernet(key_bytes)

def encrypt_poesessid(plaintext: str) -> str:
    if not plaintext:
        return None
    cipher = get_cipher()
    return cipher.encrypt(plaintext.encode()).decode()

def decrypt_poesessid(ciphertext: str) -> str:
    if not ciphertext:
        return None
    cipher = get_cipher()
    return cipher.decrypt(ciphertext.encode()).decode()