from flask import request
from werkzeug.exceptions import BadRequest
from .items import money


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise BadRequest("请求体必须为 JSON 对象")
    return value


def integer(value, name, low=0, high=2147483647):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise BadRequest(f"{name} 必须为 {low} 到 {high} 的整数")
    return value


def query_int(name, default, low=0, high=2000):
    text = request.args.get(name)
    if text is None:
        return default
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} 必须为整数") from None
    return integer(value, name, low, high)


def text(value, name, maximum=100, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BadRequest(f"{name} 必须为不超过 {maximum} 字符的非空文本")
    return value.strip()


def price(value):
    if value is None:
        return None
    try:
        return money(value)
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
