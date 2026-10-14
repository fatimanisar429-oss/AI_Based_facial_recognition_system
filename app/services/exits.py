"""Manual exit recording (project decision: exits are recorded BY STAFF, they are not detected automatically).

The system does not know when someone physically leaves; a warden or admin presses "Record exit". Guests use the
same table in a later phase (subject_type = 'guest')."""
from datetime import timedelta

from ..timeutil import to_text, utc_now, utc_now_text
from .people import like_pattern, paginate

DUPLICATE_WINDOW_SECONDS = 60       # pressing the button twice must not create two exits


def record_student_exit(db, student_id, by_role, by_id):
    """Returns ("saved" | "duplicate" | "inactive" | "missing", exit_id_or_None)."""
    student = db.execute("SELECT is_active FROM student WHERE student_id = ?", (student_id,)).fetchone()
    if student is None:
        return "missing", None
    if not student["is_active"]:
        return "inactive", None
    since = to_text(utc_now() - timedelta(seconds=DUPLICATE_WINDOW_SECONDS))
    recent = db.execute("SELECT 1 FROM exit_record WHERE subject_type = 'student' AND subject_id = ? AND recorded_at >= ?",
                        (student_id, since)).fetchone()
    if recent:
        return "duplicate", None
    cur = db.execute(
        "INSERT INTO exit_record (subject_type, subject_id, recorded_by_role, recorded_by_id, recorded_at) "
        "VALUES ('student', ?, ?, ?, ?)", (student_id, by_role, by_id, utc_now_text()))
    db.commit()
    return "saved", cur.lastrowid


GUEST_INSIDE_HOURS = 48             # a guest whose last entry is older than this is not listed as "inside"


def guests_inside(db):
    """Guests whose latest granted entry is more recent than their latest recorded exit."""
    since = to_text(utc_now() - timedelta(hours=GUEST_INSIDE_HOURS))
    rows = db.execute(
        "SELECT g.guest_id, g.full_name, h.full_name AS host_name, MAX(e.attempted_at) AS last_in FROM entry_log e "
        "JOIN guest_profile g ON g.guest_id = e.guest_id JOIN student h ON h.student_id = g.host_student_id "
        "WHERE e.subject_type = 'guest' AND e.result = 'Granted' AND e.attempted_at >= ? GROUP BY g.guest_id", (since,)).fetchall()
    inside = []
    for r in rows:
        last_out = db.execute("SELECT MAX(recorded_at) FROM exit_record WHERE subject_type = 'guest' AND subject_id = ?",
                              (r["guest_id"],)).fetchone()[0]
        if last_out is None or last_out < r["last_in"]:
            inside.append(r)
    return sorted(inside, key=lambda r: r["last_in"], reverse=True)


def record_guest_exit(db, guest_id, by_role, by_id):
    """Returns ("saved" | "duplicate" | "not_inside" | "missing", exit_id_or_None). Only a guest who is inside can leave."""
    if db.execute("SELECT 1 FROM guest_profile WHERE guest_id = ?", (guest_id,)).fetchone() is None:
        return "missing", None
    since = to_text(utc_now() - timedelta(seconds=DUPLICATE_WINDOW_SECONDS))
    if db.execute("SELECT 1 FROM exit_record WHERE subject_type = 'guest' AND subject_id = ? AND recorded_at >= ?",
                  (guest_id, since)).fetchone():
        return "duplicate", None
    if guest_id not in {r["guest_id"] for r in guests_inside(db)}:
        return "not_inside", None
    cur = db.execute(
        "INSERT INTO exit_record (subject_type, subject_id, recorded_by_role, recorded_by_id, recorded_at) "
        "VALUES ('guest', ?, ?, ?, ?)", (guest_id, by_role, by_id, utc_now_text()))
    db.commit()
    return "saved", cur.lastrowid


def search(db, start_utc, end_utc, q="", page=1):
    where, params = "WHERE x.recorded_at >= ? AND x.recorded_at < ?", [start_utc, end_utc]
    if q:
        where += " AND (s.full_name LIKE ? ESCAPE '\\' OR s.registration_no LIKE ? ESCAPE '\\' OR g.full_name LIKE ? ESCAPE '\\')"
        params += [like_pattern(q)] * 3
    base = ("FROM exit_record x LEFT JOIN student s ON x.subject_type = 'student' AND s.student_id = x.subject_id "
            "LEFT JOIN guest_profile g ON x.subject_type = 'guest' AND g.guest_id = x.subject_id " + where)
    return paginate(
        db, f"SELECT COUNT(*) {base}",
        "SELECT x.exit_id, x.recorded_at, x.subject_type, x.recorded_by_role, x.recorded_by_id, "
        "COALESCE(s.full_name, g.full_name, 'Unknown') AS person, s.registration_no, "
        "COALESCE((SELECT full_name FROM admin WHERE x.recorded_by_role = 'admin' AND admin_id = x.recorded_by_id), "
        "(SELECT full_name FROM warden WHERE x.recorded_by_role = 'warden' AND warden_id = x.recorded_by_id)) AS recorded_by "
        f"{base} ORDER BY x.recorded_at DESC, x.exit_id DESC", params, page)
