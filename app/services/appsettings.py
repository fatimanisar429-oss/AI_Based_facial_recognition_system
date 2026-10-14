"""Settings an administrator can change at run time (stored in the `setting` table).
A value saved here wins over the default in .env / config.py."""
from ..timeutil import is_hhmm, parse_hhmm


def get(db, key, default=None):
    row = db.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def put(db, key, value):
    db.execute("INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (key, value))
    db.commit()


def curfew_time(db, cfg):
    """The curfew as 'HH:MM'. Falls back to the .env value if nothing valid is saved."""
    saved = get(db, "curfew_time")
    return saved if is_hhmm(saved) else cfg["CURFEW_TIME"]


def validate_curfew(text, day_start_text):
    """Returns an error message or None. The curfew must fall inside the hostel day: after it starts."""
    text = (text or "").strip()
    if not is_hhmm(text):
        return "Enter the curfew as HH:MM in 24-hour time, for example 22:00."
    if parse_hhmm(text) <= parse_hhmm(day_start_text):
        return f"The curfew must be later than the start of the attendance day ({day_start_text})."
    return None
