"""cron.py - five field cron expressions (minute hour day month weekday) for schedules"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}

# name, min, max
FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 7),
)

# far enough for "29 2 *" (leap days) to be found, a pattern that never matches stops here
SEARCH_DAYS = 366 * 8


@dataclass(frozen=True)
class Cron:
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = sunday
    # like cron, when both day and weekday are restricted a day matching either runs
    day_star: bool
    weekday_star: bool

    def day_matches(self, day: datetime) -> bool:
        if day.month not in self.months:
            return False
        in_days = day.day in self.days
        in_weekdays = day.isoweekday() % 7 in self.weekdays
        if self.day_star or self.weekday_star:
            return in_days and in_weekdays
        return in_days or in_weekdays

    def next_after(self, after: datetime) -> datetime | None:
        """the first matching minute after `after` (naive wall time), None if it never runs"""
        start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = start.replace(hour=0, minute=0)
        for _ in range(SEARCH_DAYS):
            if self.day_matches(day):
                for hour in self.hours:
                    for minute in self.minutes:
                        candidate = day.replace(hour=hour, minute=minute)
                        if candidate >= start:
                            return candidate
            day += timedelta(days=1)
        return None

    def next_after_utc(self, after: datetime) -> datetime | None:
        """next_after for naive utc times, matching the cron in the container's TZ"""
        local = after.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        found = self.next_after(local)
        if found is None:
            return None
        return found.astimezone(timezone.utc).replace(tzinfo=None)


def parse_field(text: str, name: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) == 0:
                raise ValueError(f"{name}: bad step '{part}'")
            step = int(step_text)
        if base == "*":
            first, last = low, high
        elif "-" in base:
            first_text, _, last_text = base.partition("-")
            if not first_text.isdigit() or not last_text.isdigit():
                raise ValueError(f"{name}: bad range '{part}'")
            first, last = int(first_text), int(last_text)
        elif base.isdigit():
            first = int(base)
            # "5/15" means from 5 to the end, every 15
            last = high if step_text else first
        else:
            raise ValueError(f"{name}: '{part}' isn't a number, range or *")
        if first < low or last > high or first > last:
            raise ValueError(f"{name}: '{part}' is outside {low}-{high}")
        values.update(range(first, last + 1, step))
    return values


def parse(text: str) -> Cron:
    """raises ValueError saying which field is wrong"""
    expression = MACROS.get(text.strip().lower(), text)
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("needs 5 fields: minute hour day month weekday")
    minutes, hours, days, months, weekdays = (
        parse_field(part, *field) for part, field in zip(parts, FIELDS)
    )
    weekdays = {d % 7 for d in weekdays}  # 7 is sunday too
    cron = Cron(
        minutes=tuple(sorted(minutes)),
        hours=tuple(sorted(hours)),
        days=frozenset(days),
        months=frozenset(months),
        weekdays=frozenset(weekdays),
        day_star=parts[2].startswith("*"),
        weekday_star=parts[4].startswith("*"),
    )
    if cron.next_after(datetime(2000, 1, 1)) is None:
        raise ValueError("never runs: that day doesn't exist in those months")
    return cron
