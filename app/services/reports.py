"""Attendance and security reports (SRS FR-12, FR-13, UC-08, UC-09).

Attendance reports are calculated on demand from the attendance table. A day WITHOUT a record counts as absent, but only for
days that have already happened and only from the day the student was registered (students.created_at), so nobody is marked
absent for days before they existed or days that have not come yet. Days are HOSTEL days (a day starts at ATTENDANCE_DAY_START).
The weekly parent email (Phase 10) uses the same calculation.
"""
from datetime import date, datetime, timedelta, timezone

from ..timeutil import FORMAT, day_bounds, hostel_day
from . import emailer, entries
from .people import like_pattern, paginate

REPORT_TYPES = ("daily", "weekly", "monthly")
ORDERS = ("name", "attendance", "late")


# ---------- periods ----------

def previous_week(today_text):
    """The 7 complete hostel days before today: (first_day, last_day) as YYYY-MM-DD."""
    last = date.fromisoformat(today_text) - timedelta(days=1)
    return (last - timedelta(days=6)).isoformat(), last.isoformat()


def calendar_week(day_text):
    """Monday to Sunday of the week containing the day."""
    d = date.fromisoformat(day_text)
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def calendar_month(month_text):
    """First and last day of 'YYYY-MM'."""
    year, month = (int(x) for x in month_text.split("-"))
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def period_for(kind, day_text, week_text, month_text, today):
    """(first_day, last_day, title) for the requested report. Inputs that are missing or invalid fall back to today."""
    if kind == "daily":
        d = day_text or today
        return d, d, date.fromisoformat(d).strftime("Daily report: %A, %d %B %Y")
    if kind == "weekly":
        first, last = calendar_week(week_text or today)
        return first, last, f"Weekly report: {date.fromisoformat(first).strftime('%d %b')} to {date.fromisoformat(last).strftime('%d %b %Y')}"
    first, last = calendar_month(month_text or today[:7])
    return first, last, date.fromisoformat(first).strftime("Monthly report: %B %Y")


def _days(first, last):
    return (date.fromisoformat(last) - date.fromisoformat(first)).days + 1


def _registered_day(created_at, cfg):
    """The hostel day a student was registered, or None for accounts that predate the column."""
    if not created_at:
        return None
    moment = datetime.strptime(created_at, FORMAT).replace(tzinfo=timezone.utc)
    return hostel_day(moment, cfg["TIMEZONE"], cfg["ATTENDANCE_DAY_START"])[0].isoformat()


# ---------- per-student statistics ----------

def student_period_stats(db, student_id, first_day, last_day, registered_on=None):
    """Days present / late / absent for one student. `registered_on` (YYYY-MM-DD) shortens the period for new students."""
    start = max(first_day, registered_on) if registered_on else first_day
    days = max(_days(start, last_day), 0) if start <= last_day else 0
    row = db.execute("SELECT COUNT(*) AS present, SUM(status = 'Late') AS late FROM attendance "
                     "WHERE student_id = ? AND local_date BETWEEN ? AND ?", (student_id, start, last_day)).fetchone()
    present, late = row["present"], row["late"] or 0
    return {"days": days, "days_present": present, "late_entries": late, "days_absent": max(days - present, 0),
            "percentage": round(100.0 * present / days, 1) if days else None}


def attendance_report(db, cfg, first, last, today, kind="weekly", q="", order="name"):
    """Rows for every active student plus totals. Only days up to `today` are counted."""
    through = min(last, today)                                         # days that have not happened yet are not absences
    where, params = "WHERE is_active = 1", []
    if q:
        where += " AND (full_name LIKE ? ESCAPE '\\' OR registration_no LIKE ? ESCAPE '\\' OR room_no LIKE ? ESCAPE '\\')"
        params += [like_pattern(q)] * 3
    students = db.execute(f"SELECT student_id, full_name, registration_no, room_no, created_at FROM student {where} "
                          "ORDER BY full_name COLLATE NOCASE, student_id", params).fetchall()
    records = {}
    if first <= through:
        for r in db.execute("SELECT student_id, local_date, local_time, status FROM attendance WHERE local_date BETWEEN ? AND ?", (first, through)):
            records.setdefault(r["student_id"], []).append(r)

    rows = []
    for s in students:
        registered = _registered_day(s["created_at"], cfg)
        start = max(first, registered) if registered else first
        mine = [r for r in records.get(s["student_id"], []) if r["local_date"] >= start]
        days = _days(start, through) if first <= through and start <= through else 0
        if registered and registered > last:
            continue                                                   # not registered yet in this period
        present = len(mine)
        late = sum(1 for r in mine if r["status"] == "Late")
        row = {"student_id": s["student_id"], "full_name": s["full_name"], "registration_no": s["registration_no"], "room_no": s["room_no"],
               "days": days, "days_present": present, "late_entries": late, "days_absent": max(days - present, 0),
               "percentage": round(100.0 * present / days, 1) if days else None}
        if kind == "daily":
            if mine:
                row["status"], row["first_entry"] = mine[0]["status"], mine[0]["local_time"][:5]
            else:
                row["status"], row["first_entry"] = ("Not entered yet" if first == today else "Absent" if days else "-"), ""
        rows.append(row)

    if order == "attendance":
        rows.sort(key=lambda r: (r["percentage"] is None, r["percentage"] if r["percentage"] is not None else 0, r["full_name"].lower()))
    elif order == "late":
        rows.sort(key=lambda r: (-r["late_entries"], r["full_name"].lower()))
    counted = [r for r in rows if r["days"]]
    total_days = sum(r["days"] for r in counted)
    total_present = sum(r["days_present"] for r in counted)
    summary = {"students": len(rows), "present": total_present - sum(r["late_entries"] for r in counted),
               "late": sum(r["late_entries"] for r in counted), "absent": sum(r["days_absent"] for r in counted),
               "days_present": total_present, "average": round(100.0 * total_present / total_days, 1) if total_days else None,
               "perfect": sum(1 for r in counted if r["percentage"] == 100.0)}
    return {"rows": rows, "summary": summary, "first": first, "last": last, "through": through,
            "elapsed_days": max(_days(first, through), 0) if first <= through else 0, "partial": last > today, "future": first > today}


# ---------- security report ----------

def security_report(db, cfg, first, last):
    start, end = day_bounds(first, last, cfg["TIMEZONE"], cfg["ATTENDANCE_DAY_START"])
    attempts = db.execute("SELECT result, method, subject_type, needs_review, failure_code, attempted_at FROM entry_log WHERE attempted_at >= ? AND attempted_at < ?",
                          (start, end)).fetchall()
    granted = [a for a in attempts if a["result"] == "Granted"]
    denied = [a for a in attempts if a["result"] == "Denied"]
    reasons = {}
    for a in denied:
        label = entries.FAILURE_LABELS.get(a["failure_code"], a["failure_code"] or "Other")
        reasons[label] = reasons.get(label, 0) + 1
    by_day = {}
    for a in attempts:
        moment = datetime.strptime(a["attempted_at"], FORMAT).replace(tzinfo=timezone.utc)
        day = hostel_day(moment, cfg["TIMEZONE"], cfg["ATTENDANCE_DAY_START"])[0]
        by_day.setdefault(day, [0, 0])[0 if a["result"] == "Granted" else 1] += 1
    days = [date.fromisoformat(first) + timedelta(days=i) for i in range(_days(first, last))]
    chart_days = [{"label": d.strftime("%a") if len(days) <= 7 else str(d.day), "full": d.strftime("%a %d %b"), "values": by_day.get(d, [0, 0])} for d in days]

    alert_rows = db.execute("SELECT a.alert_id, a.alert_type, a.message, a.created_at, a.status, s.full_name AS student_name, s.room_no "
                            "FROM alert a LEFT JOIN student s ON s.student_id = a.student_id WHERE a.created_at >= ? AND a.created_at < ? "
                            "ORDER BY a.created_at DESC", (start, end)).fetchall()

    def alert_counts(kind):
        mine = [a for a in alert_rows if a["alert_type"] == kind]
        return {"total": len(mine), "acknowledged": sum(a["status"] == "Acknowledged" for a in mine), "open": sum(a["status"] == "New" for a in mine)}

    exits = db.execute("SELECT subject_type, COUNT(*) AS n FROM exit_record WHERE recorded_at >= ? AND recorded_at < ? GROUP BY subject_type", (start, end)).fetchall()
    visits = db.execute("SELECT status, COUNT(*) AS n FROM guest_visit_request WHERE visit_date BETWEEN ? AND ? GROUP BY status", (first, last)).fetchall()
    visit_counts = {r["status"]: r["n"] for r in visits}
    return {
        "attempts": len(attempts), "granted": len(granted), "denied": len(denied),
        "student_entries": sum(a["subject_type"] == "student" for a in granted), "guest_entries": sum(a["subject_type"] == "guest" for a in granted),
        "face": sum(a["method"] == "Face" for a in granted), "eye": sum(a["method"] == "Eye-Region" for a in granted),
        "review": sum(bool(a["needs_review"]) for a in attempts),
        "reasons": sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])),
        "chart_days": chart_days, "alerts": alert_rows[:20], "alerts_total": len(alert_rows),
        "emergency": alert_counts("Emergency"), "unauthorized": alert_counts("Unauthorized Attempt"),
        "exits": {r["subject_type"]: r["n"] for r in exits},
        "visits": {"total": sum(visit_counts.values()), "approved": visit_counts.get("Approved", 0), "rejected": visit_counts.get("Rejected", 0),
                   "pending": visit_counts.get("Pending", 0)},
    }


# ---------- the saved weekly parent reports ----------

def parent_reports(db, first, last, status="", q="", page=1):
    where, params = "WHERE r.period_end BETWEEN ? AND ?", [first, last]
    if status in ("Pending", "Sent", "Failed"):
        where += " AND r.email_status = ?"
        params.append(status)
    if q:
        where += " AND (s.full_name LIKE ? ESCAPE '\\' OR s.registration_no LIKE ? ESCAPE '\\')"
        params += [like_pattern(q)] * 2
    return paginate(db, f"SELECT COUNT(*) FROM report r JOIN student s ON s.student_id = r.student_id {where}",
                    "SELECT r.*, s.full_name, s.registration_no, s.parent_email FROM report r JOIN student s ON s.student_id = r.student_id "
                    f"{where} ORDER BY r.period_end DESC, s.full_name COLLATE NOCASE", params, page)


# ---------- the weekly parent email (Phase 10) ----------

def _email_text(student, stats, first_day, last_day, hostel):
    return (f"Dear {student['parent_name']},\n\n"
            f"This is the weekly attendance report for {student['full_name']} ({student['registration_no']}) at {hostel}.\n\n"
            f"Period: {first_day} to {last_day}\n"
            f"Days present: {stats['days_present']} of {stats['days']} ({stats['percentage']}%)\n"
            f"Late entries: {stats['late_entries']}\n"
            f"Days absent: {stats['days_absent']}\n\n"
            "A day counts as present when the student entered the hostel that day. This message was sent automatically.\n")


def generate_weekly(db, cfg, today_text, now_text):
    """Create one report per active student for the previous week and queue the parent's email. Safe to run twice: a student's
    report for a period is created only once. A student registered after the period ended gets none. Commits."""
    first, last = previous_week(today_text)
    created = existing = 0
    students = db.execute("SELECT student_id, full_name, registration_no, parent_name, parent_email, created_at FROM student WHERE is_active = 1 "
                          "ORDER BY student_id").fetchall()
    try:
        for s in students:
            registered = _registered_day(s["created_at"], cfg)
            stats = student_period_stats(db, s["student_id"], first, last, registered)
            if stats["days"] == 0:
                continue
            cur = db.execute(
                "INSERT OR IGNORE INTO report (student_id, period_start, period_end, days_present, late_entries, attendance_percentage, "
                "email_status, generated_date) VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?)",
                (s["student_id"], first, last, stats["days_present"], stats["late_entries"], stats["percentage"], today_text))
            if cur.rowcount == 0:
                existing += 1
                continue
            created += 1
            emailer.enqueue(db, "weekly_report", [s["parent_email"]],
                            f"[{cfg['HOSTEL_NAME']}] Weekly attendance report: {s['full_name']} ({first} to {last})",
                            _email_text(s, stats, first, last, cfg["HOSTEL_NAME"]), cur.lastrowid)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"created": created, "existing": existing, "period": (first, last)}