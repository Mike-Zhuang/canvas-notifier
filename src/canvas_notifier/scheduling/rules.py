import re
from datetime import timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from canvas_notifier.db import Rule


class Rules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipients: list[str] | None = None
    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    due: list[str] = Field(default_factory=lambda: ["P3D", "PT24H", "PT6H", "PT1H"])
    lock: list[str] = Field(default_factory=lambda: ["PT24H", "PT2H"])
    unlock: list[str] = Field(default_factory=list)
    personal: list[str] = Field(default_factory=lambda: ["PT24H", "PT1H"])
    only_incomplete: bool = True
    score_in_email: bool = False
    events: dict[str, Literal["immediate", "digest", "off"]] = Field(
        default_factory=lambda: {
            "file_created": "digest",
            "file_changed": "digest",
            "reply_created": "digest",
            "reply_changed": "digest",
        }
    )
    default_event: Literal["immediate", "digest", "off"] = "immediate"
    digest_hour: int = Field(default=18, ge=0, le=23)
    quiet_enabled: bool = False
    quiet_start: str = "23:00"
    quiet_end: str = "08:00"
    critical_policy: Literal["allow_before_boundary", "advance", "digest_only"] = "allow_before_boundary"
    stale_source_policy: Literal["send_with_last_synced_at", "pause"] = "send_with_last_synced_at"
    stale_after_minutes: int = Field(default=30, ge=5, le=10080)
    overdue_enabled: bool = False
    overdue_repeat: str = "PT24H"
    overdue_max: int = Field(default=2, ge=0, le=10)
    history_days: int = Field(default=120, ge=0, le=3650)
    pinned: bool = False

    @field_validator("recipients")
    @classmethod
    def valid_recipients(cls, values):
        if values is None:
            return None
        from canvas_notifier.delivery.queue import recipients

        return recipients(",".join(values))

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown IANA timezone") from None
        return value

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def valid_clock(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Use HH:MM")
        return value

    @field_validator("due", "lock", "unlock", "personal")
    @classmethod
    def valid_offsets(cls, values):
        if len(values) > 20:
            raise ValueError("At most 20 offsets")
        seconds = sorted({duration(v) for v in values}, reverse=True)
        return [f"PT{s}S" for s in seconds]

    @field_validator("overdue_repeat")
    @classmethod
    def valid_repeat(cls, value):
        if duration(value) < 3600:
            raise ValueError("Overdue repeat must be at least one hour")
        return value

    @model_validator(mode="after")
    def different_quiet(self):
        if self.quiet_enabled and self.quiet_start == self.quiet_end:
            raise ValueError("Quiet start and end must differ")
        return self


def duration(value: str) -> int:
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value)
    if not match:
        raise ValueError("Use positive ISO duration: P3D / PT24H / PT30M; months unsupported")
    days, hours, minutes, seconds = (int(v or 0) for v in match.groups())
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    if not 0 < total <= 366 * 86400:
        raise ValueError("Duration must be between 1 second and 366 days")
    return total


async def effective_rules(session, course_id="", resource_key="") -> tuple[Rules, list[str]]:
    data, sources = {}, []
    for scope in ["global", f"course:{course_id}", resource_key]:
        if not scope:
            continue
        row = await session.get(Rule, scope)
        if row:
            if "events" in row.data:
                data["events"] = {**data.get("events", {}), **row.data["events"]}
            data.update({k: v for k, v in row.data.items() if k != "events"})
            sources.append(scope)
    return Rules.model_validate(data), sources


def quiet_adjust(trigger, boundary, rule: Rules):
    if not rule.quiet_enabled:
        return trigger
    local = trigger.astimezone(ZoneInfo(rule.timezone))
    start_h, start_m = map(int, rule.quiet_start.split(":"))
    end_h, end_m = map(int, rule.quiet_end.split(":"))
    start = local.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if start >= end:
        if local < end:
            start -= timedelta(days=1)
        else:
            end += timedelta(days=1)
    if not start <= local < end:
        return trigger
    if boundary and end >= boundary:
        if rule.critical_policy == "allow_before_boundary":
            return trigger
        if rule.critical_policy == "advance":
            return start - timedelta(minutes=1)
        return None
    return end


def digest_time(now, rule: Rules):
    local = now.astimezone(ZoneInfo(rule.timezone))
    target = local.replace(hour=rule.digest_hour, minute=0, second=0, microsecond=0)
    return target if target > local else target + timedelta(days=1)
