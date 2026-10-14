"""Attendance (SRS FR-09, FR-10).

Rules
  * The FIRST successful student entry of a hostel day creates that day's attendance record.
  * Before (or exactly at) the curfew it is Present, after the curfew it is Late.
  * Later successful entries the same day are logged but never create another attendance record.
  * Failed attempts and guest entries never create attendance (this module is only called for a granted student).
Days without a record are counted as absent in reports; no "Absent" row is stored.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..timeutil import hostel_day, parse_hhmm
from . import appsettings
from .people import like_pattern, paginate

STATUSES = ("Present", "Late")


def mark_entry(db, student_id, moment, cfg):
    """Record attendance for a successful entry at `moment` (UTC datetime). Does NOT commit: the caller commits it
    together with the entry-log row so both are saved, or neither.

    Returns {"status": "Present"|"Late", "first_entry": bool, "date": "YYYY-MM-DD"}."""
    tz = cfg["TIMEZONE"]
    hostel_date, local = hostel_day(moment, tz, cfg["ATTENDANCE_DAY_START"])
    curfew = datetime.combine(hostel_date, parse_hhmm(appsettings.curfew_time(db, cfg)), tzinfo=ZoneInfo(tz))
    status = "Late" if local > curfew else "Present"
    # UNIQUE(student_id, local_date) makes this safe even if two entries arrive at the same moment.
    cur = db.execute(
        "INSERT OR IGNORE INTO attendance (student_id, local_date, local_time, status) VALUES (?, ?, ?, ?)",
        (student_id, hostel_date.isoformat(), local.strftime("%H:%M:%S"), status))
    if cur.rowcount == 1:
        return {"status": status, "first_entry": True, "date": hostel_date.isoformat()}
    existing = db.execute("SELECT status FROM attendance WHERE student_id = ? AND local_date = ?",
                          (student_id, hostel_date.isoformat())).fetchone()
    return {"status": existing["status"], "first_entry": False, "date": hostel_date.isoformat()}


# ---------- reading ----------

def _where(date_from, date_to, status, q):
    where, params = "WHERE a.local_date BETWEEN ? AND ?", [date_from, date_to]
    if status in STATUSES:
        where += " AND a.status = ?"
        params.append(status)
    if q:
        where += (" AND (s.full_name LIKE ? ESCAPE '\\' OR s.registration_no LIKE ? ESCAPE '\\'"
                  " OR s.room_no LIKE ? ESCAPE '\\')")
        params += [like_pattern(q)] * 3
    return where, params


def search(db, date_from, date_to, status="", q="", page=1):
    """Staff view: attendance records of all students, newest first, plus Present/Late counts for the whole filter."""
    where, params = _where(date_from, date_to, status, q)
    base = "FROM attendance a JOIN student s ON s.student_id = a.student_id " + where
    result = paginate(
        db, f"SELECT COUNT(*) {base}",
        "SELECT a.attendance_id, a.local_date, a.local_time, a.status, s.student_id, s.full_name, s.registration_no, "
        f"s.room_no {base} ORDER BY a.local_date DESC, a.local_time DESC, a.attendance_id DESC", params, page)
    counts = {r[0]: r[1] for r in db.execute(f"SELECT a.status, COUNT(*) {base} GROUP BY a.status", params)}
    result["present"], result["late"] = counts.get("Present", 0), counts.get("Late", 0)
    return result


def for_student(db, student_id, month_prefix):
    """One student's own records for a month ('YYYY-MM'), newest first, with counts."""
    rows = db.execute(
        "SELECT local_date, local_time, status FROM attendance WHERE student_id = ? AND local_date LIKE ? "
        "ORDER BY local_date DESC", (student_id, month_prefix + "%")).fetchall()
    return {"rows": rows, "present": sum(r["status"] == "Present" for r in rows), "late": sum(r["status"] == "Late" for r in rows)}


def student_entry_history(db, student_id, limit=20):
    """The student's own entry attempts that were granted to them (denied attempts are never linked to a student)."""
    return db.execute(
        "SELECT attempted_at, method, result FROM entry_log WHERE student_id = ? "
        "ORDER BY attempted_at DESC, entry_id DESC LIMIT ?", (student_id, limit)).fetchall()


def valid_month(text, default):
    try:
        year, month = (text or "").split("-")
        date(int(year), int(month), 1)
        return f"{int(year):04d}-{int(month):02d}"
    except (ValueError, TypeError):
        return default
