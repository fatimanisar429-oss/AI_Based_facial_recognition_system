"""Reading ?from=...&to=... from a page's URL safely (shared by the attendance, log and exit pages)."""
from datetime import date, timedelta

from flask import current_app

from ..timeutil import hostel_today, parse_date

MAX_RANGE_DAYS = 366


def today():
    cfg = current_app.config
    return hostel_today(cfg["TIMEZONE"], cfg["ATTENDANCE_DAY_START"])


def date_range(args, default_days=0):
    """(first_day, last_day) as YYYY-MM-DD strings. Bad or missing values fall back to the last `default_days`+1
    days; reversed dates are swapped; ranges longer than a year are shortened."""
    end_default = today()
    start_default = (date.fromisoformat(end_default) - timedelta(days=default_days)).isoformat()
    first = parse_date(args.get("from"), start_default)
    last = parse_date(args.get("to"), end_default)
    if first > last:
        first, last = last, first
    earliest = (date.fromisoformat(last) - timedelta(days=MAX_RANGE_DAYS - 1)).isoformat()
    return max(first, earliest), last
