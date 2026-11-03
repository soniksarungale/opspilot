from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import db

IST = ZoneInfo("Asia/Kolkata")


def offset_hours() -> float:
    return float(db.get_setting("clock_offset_hours", "0") or 0)


def now() -> datetime:
    return datetime.now(IST) + timedelta(hours=offset_hours())


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)
