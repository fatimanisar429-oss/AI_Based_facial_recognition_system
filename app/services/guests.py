"""Guest visits: requests, warden decisions, and the rules that decide whether a guest may enter.

A guest visit works like this
  1. A STUDENT asks for a visit (guest name and contact, date, time window, purpose).
  2. A WARDEN approves or rejects it and says how many entries are allowed (1 by default = one-time entry).
  3. At the gate a warden registers the guest's face (only for an approved visit).
  4. At the entry terminal the guest passes the same blink + face checks as a student. The server then also
     requires: approved, inside the time window, entries left, host student still active.
  5. Using an entry is ONE atomic database update, so the same entry can never be spent twice.
Guest entries never create student attendance. Times are stored as UTC text, like everywhere else.
"""
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..timeutil import is_hhmm, parse_hhmm, to_text, utc_now_text
from .people import like_pattern, paginate
from .verification import Decision

PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9 \-]*$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
STATUS_FILTERS = ("Pending", "Approved", "Rejected", "Expired", "Used")

# Why a recognised guest was refused. Shown to the guest/warden at the terminal and kept in the entry log.
VISIT_REFUSALS = {
    "guest_no_visit": "No visit request was found for this guest.",
    "guest_pending": "The visit has not been approved yet.",
    "guest_rejected": "The visit request was rejected.",
    "guest_not_yet": "The visit is approved but its time window has not started.",
    "guest_expired": "The visit has expired.",
    "guest_used": "The approved entry has already been used.",
    "guest_host_inactive": "The host student's account is not active.",
}


# ---------- status ----------

def display_status(row, now_text):
    """Pending / Approved / Rejected, plus the derived Expired and Used."""
    if row["status"] == "Rejected":
        return "Rejected"
    expired = row["valid_to"] < now_text
    if row["status"] == "Pending":
        return "Expired" if expired else "Pending"
    if row["entries_used"] >= row["max_entries"]:
        return "Used"
    return "Expired" if expired else "Approved"


# ---------- creating a request ----------

def _local_to_utc(day, hhmm, tz_name):
    local = datetime.combine(date.fromisoformat(day), parse_hhmm(hhmm), tzinfo=ZoneInfo(tz_name))
    return to_text(local)


def validate_request(db, form, student_id, today, now_text, cfg):
    """Returns (clean values, errors). `today` is the hostel date (YYYY-MM-DD). The host student always comes from the
    login session; nothing in the form can name another student."""
    clean, errors = {}, {}
    clean["previous_guest_id"] = None
    previous = (form.get("previous_guest_id") or "").strip()
    if previous:
        row = db.execute("SELECT * FROM guest_profile WHERE guest_id = ? AND host_student_id = ?",
                         (previous if previous.isdigit() else -1, student_id)).fetchone()
        if row is None:
            errors["previous_guest_id"] = "Choose one of your previous guests from the list."
        else:
            clean["previous_guest_id"] = row["guest_id"]
            clean["guest_name"], clean["guest_contact"] = row["full_name"], row["contact"]

    if clean["previous_guest_id"] is None:
        clean["guest_name"] = (form.get("guest_name") or "").strip()
        if not 2 <= len(clean["guest_name"]) <= 100 or not any(c.isalpha() for c in clean["guest_name"]):
            errors["guest_name"] = "Enter the guest's full name (2-100 letters)."
        clean["guest_contact"] = (form.get("guest_contact") or "").strip()
        contact = clean["guest_contact"]
        digits = re.sub(r"\D", "", contact)
        phone_ok = PHONE_PATTERN.match(contact) and 7 <= len(digits) <= 15
        if not (phone_ok or (len(contact) <= 100 and EMAIL_PATTERN.match(contact))):
            errors["guest_contact"] = "Enter a phone number (7-15 digits) or an email address."

    clean["purpose"] = (form.get("purpose") or "").strip()
    if not 3 <= len(clean["purpose"]) <= 200:
        errors["purpose"] = "Describe the purpose of the visit (3-200 characters)."

    clean["visit_date"] = (form.get("visit_date") or "").strip()
    try:
        visit = date.fromisoformat(clean["visit_date"])
    except ValueError:
        visit = None
        errors["visit_date"] = "Choose the visit date."
    if visit is not None:
        first = date.fromisoformat(today)
        if visit < first:
            errors["visit_date"] = "The visit date cannot be in the past."
        elif visit > first + timedelta(days=cfg["GUEST_MAX_DAYS_AHEAD"]):
            errors["visit_date"] = f"Visits can be requested up to {cfg['GUEST_MAX_DAYS_AHEAD']} days ahead."

    clean["from_time"], clean["to_time"] = (form.get("from_time") or "").strip(), (form.get("to_time") or "").strip()
    for field, label in (("from_time", "start time"), ("to_time", "end time")):
        if not is_hhmm(clean[field]):
            errors[field] = f"Enter the {label} as HH:MM (24-hour)."

    if "from_time" not in errors and "to_time" not in errors:
        start, end = parse_hhmm(clean["from_time"]), parse_hhmm(clean["to_time"])
        hours = (datetime.combine(date.min, end) - datetime.combine(date.min, start)).total_seconds() / 3600
        if hours <= 0:
            errors["to_time"] = "The end time must be after the start time (same day)."
        elif hours > cfg["GUEST_MAX_HOURS"]:
            errors["to_time"] = f"A visit can last at most {cfg['GUEST_MAX_HOURS']} hours."
        elif "visit_date" not in errors:
            clean["valid_from"] = _local_to_utc(clean["visit_date"], clean["from_time"], cfg["TIMEZONE"])
            clean["valid_to"] = _local_to_utc(clean["visit_date"], clean["to_time"], cfg["TIMEZONE"])
            if clean["valid_to"] <= now_text:
                errors["to_time"] = "That time window has already ended."

    if not errors:
        open_requests = db.execute(
            "SELECT COUNT(*) FROM guest_visit_request WHERE student_id = ? AND status = 'Pending' AND valid_to >= ?",
            (student_id, now_text)).fetchone()[0]
        if open_requests >= cfg["GUEST_MAX_PENDING"]:
            errors["purpose"] = f"You already have {open_requests} requests waiting. Wait for a decision before sending more."
    return clean, errors


def create_request(db, student_id, clean, now_text):
    """Create the guest profile (unless an earlier guest was chosen) and a Pending request. Returns (request_id, guest_id)."""
    guest_id = clean["previous_guest_id"]
    try:
        if guest_id is None:
            guest_id = db.execute("INSERT INTO guest_profile (full_name, contact, host_student_id, created_at) VALUES (?, ?, ?, ?)",
                                  (clean["guest_name"], clean["guest_contact"], student_id, now_text)).lastrowid
        request_id = db.execute(
            "INSERT INTO guest_visit_request (student_id, guest_id, visit_date, valid_from, valid_to, purpose, status, max_entries, "
            "entries_used, created_at) VALUES (?, ?, ?, ?, ?, ?, 'Pending', 1, 0, ?)",
            (student_id, guest_id, clean["visit_date"], clean["valid_from"], clean["valid_to"], clean["purpose"], now_text)).lastrowid
        db.commit()
    except Exception:
        db.rollback()
        raise
    return request_id, guest_id


# ---------- reading ----------

_REQUEST_SELECT = (
    "SELECT r.*, g.full_name AS guest_name, g.contact AS guest_contact, s.full_name AS host_name, s.registration_no AS host_reg, "
    "s.room_no AS host_room, s.phone AS host_phone, s.is_active AS host_active, w.full_name AS decided_by, "
    "(SELECT COUNT(*) FROM biometric_profile b WHERE b.owner_type = 'guest' AND b.owner_id = r.guest_id AND b.kind = 'face') AS face_samples "
    "FROM guest_visit_request r JOIN guest_profile g ON g.guest_id = r.guest_id JOIN student s ON s.student_id = r.student_id "
    "LEFT JOIN warden w ON w.warden_id = r.decided_by_warden_id ")


def get_request(db, request_id):
    return db.execute(_REQUEST_SELECT + "WHERE r.request_id = ?", (request_id,)).fetchone()


def for_student(db, student_id, limit=50):
    return db.execute(_REQUEST_SELECT + "WHERE r.student_id = ? ORDER BY r.created_at DESC, r.request_id DESC LIMIT ?",
                      (student_id, limit)).fetchall()


def previous_guests(db, student_id):
    return db.execute("SELECT guest_id, full_name, contact FROM guest_profile WHERE host_student_id = ? ORDER BY full_name COLLATE NOCASE",
                      (student_id,)).fetchall()


def _status_clause(status, now_text):
    """SQL for the derived statuses. Returns (sql, params)."""
    return {
        "Pending": (" AND r.status = 'Pending' AND r.valid_to >= ?", [now_text]),
        "Approved": (" AND r.status = 'Approved' AND r.entries_used < r.max_entries AND r.valid_to >= ?", [now_text]),
        "Rejected": (" AND r.status = 'Rejected'", []),
        "Expired": (" AND r.valid_to < ? AND (r.status = 'Pending' OR (r.status = 'Approved' AND r.entries_used < r.max_entries))", [now_text]),
        "Used": (" AND r.status = 'Approved' AND r.entries_used >= r.max_entries", []),
    }.get(status, ("", []))


def search(db, status, first_day, last_day, q, now_text, page=1):
    clause, extra = _status_clause(status, now_text)
    where = "WHERE r.visit_date BETWEEN ? AND ?" + clause
    params = [first_day, last_day] + extra
    if q:
        where += (" AND (g.full_name LIKE ? ESCAPE '\\' OR s.full_name LIKE ? ESCAPE '\\' OR s.registration_no LIKE ? ESCAPE '\\')")
        params += [like_pattern(q)] * 3
    return paginate(
        db,
        "SELECT COUNT(*) FROM guest_visit_request r JOIN guest_profile g ON g.guest_id = r.guest_id "
        f"JOIN student s ON s.student_id = r.student_id {where}",
        _REQUEST_SELECT + f"{where} ORDER BY CASE WHEN r.status = 'Pending' THEN 0 ELSE 1 END, r.valid_from DESC, r.request_id DESC",
        params, page)


def pending_count(db, now_text):
    return db.execute("SELECT COUNT(*) FROM guest_visit_request WHERE status = 'Pending' AND valid_to >= ?", (now_text,)).fetchone()[0]


def pending_list(db, now_text, limit=5):
    return db.execute(_REQUEST_SELECT + "WHERE r.status = 'Pending' AND r.valid_to >= ? ORDER BY r.created_at LIMIT ?",
                      (now_text, limit)).fetchall()


def request_events(db, request_id):
    """Entry attempts and exits belonging to one visit, oldest first."""
    entries = db.execute("SELECT attempted_at AS at, result, method FROM entry_log WHERE visit_request_id = ? ORDER BY attempted_at",
                         (request_id,)).fetchall()
    guest_id = db.execute("SELECT guest_id FROM guest_visit_request WHERE request_id = ?", (request_id,)).fetchone()
    exits = []
    if guest_id:
        exits = db.execute("SELECT x.recorded_at AS at, COALESCE(w.full_name, a.full_name) AS by_name FROM exit_record x "
                           "LEFT JOIN warden w ON x.recorded_by_role = 'warden' AND w.warden_id = x.recorded_by_id "
                           "LEFT JOIN admin a ON x.recorded_by_role = 'admin' AND a.admin_id = x.recorded_by_id "
                           "WHERE x.subject_type = 'guest' AND x.subject_id = ? ORDER BY x.recorded_at", (guest_id[0],)).fetchall()
    return entries, exits


# ---------- the warden's decision ----------

def decide(db, request_id, warden_id, approve, entries_allowed, now_text, max_entries):
    """Approve or reject a Pending request. Returns 'approved' | 'rejected' | 'missing' | 'not_pending' | 'expired' | 'bad_entries'.
    The UPDATE only touches a request that is still Pending, so two wardens cannot both decide it."""
    row = db.execute("SELECT status, valid_to FROM guest_visit_request WHERE request_id = ?", (request_id,)).fetchone()
    if row is None:
        return "missing"
    if row["status"] != "Pending":
        return "not_pending"
    if approve:
        if row["valid_to"] < now_text:
            return "expired"
        if not 1 <= entries_allowed <= max_entries:
            return "bad_entries"
    cur = db.execute(
        "UPDATE guest_visit_request SET status = ?, decided_by_warden_id = ?, decided_at = ?, max_entries = ? "
        "WHERE request_id = ? AND status = 'Pending'",
        ("Approved" if approve else "Rejected", warden_id, now_text, entries_allowed if approve else 1, request_id))
    db.commit()
    if cur.rowcount != 1:
        return "not_pending"
    return "approved" if approve else "rejected"


# ---------- may this recognised guest enter right now? ----------

def check_visit(db, guest_id, now_text):
    """Returns (request_row_or_None, refusal_code_or_None). Read-only. Code None means a usable visit exists."""
    rows = db.execute(_REQUEST_SELECT + "WHERE r.guest_id = ? ORDER BY r.valid_from", (guest_id,)).fetchall()
    approved = [r for r in rows if r["status"] == "Approved"]
    usable = [r for r in approved if r["entries_used"] < r["max_entries"] and r["valid_from"] <= now_text <= r["valid_to"]]
    if usable:
        row = usable[0]
        return (row, "guest_host_inactive") if not row["host_active"] else (row, None)

    def first(seq):
        return seq[0] if seq else None

    used_now = [r for r in approved if r["entries_used"] >= r["max_entries"] and r["valid_from"] <= now_text <= r["valid_to"]]
    not_yet = [r for r in approved if r["entries_used"] < r["max_entries"] and r["valid_from"] > now_text]
    expired = [r for r in approved if r["entries_used"] < r["max_entries"] and r["valid_to"] < now_text]
    pending = [r for r in rows if r["status"] == "Pending" and r["valid_to"] >= now_text]
    rejected = [r for r in rows if r["status"] == "Rejected"]
    if used_now:
        return first(used_now), "guest_used"
    if not_yet:
        return first(not_yet), "guest_not_yet"
    if expired:
        return expired[-1], "guest_expired"
    if pending:
        return first(pending), "guest_pending"
    if rejected:
        return rejected[-1], "guest_rejected"
    return None, "guest_no_visit"


def consume_entry(db, request_id, now_text):
    """Spend one entry of an approved visit. True only if it was still valid; the check and the update are one statement,
    so two simultaneous attempts cannot both succeed. The caller commits."""
    cur = db.execute(
        "UPDATE guest_visit_request SET entries_used = entries_used + 1 WHERE request_id = ? AND status = 'Approved' "
        "AND entries_used < max_entries AND valid_from <= ? AND valid_to >= ?", (request_id, now_text, now_text))
    return cur.rowcount == 1


def resolve(db, decision, now_text):
    """Turn 'this face is guest X' (guest_candidate) into a real grant or a refusal by applying the visit rules.
    Any other decision is returned unchanged. A refusal here keeps the guest attached to the log row: the face was
    recognised and the blink passed, so the person IS known; only the visit rules said no."""
    if decision.code != "guest_candidate":
        return decision
    row, refusal = check_visit(db, decision.guest_id, now_text)
    common = dict(method="Face", distance=decision.distance, liveness=decision.liveness, subject_type="guest",
                  guest_id=decision.guest_id, visit_request_id=row["request_id"] if row else None)
    if refusal:
        return Decision(False, refusal, VISIT_REFUSALS[refusal], checks={"face": "ok", "liveness": "ok", "recognition": "ok"}, **common)
    return Decision(True, "granted", "Guest access granted.", checks={"face": "ok", "liveness": "ok", "recognition": "ok"}, **common)


# ---------- reusing a returning guest ----------

def reuse_guest(db, new_guest_id, existing_guest_id):
    """The face just registered belongs to a guest we already know: point the visit at that profile and drop the empty new one."""
    db.execute("UPDATE guest_visit_request SET guest_id = ? WHERE guest_id = ?", (existing_guest_id, new_guest_id))
    leftover = db.execute("SELECT (SELECT COUNT(*) FROM guest_visit_request WHERE guest_id = ?) + "
                          "(SELECT COUNT(*) FROM entry_log WHERE guest_id = ?) + "
                          "(SELECT COUNT(*) FROM biometric_profile WHERE owner_type = 'guest' AND owner_id = ?)",
                          (new_guest_id, new_guest_id, new_guest_id)).fetchone()[0]
    if leftover == 0:
        db.execute("DELETE FROM guest_profile WHERE guest_id = ?", (new_guest_id,))
    db.commit()


def guest_needs_enrollment_request(db, guest_id):
    """The warden may only register a face for a guest who has an approved visit."""
    return db.execute("SELECT 1 FROM guest_visit_request WHERE guest_id = ? AND status = 'Approved'", (guest_id,)).fetchone() is not None
