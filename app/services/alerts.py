"""Alerts (SRS FR-11, FR-14, FR-16): student emergencies and automatic unauthorized-attempt alerts.

What an emergency alert does: it is SAVED, shown to wardens, and emailed to the wardens and the configured emergency contacts.
It does NOT call the police or any emergency service. Nothing here contacts anyone outside the configured recipients.
"""
from datetime import timedelta

from ..timeutil import format_local, to_text, utc_now
from . import emailer
from .people import paginate

TYPES = ("Emergency", "Unauthorized Attempt")
STATUSES = ("New", "Acknowledged")

# Which denied attempts count towards the "three failures" rule. POLICY (also in docs/alerts-and-email.md):
#  * Only attempts where a person was in front of the camera and could not be verified: failed blink, face not recognised,
#    or two identities matched equally. These are identity problems, and the person is unknown, so they are counted for the
#    terminal as a whole and never against a student.
#  * NOT counted: no face / too dark / blurry / too far / unusable frames (technical, nobody was examined), nobody
#    enrolled, processing errors, and refusals of a recognised guest for visit rules (expired, used...).
#  * A success in between does not reset the count; the window is a plain "last N minutes".
COUNTED_CODES = ("liveness_failed", "not_recognized", "ambiguous")


# ---------- recipients ----------

def _wardens(db):
    return [r["email"] for r in db.execute("SELECT email FROM warden WHERE is_active = 1")]


def _contacts(db, only_type=None):
    if only_type:
        rows = db.execute("SELECT email FROM emergency_contact WHERE contact_type = ?", (only_type,))
    else:
        rows = db.execute("SELECT email FROM emergency_contact")
    return [r["email"] for r in rows]


# ---------- automatic alert after repeated failures ----------

def check_failed_attempts(db, cfg, now=None):
    """Create an 'Unauthorized Attempt' alert if the failure threshold was reached in the window and no alert was raised for
    this incident yet. Does NOT commit (the caller commits it with the log row). Returns the new alert_id or None."""
    now = now or utc_now()
    window = cfg["ALERT_WINDOW_MINUTES"]
    since = to_text(now - timedelta(minutes=window))
    marks = ",".join("?" * len(COUNTED_CODES))
    count = db.execute(f"SELECT COUNT(*) FROM entry_log WHERE result = 'Denied' AND attempted_at >= ? AND failure_code IN ({marks})",
                       (since, *COUNTED_CODES)).fetchone()[0]
    if count < cfg["ALERT_FAILED_THRESHOLD"]:
        return None
    # One alert per incident: while an alert from the last `window` minutes exists, further failures do not raise another.
    if db.execute("SELECT 1 FROM alert WHERE alert_type = 'Unauthorized Attempt' AND created_at >= ?", (since,)).fetchone():
        return None
    when = to_text(now)
    message = (f"{count} failed entry attempts by unidentified people at the gate terminal within {window} minutes. "
               "The people could not be identified, so no student is named.")
    alert_id = db.execute("INSERT INTO alert (student_id, alert_type, message, created_at, status) VALUES (NULL, 'Unauthorized Attempt', ?, ?, 'New')",
                          (message, when)).lastrowid
    local = format_local(when, cfg["TIMEZONE"], "%d %b %Y, %I:%M %p")
    emailer.enqueue(db, "security", _wardens(db) + _contacts(db, "Security"),
                    f"[{cfg['HOSTEL_NAME']}] Security alert: repeated failed entry attempts",
                    f"{message}\n\nTime: {local}\n\nPlease check the entry logs and acknowledge this alert in the system.", alert_id)
    return alert_id


# ---------- student emergency ----------

def create_emergency(db, student, cfg, now=None):
    """Save an emergency alert for this student. Returns ("created" | "cooldown", alert_id). Commits.
    `student` is the logged-in student's own row: nothing from the browser decides who the alert is for."""
    now = now or utc_now()
    since = to_text(now - timedelta(seconds=cfg["EMERGENCY_COOLDOWN_SECONDS"]))
    recent = db.execute("SELECT alert_id FROM alert WHERE alert_type = 'Emergency' AND student_id = ? AND created_at >= ?",
                        (student["student_id"], since)).fetchone()
    if recent:
        return "cooldown", recent["alert_id"]
    when = to_text(now)
    message = f"Emergency alert from {student['full_name']} (Room {student['room_no']})."
    try:
        alert_id = db.execute("INSERT INTO alert (student_id, alert_type, message, created_at, status) VALUES (?, 'Emergency', ?, ?, 'New')",
                              (student["student_id"], message, when)).lastrowid
        local = format_local(when, cfg["TIMEZONE"], "%d %b %Y, %I:%M %p")
        body = (f"EMERGENCY ALERT\n\nStudent: {student['full_name']}\nRegistration no.: {student['registration_no']}\n"
                f"Room: {student['room_no']}\nPhone: {student['phone']}\nTime: {local}\n\n"
                "The student pressed the emergency button in the hostel system. Please check on the student.")
        emailer.enqueue(db, "emergency", _wardens(db) + _contacts(db),
                        f"[{cfg['HOSTEL_NAME']}] EMERGENCY alert from {student['full_name']}", body, alert_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return "created", alert_id


# ---------- acknowledging ----------

def acknowledge(db, alert_id, warden_id, now_text):
    """'ok' | 'already' | 'missing'. Only a New alert can be acknowledged, and only once."""
    row = db.execute("SELECT status FROM alert WHERE alert_id = ?", (alert_id,)).fetchone()
    if row is None:
        return "missing"
    cur = db.execute("UPDATE alert SET status = 'Acknowledged', acknowledged_by_warden_id = ?, acknowledged_at = ? "
                     "WHERE alert_id = ? AND status = 'New'", (warden_id, now_text, alert_id))
    db.commit()
    return "ok" if cur.rowcount == 1 else "already"


# ---------- reading ----------

_SELECT = ("SELECT a.*, s.full_name AS student_name, s.room_no, s.registration_no, w.full_name AS acknowledged_by "
           "FROM alert a LEFT JOIN student s ON s.student_id = a.student_id "
           "LEFT JOIN warden w ON w.warden_id = a.acknowledged_by_warden_id ")


def search(db, alert_type="", status="", start_utc=None, end_utc=None, page=1):
    where, params = "WHERE 1=1", []
    if alert_type in TYPES:
        where += " AND a.alert_type = ?"
        params.append(alert_type)
    if status in STATUSES:
        where += " AND a.status = ?"
        params.append(status)
    if start_utc and end_utc:
        where += " AND a.created_at >= ? AND a.created_at < ?"
        params += [start_utc, end_utc]
    return paginate(db, f"SELECT COUNT(*) FROM alert a {where}",
                    _SELECT + f"{where} ORDER BY CASE WHEN a.status = 'New' THEN 0 ELSE 1 END, a.created_at DESC, a.alert_id DESC", params, page)


def for_student(db, student_id, limit=10):
    return db.execute(_SELECT + "WHERE a.student_id = ? ORDER BY a.created_at DESC, a.alert_id DESC LIMIT ?", (student_id, limit)).fetchall()


def open_alerts(db, alert_type, limit=5):
    return db.execute(_SELECT + "WHERE a.alert_type = ? AND a.status = 'New' ORDER BY a.created_at DESC LIMIT ?", (alert_type, limit)).fetchall()


def new_count(db):
    return db.execute("SELECT COUNT(*) FROM alert WHERE status = 'New'").fetchone()[0]