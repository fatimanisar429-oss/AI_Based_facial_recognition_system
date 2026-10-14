"""Read-only queries that feed the three dashboards.

Each function takes the database and returns plain dicts/lists. The student functions
always filter by the student's own id, so a student can never see someone else's rows.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..timeutil import FORMAT, day_bounds, hostel_day, utc_now_text
from .charts import column_chart

# Entry rows with the person's name. Unidentified people (student_id and guest_id NULL) show as "Unidentified".
_ENTRY_SELECT = """
    SELECT e.entry_id, e.attempted_at, e.method, e.result, e.failure_reason, e.needs_review,
           e.subject_type,
           COALESCE(s.full_name, g.full_name, 'Unidentified') AS person
    FROM entry_log e
    LEFT JOIN student s ON s.student_id = e.student_id
    LEFT JOIN guest_profile g ON g.guest_id = e.guest_id
"""


def _count(db, sql, params=()):
    return db.execute(sql, params).fetchone()[0]


def attendance_counts_for_day(db, local_date):
    rows = db.execute(
        "SELECT status, COUNT(*) FROM attendance WHERE local_date = ? GROUP BY status", (local_date,)
    ).fetchall()
    counts = {r[0]: r[1] for r in rows}
    return {"present": counts.get("Present", 0), "late": counts.get("Late", 0)}


def recent_entries(db, result, limit=8):
    """Latest granted or denied attempts, newest first (all people; staff dashboards only)."""
    return db.execute(
        _ENTRY_SELECT + " WHERE e.result = ? ORDER BY e.attempted_at DESC, e.entry_id DESC LIMIT ?",
        (result, limit),
    ).fetchall()


def entries_by_day(db, tz_name, today, days=7, day_start="00:00"):
    """Granted/denied attempt counts for the last `days` hostel days, oldest first."""
    last = date.fromisoformat(today)
    first = last - timedelta(days=days - 1)
    start_utc, end_utc = day_bounds(first.isoformat(), last.isoformat(), tz_name, day_start)
    counts = {first + timedelta(days=i): [0, 0] for i in range(days)}  # [granted, denied]
    for row in db.execute("SELECT attempted_at, result FROM entry_log WHERE attempted_at >= ? AND attempted_at < ?",
                          (start_utc, end_utc)):
        moment = datetime.strptime(row["attempted_at"], FORMAT).replace(tzinfo=timezone.utc)
        day = hostel_day(moment, tz_name, day_start)[0]
        if day in counts:
            counts[day][0 if row["result"] == "Granted" else 1] += 1
    return [{"label": d.strftime("%a"), "full": d.strftime("%a %d %b"), "values": v}
            for d, v in counts.items()]


def admin_dashboard(db, today, tz_name, day_start="00:00"):
    total_students = _count(db, "SELECT COUNT(*) FROM student WHERE is_active = 1")
    attendance = attendance_counts_for_day(db, today)
    return {
        "total_students": total_students,
        "total_wardens": _count(db, "SELECT COUNT(*) FROM warden WHERE is_active = 1"),
        "attendance": attendance,
        "not_yet_in": max(total_students - attendance["present"] - attendance["late"], 0),
        "week": column_chart(entries_by_day(db, tz_name, today, 7, day_start), ["Granted", "Denied"]),
        "recent_granted": recent_entries(db, "Granted"),
        "recent_failed": recent_entries(db, "Denied"),
    }


def warden_dashboard(db, today, tz_name, day_start="00:00"):
    total_students = _count(db, "SELECT COUNT(*) FROM student WHERE is_active = 1")
    attendance = attendance_counts_for_day(db, today)
    return {
        "total_students": total_students,
        "attendance": attendance,
        "not_yet_in": max(total_students - attendance["present"] - attendance["late"], 0),
        "week": column_chart(entries_by_day(db, tz_name, today, 7, day_start), ["Granted", "Denied"]),
        "recent_granted": recent_entries(db, "Granted"),
        "recent_failed": recent_entries(db, "Denied"),
        "pending_guests": _count(db, "SELECT COUNT(*) FROM guest_visit_request WHERE status = 'Pending' AND valid_to >= ?", (utc_now_text(),)),
        "security_alerts": db.execute(
            "SELECT alert_id, message, created_at, status FROM alert "
            "WHERE alert_type = 'Unauthorized Attempt' AND status = 'New' "
            "ORDER BY created_at DESC LIMIT 5"
        ).fetchall(),
        "emergency_alerts": db.execute(
            "SELECT a.alert_id, a.message, a.created_at, a.status, s.full_name, s.room_no "
            "FROM alert a LEFT JOIN student s ON s.student_id = a.student_id "
            "WHERE a.alert_type = 'Emergency' ORDER BY a.created_at DESC LIMIT 5"
        ).fetchall(),
    }


def student_dashboard(db, student_id, today):
    today_row = db.execute(
        "SELECT status, local_time FROM attendance WHERE student_id = ? AND local_date = ?",
        (student_id, today),
    ).fetchone()
    month_prefix = today[:7]  # "YYYY-MM"
    summary = {r[0]: r[1] for r in db.execute(
        "SELECT status, COUNT(*) FROM attendance WHERE student_id = ? AND local_date LIKE ? GROUP BY status",
        (student_id, month_prefix + "%"),
    )}
    history = db.execute(
        "SELECT attempted_at, method, result, failure_reason FROM entry_log "
        "WHERE student_id = ? ORDER BY attempted_at DESC, entry_id DESC LIMIT 8",
        (student_id,),
    ).fetchall()
    return {
        "today": today_row,
        "month_present": summary.get("Present", 0),
        "month_late": summary.get("Late", 0),
        "history": history,
    }
