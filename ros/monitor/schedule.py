"""Time: an injectable clock and schedule arithmetic.

Every stored time is UTC ISO 8601. Local schedules ("a las 20:00", Europe/Madrid) are resolved
with zoneinfo: a nonexistent local time (spring forward) moves to the first valid instant after
it, and an ambiguous one (fall back) runs once, at the first occurrence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from .spec import DigestPolicy, WatchSpec

UTC = timezone.utc


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC).replace(microsecond=0)


class FixedClock:
    """Test clock: set or advance it explicitly."""

    def __init__(self, start: datetime):
        self.current = start.astimezone(UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> datetime:
        self.current += timedelta(**delta)
        return self.current


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def local_to_utc(naive: datetime, tz: ZoneInfo) -> datetime:
    """Resolve a local wall-clock time. Ambiguous -> first occurrence; nonexistent -> next valid minute."""
    candidate = naive
    for _ in range(24 * 60):
        aware = candidate.replace(tzinfo=tz, fold=0)
        back = aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
        if back == candidate:
            return aware.astimezone(UTC)
        candidate += timedelta(minutes=1)
    raise ValueError(f"cannot resolve local time {naive} in {tz}")  # pragma: no cover


def next_daily(after: datetime, at: str, tz_name: str, weekday: int | None = None) -> datetime:
    """First instant strictly after `after` whose local time is `at` (optionally on `weekday`)."""
    tz = ZoneInfo(tz_name)
    hour, minute = (int(x) for x in at.split(":"))
    local_day = after.astimezone(tz).date()
    for offset in range(0, 15):
        day = local_day + timedelta(days=offset)
        if weekday is not None and day.weekday() != weekday:
            continue
        when = local_to_utc(datetime(day.year, day.month, day.day, hour, minute), tz)
        if when > after:
            return when
    raise ValueError("no next occurrence found")  # pragma: no cover


def next_digest_at(policy: DigestPolicy, after: datetime, tz_name: str) -> datetime | None:
    if policy.mode == "daily":
        return next_daily(after, policy.at, tz_name)
    if policy.mode == "weekly":
        return next_daily(after, policy.at, tz_name, weekday=policy.weekday)
    if policy.mode == "interval":
        return after + timedelta(hours=policy.every_hours)
    return None  # threshold and manual are not time-driven


def next_run_at(spec: WatchSpec, after: datetime) -> datetime:
    return after + timedelta(minutes=spec.every_minutes)
