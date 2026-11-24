"""Time-limit arithmetic. Pure functions so they are easy to test.

Each kid has one rule per day of the week (a "playtime"): a daily minute limit, a bedtime, and
optional play windows. Parents edit them in the dashboard's Playtimes tab; until they do, the
daily_minutes/bedtime in config.yaml are used. With no windows, any time before bedtime is play time,
and last night's bedtime lasts until DAY_START: midnight starts a new day's minutes, not a new play time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import Kid

WARNING_MINUTES = 10
DAY_START = "06:00"   # on a day with no play windows, bedtime the night before holds until this time
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]   # datetime.weekday() order


def local_now(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


def day_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def parse_hhmm(raw: str) -> time:
    hh, mm = raw.split(":")
    return time(int(hh), int(mm))


@dataclass
class DayRule:
    daily_minutes: int
    bedtime: str = ""                                   # "HH:MM"; "" = no bedtime
    windows: list[tuple[str, str]] = field(default_factory=list)   # [("15:30", "17:30"), ...]; [] = any time


def default_rule(kid: Kid, weekday: int) -> DayRule:
    """config.yaml defaults: weekend minutes on Sat/Sun, weekend bedtime on Fri/Sat nights."""
    minutes = kid.daily_minutes.weekend if weekday >= 5 else kid.daily_minutes.weekday
    bedtime = kid.bedtime.weekend if weekday in (4, 5) else kid.bedtime.weekday
    return DayRule(daily_minutes=minutes, bedtime=bedtime, windows=[])


@dataclass
class TimeState:
    allowance: int
    granted: int
    used: int
    minutes_left: int          # what the kid can actually play now: min(today's minutes, time until the stop)
    minutes_to_bedtime: int
    locked: bool
    lock_reason: str           # "", "time", "bedtime", "outside_hours", "paused"
    warn: bool
    minutes_to_stop: int = 0   # until the current play window ends or bedtime, whichever is first
    next_play: str = ""        # ISO local time the next play window opens, when locked outside hours


def _at(now: datetime, hhmm: str) -> datetime:
    if hhmm == "24:00":                      # a window may run to midnight
        return _end_of_day(now)
    t = parse_hhmm(hhmm)
    return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def _end_of_day(now: datetime) -> datetime:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def next_window_start(rule_for, now: datetime) -> datetime | None:
    """First play-window start after now, looking up to a week ahead. rule_for(weekday) -> DayRule."""
    for offset in range(8):
        day = (now + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        rule = rule_for(day.weekday())
        if rule.daily_minutes <= 0:
            continue
        if not rule.windows:
            # An "any time" day opens when last night's bedtime ends, or at midnight if there was none.
            prev = rule_for((day.weekday() - 1) % 7)
            start = _at(day, DAY_START) if prev.bedtime else day
            if start > now:
                return start
            continue
        bed = _at(day, rule.bedtime) if rule.bedtime else _end_of_day(day)
        for s in sorted(_at(day, s) for s, _ in rule.windows):
            if s > now and s < bed:
                return s
    return None


def compute(rule: DayRule, now: datetime, used: int, granted: int, late_until: datetime | None = None,
            paused_until: datetime | None = None, rule_for=None) -> TimeState:
    """used/granted are today's minutes. late_until is set when a parent approved extra time
    after bedtime or outside the play windows: play is allowed until then regardless."""
    allowance = rule.daily_minutes
    left = max(0, allowance + granted - used)
    midnight = _end_of_day(now)
    bed_dt = _at(now, rule.bedtime) if rule.bedtime else midnight

    # The stretch of play the kid is in right now, if any.
    if rule.windows:
        current = [(_at(now, s), _at(now, e)) for s, e in rule.windows]
        stop = max((e for s, e in current if s <= now < e), default=None)
    else:
        # No windows: any time before bedtime. But the small hours belong to the night before: if that
        # day had a bedtime, it holds until DAY_START. Without this, midnight unlocked the PC with a fresh day.
        prev = rule_for((now.weekday() - 1) % 7) if rule_for else rule
        asleep = bool(prev.bedtime) and now < _at(now, DAY_START)
        stop = None if asleep else midnight
    if stop is not None:
        stop = min(stop, bed_dt)
    late = bool(late_until and late_until > now)
    if late:
        stop = max(stop or now, late_until)

    to_bed = max(0, int(((max(bed_dt, late_until) if late else bed_dt) - now).total_seconds() // 60))
    to_stop = max(0, int((stop - now).total_seconds() // 60)) if stop else 0
    effective_left = min(left, to_stop)

    if left == 0:
        reason = "time"
    elif to_bed == 0:
        reason = "bedtime"
    elif to_stop == 0:
        reason = "outside_hours" if rule.windows else "bedtime"
    else:
        reason = ""
    if paused_until and paused_until > now:
        reason = "paused"

    nxt = ""
    if reason in ("outside_hours", "bedtime") and rule_for:
        n = next_window_start(rule_for, now)
        nxt = n.isoformat() if n else ""

    return TimeState(
        allowance=allowance,
        granted=granted,
        used=used,
        minutes_left=effective_left,
        minutes_to_bedtime=to_bed,
        locked=bool(reason),
        lock_reason=reason,
        warn=0 < effective_left <= WARNING_MINUTES,
        minutes_to_stop=to_stop,
        next_play=nxt,
    )
