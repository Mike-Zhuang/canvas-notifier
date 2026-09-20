from datetime import UTC, datetime


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_time(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return dt.astimezone(UTC)
