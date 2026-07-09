"""Small helpers for Intervals.icu date strings."""

from datetime import date, datetime


def date_part(value: str | datetime | date | None) -> str:
    """Return YYYY-MM-DD from a date, datetime, or ISO-like string."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = str(value).strip()
    if not text:
        return ""
    if "T" in text:
        return text.split("T", 1)[0]
    return text[:10]


def parse_date(value: str | datetime | date) -> date:
    """Parse an Intervals.icu date or datetime into a date."""
    return datetime.fromisoformat(date_part(value)).date()


def start_datetime(value: str | datetime | date) -> str:
    """Return an ISO local datetime accepted by Intervals event writes."""
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()

    text = str(value).strip()
    if "T" in text:
        return text
    return f"{date_part(text)}T00:00:00"
