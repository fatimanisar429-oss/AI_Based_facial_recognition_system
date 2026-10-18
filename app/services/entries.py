"""The entry log: one row for every verification attempt (SRS FR-08). Rows are only ever added: the database itself
refuses to change or delete them (triggers in schema.sql).

Privacy rule: a DENIED attempt is never linked to a student. When a face is not recognised, or liveness fails,
we do not know who stood in front of the camera, so student_id stays empty. (A photo of student X must not be
recorded as "X failed".)
"""
from ..timeutil import to_text, utc_now
from . import alerts, attendance, guests, verification
from .people import like_pattern, paginate

# Short wording for wardens reading the log (the terminal shows the person a different, gentler message).
FAILURE_LABELS = {
    "liveness_failed": "Liveness check failed (no blink detected)",
    "not_recognized": "Face not recognised",
    "ambiguous": "Inconclusive: two students matched closely",
    "no_face": "No face detected",
    "face_covered": "Face covered (eyes only): needs the warden to verify in person",
    "multiple_faces": "More than one face in view",
    "face_too_small": "Face too far from the camera",
    "too_dark": "Image too dark",
    "too_bright": "Image too bright",
    "blurry": "Image blurry",
    "bad_frames": "Camera pictures not usable",
    "no_enrolled": "No students enrolled",
    "processing_error": "Processing error",
    "guest_no_visit": "Guest recognised, but no visit request found",
    "guest_pending": "Guest recognised, but the visit is not approved yet",
    "guest_rejected": "Guest recognised, but the visit was rejected",
    "guest_not_yet": "Guest recognised, but the visit window has not started",
    "guest_expired": "Guest recognised, but the visit has expired",
    "guest_used": "Guest recognised, but the approved entry was already used",
    "guest_host_inactive": "Guest recognised, but the host student is not active",
}


def record_attempt(db, decision, cfg):
    """Write one entry_log row for this decision.

    * Student granted: also records attendance (first entry of the hostel day only), in the SAME transaction.
    * Guest granted: spends one entry of the approved visit in the SAME transaction (if that fails because the entry was just
      used, the decision is changed to a refusal IN PLACE, so the caller sees what was really recorded). No attendance.
    * Denied: never linked to a student. A denial for a recognised GUEST whose visit rules said no keeps the guest, because
      recognition and liveness succeeded; failed recognition or liveness is never attributed to anyone.

    Returns (entry_id, attendance_info_or_None)."""
    moment = utc_now()
    now_text = to_text(moment)
    is_guest = decision.subject_type == "guest" and decision.guest_id is not None
    try:
        if decision.granted and is_guest and not guests.consume_entry(db, decision.visit_request_id, now_text):
            lost = verification.deny("guest_used", guests.VISIT_REFUSALS["guest_used"], method="Face", distance=decision.distance,
                                     liveness=decision.liveness, subject_type="guest", guest_id=decision.guest_id,
                                     visit_request_id=decision.visit_request_id,
                                     checks={"face": "ok", "liveness": "ok", "recognition": "ok"})
            decision.__dict__.update(lost.__dict__)
        granted = decision.granted
        reason = None if granted else FAILURE_LABELS.get(decision.code, decision.message)
        cur = db.execute(
            "INSERT INTO entry_log (subject_type, student_id, guest_id, visit_request_id, attempted_at, method, result, score, "
            "failure_reason, door_status, needs_review, failure_code, liveness) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("guest" if is_guest else ("student" if granted else None),
             decision.student_id if (granted and not is_guest) else None,
             decision.guest_id if is_guest else None,
             decision.visit_request_id if is_guest else None,
             now_text,
             decision.method if (granted or is_guest) else "None",
             "Granted" if granted else "Denied",
             round(decision.distance, 4) if decision.distance is not None else None,
             reason,
             "Unlocked" if granted else "Locked",
             1 if decision.needs_review else 0,
             None if granted else decision.code,
             decision.liveness))
        info = attendance.mark_entry(db, decision.student_id, moment, cfg) if (granted and not is_guest) else None
        if not granted and decision.code in alerts.COUNTED_CODES:
            alerts.check_failed_attempts(db, cfg, moment)          # same transaction: the log row and its alert stand or fall together
        db.commit()
    except Exception:
        db.rollback()
        raise
    return cur.lastrowid, info


# ---------- reading the log (staff pages) ----------

def search(db, start_utc, end_utc, result="", method="", review=False, q="", page=1):
    where, params = "WHERE e.attempted_at >= ? AND e.attempted_at < ?", [start_utc, end_utc]
    if result in ("Granted", "Denied"):
        where += " AND e.result = ?"
        params.append(result)
    if method in ("Face", "Eye-Region", "None"):
        where += " AND e.method = ?"
        params.append(method)
    if review:
        where += " AND e.needs_review = 1"
    if q:
        where += (" AND (s.full_name LIKE ? ESCAPE '\\' OR s.registration_no LIKE ? ESCAPE '\\'"
                  " OR g.full_name LIKE ? ESCAPE '\\')")
        params += [like_pattern(q)] * 3
    base = ("FROM entry_log e LEFT JOIN student s ON s.student_id = e.student_id "
            "LEFT JOIN guest_profile g ON g.guest_id = e.guest_id " + where)
    out = paginate(
        db, f"SELECT COUNT(*) {base}",
        "SELECT e.entry_id, e.attempted_at, e.subject_type, e.method, e.result, e.score, e.failure_reason, e.liveness, "
        "e.needs_review, s.student_id, s.registration_no, COALESCE(s.full_name, g.full_name, 'Unidentified') AS person "
        f"{base} ORDER BY e.attempted_at DESC, e.entry_id DESC", params, page)
    counts = {r[0]: r[1] for r in db.execute(f"SELECT e.result, COUNT(*) {base} GROUP BY e.result", params)}
    out["granted"], out["denied"] = counts.get("Granted", 0), counts.get("Denied", 0)
    out["review"] = db.execute(f"SELECT COUNT(*) {base} AND e.needs_review = 1", params).fetchone()[0]
    return out
