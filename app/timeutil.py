"""Time helpers. All stored timestamps are UTC text like 2026-10-11T15:04:05."""
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

FORMAT = "%Y-%m-%dT%H:%M:%S"


def local_today(tz_name: str) -> str:
    """Today's date (YYYY-MM-DD) in the hostel's timezone. Attendance days use this."""
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def format_local(utc_text, tz_name: str, pattern="%d %b %Y, %I:%M %p") -> str:
    """Convert a stored UTC text timestamp to the hostel's timezone for display."""
    if not utc_text:
        return ""
    moment = datetime.strptime(utc_text, FORMAT).replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo(tz_name)).strftime(pattern)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(FORMAT)


def utc_now_text() -> str:
    return to_text(utc_now())


def minutes_ago_text(minutes: int) -> str:
    return to_text(utc_now() - timedelta(minutes=minutes))


# ---------- the "hostel day" ----------
# An attendance day does not start at midnight: someone arriving at 00:30 is late for YESTERDAY's curfew, not
# early for today. The hostel day therefore starts at ATTENDANCE_DAY_START (04:00 by default) in the local timezone.

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def is_hhmm(text) -> bool:
    return bool(text) and bool(HHMM.match(text))


def parse_hhmm(text) -> dtime:
    hour, minute = text.split(":")
    return dtime(int(hour), int(minute))


def hostel_day(moment, tz_name, day_start_text):
    """(hostel_date, local_datetime) for a UTC moment."""
    local = moment.astimezone(ZoneInfo(tz_name))
    start = parse_hhmm(day_start_text)
    return (local - timedelta(hours=start.hour, minutes=start.minute)).date(), local


def hostel_today(tz_name, day_start_text) -> str:
    return hostel_day(utc_now(), tz_name, day_start_text)[0].isoformat()


def parse_date(text, default=None):
    """YYYY-MM-DD -> that string if it is a real date, otherwise `default`."""
    try:
        return date.fromisoformat((text or "").strip()).isoformat()
    except ValueError:
        return default


def day_bounds(first_day, last_day, tz_name, day_start_text):
    """UTC text bounds [start, end) covering the hostel days first_day..last_day (inclusive)."""
    zone, start = ZoneInfo(tz_name), parse_hhmm(day_start_text)
    begin = datetime.combine(date.fromisoformat(first_day), start, tzinfo=zone)
    end = datetime.combine(date.fromisoformat(last_day) + timedelta(days=1), start, tzinfo=zone)
    return to_text(begin), to_text(end)
