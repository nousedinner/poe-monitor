"""UTC storage and explicit Beijing calendar-day boundaries."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(value):
    return value.isoformat(timespec="microseconds") + "Z" if value else None


def parse_utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except (ValueError, TypeError, AttributeError):
        raise ValueError("时间参数必须为 ISO 时间格式") from None


def day_bounds(now=None):
    local = (now or utcnow()).replace(tzinfo=timezone.utc).astimezone(BEIJING)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    today = start.astimezone(timezone.utc).replace(tzinfo=None)
    return today - timedelta(days=1), today, today + timedelta(days=1)
