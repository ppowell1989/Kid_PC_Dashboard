"""Time-limit arithmetic. Pure functions so they are easy to test."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .config import Kid

WARNING_MINUTES = 10


def local_now(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


def day_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def is_weekend(now: datetime) -> bool:
    # Sat/Sun get weekend minutes; Fri/Sat nights get the weekend bedtime (see bedtime_for). GUESS: adjust to taste.
    return now.weekday() >= 5


def allowance_for(kid: Kid, now: datetime) -> int:
    return kid.daily_minutes.weekend if is_weekend(now) else kid.daily_minutes.weekday


def bedtime_for(kid: Kid, now: datetime) -> time:
    raw = kid.bedtime.weekend if now.weekday() in (4, 5) else kid.bedtime.weekday
    hh, mm = raw.split(":")
    return time(int(hh), int(mm))


@dataclass
class TimeState:
    allowance: int
    granted: int
    used: int
    minutes_left: int
    minutes_to_bedtime: int
    locked: bool
    lock_reason: str  # "", "time", "bedtime"
    warn: bool


def compute(kid: Kid, now: datetime, used: int, granted: int, late_until: datetime | None = None) -> TimeState:
    """used/granted are today's minutes. late_until is set when a parent approved
    extra time after bedtime: play is allowed until then even though bedtime passed."""
    allowance = allowance_for(kid, now)
    left = max(0, allowance + granted - used)
    bt = bedtime_for(kid, now)
    bed_dt = now.replace(hour=bt.hour, minute=bt.minute, second=0, microsecond=0)
    if late_until and late_until > bed_dt:
        bed_dt = late_until
    to_bed = max(0, int((bed_dt - now).total_seconds() // 60))
    effective_left = min(left, to_bed)
    reason = "time" if left == 0 else ("bedtime" if to_bed == 0 else "")
    return TimeState(
        allowance=allowance,
        granted=granted,
        used=used,
        minutes_left=effective_left,
        minutes_to_bedtime=to_bed,
        locked=bool(reason),
        lock_reason=reason,
        warn=0 < effective_left <= WARNING_MINUTES,
    )
