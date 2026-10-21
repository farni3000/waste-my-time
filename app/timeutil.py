from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite stores no timezone)."""
    return datetime.now(UTC).replace(tzinfo=None)


def local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    from zoneinfo import ZoneInfo

    from .config import get_settings

    return dt.replace(tzinfo=UTC).astimezone(ZoneInfo(get_settings().timezone))
