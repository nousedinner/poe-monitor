from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
from werkzeug.exceptions import BadRequest


@lru_cache(maxsize=1)
def item_index():
    path = Path(__file__).resolve().parents[1] / "data" / "items.json"
    with path.open(encoding="utf8") as stream:
        data = json.load(stream)
    return {
        entry["text"]: entry
        for category in data.get("result", [])
        for entry in category.get("entries", [])
        if entry.get("text")
    }


def convert_filters(filters):
    if not isinstance(filters, dict):
        raise BadRequest("filters 必须为对象")
    result = deepcopy(filters)
    for group in result.values():
        if not isinstance(group, dict) or not isinstance(group.get("filters", {}), dict):
            raise BadRequest("筛选组格式无效")
        for key, value in group.get("filters", {}).items():
            if isinstance(value, dict) and isinstance(value.get("value"), dict):
                group["filters"][key] = {
                    **{k: v for k, v in value.items() if k != "value"},
                    **value["value"],
                }
    return result


def normalize_query(raw):
    if not isinstance(raw, dict):
        raise BadRequest("query 必须为对象")
    query = {
        k: deepcopy(v)
        for k, v in raw.items()
        if not k.startswith("_") and k in {"name", "type", "filters", "stats", "status", "term"}
    }
    for name in ["name", "type"]:
        value = query.get(name)
        if isinstance(value, dict):
            if not isinstance(value.get("option"), str):
                raise BadRequest(f"{name}.option 必须为文本")
            # Discriminators identify transfigured gems; never discard them.
            if not value.get("discriminator"):
                query[name] = value["option"]
        elif value is not None and not isinstance(value, str):
            raise BadRequest(f"{name} 必须为文本或选项对象")
    if query.get("name") and not query.get("type") and isinstance(query["name"], str):
        entry = item_index().get(query["name"])
        if entry and entry.get("disc"):
            query["type"] = {"option": entry["type"], "discriminator": entry["disc"]}
            query.pop("name", None)
        elif entry:
            query["type"] = entry["type"]
            if entry.get("name"):
                query["name"] = entry["name"]
            else:
                query.pop("name", None)
        else:
            query["type"] = query.pop("name")
    if "filters" in query:
        query["filters"] = convert_filters(query["filters"])
    if "stats" in query and not isinstance(query["stats"], list):
        raise BadRequest("stats 必须为列表")
    if "status" in query and not isinstance(query["status"], dict):
        raise BadRequest("status 必须为对象")
    if not any(query.get(key) for key in ["name", "type", "filters", "stats"]):
        query["type"] = ""
    return query
