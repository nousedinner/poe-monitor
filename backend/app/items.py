"""Shared item parsing and fingerprints for account relisting across changing IDs."""

from decimal import Decimal, InvalidOperation
import hashlib
import json

MOD_FIELDS = {
    "explicitMods": "explicit_mods",
    "implicitMods": "implicit_mods",
    "fracturedMods": "fractured_mods",
    "craftedMods": "crafted_mods",
    "enchantMods": "enchant_mods",
    "veiledMods": "veiled_mods",
    "mutatedMods": "mutated_mods",
}
PRECISION = Decimal("0.000001")


def money(value):
    if isinstance(value, bool):
        raise ValueError("价格必须为非负数")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number >= Decimal("100000000000000"):
            raise ValueError("价格超出范围")
        rounded = number.quantize(PRECISION)
        if rounded >= Decimal("100000000000000"):
            raise ValueError("价格超出范围")
        return rounded
    except (InvalidOperation, TypeError):
        raise ValueError("价格格式无效") from None


def mod_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("description") or value.get("text") or "")
    return ""


def extract_mods(item):
    result = {
        key: [mod_text(v) for v in item.get(key, []) if mod_text(v)]
        if isinstance(item.get(key, []), list)
        else []
        for key in MOD_FIELDS
    }
    extra = []
    for prop in item.get("notableProperties") or []:
        if isinstance(prop, dict) and prop.get("name"):
            extra.append(str(prop["name"]))
    for prop in item.get("properties") or []:
        if not isinstance(prop, dict):
            continue
        values = prop.get("values") or []
        if values and isinstance(values[0], (list, tuple)) and values[0] and values[0][0]:
            extra.append(f"{prop.get('name', '')}: {values[0][0]}")
    if item.get("builtInSupport"):
        extra.append(f"内置辅助: {item['builtInSupport']}")
    result["explicitMods"] += extra
    return result


def fingerprint(item):
    mods = [m for values in extract_mods(item).values() for m in values]
    value = f"{item.get('name', '')}|{item.get('typeLine', '')}|{json.dumps(sorted(mods))}"
    return hashlib.sha256(value.encode()).hexdigest()


def item_values(raw):
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("item"), dict)
        or not isinstance(raw.get("listing"), dict)
    ):
        raise ValueError("物品详情结构不完整")
    item = raw["item"]
    listing = raw["listing"]
    price = listing.get("price")
    if (
        not isinstance(raw.get("id"), str)
        or not raw["id"]
        or len(raw["id"]) > 255
        or not isinstance(price, dict)
    ):
        raise ValueError("物品详情缺少 ID 或报价")
    currency = price.get("currency")
    if not isinstance(currency, str) or not currency or len(currency) > 20:
        raise ValueError("报价币种无效")
    values = dict(
        item_id=raw["id"],
        item_name=item.get("name"),
        base_type=item.get("typeLine"),
        price=money(price.get("amount")),
        price_currency=currency,
        seller_account=listing.get("account", {}).get("name"),
    )
    mods = extract_mods(item)
    values.update(
        {column: json.dumps(mods[key], ensure_ascii=False) for key, column in MOD_FIELDS.items()}
    )
    return values


def validate_details(requested_ids, details):
    if not isinstance(details, list):
        raise ValueError("物品详情响应无效")
    received = [row.get("id") for row in details if isinstance(row, dict)]
    if (
        len(received) != len(details)
        or len(set(received)) != len(received)
        or set(received) != set(requested_ids)
    ):
        raise ValueError("物品详情不完整，本次保留原快照与事件")
    for row in details:
        item_values(row)
    return details
