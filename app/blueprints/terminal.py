"""The entry terminal (SRS FR-04..FR-08, UC-04): camera page at the hostel gate.

It needs no login (a student must not have to type anything, SRS NFR-06), so it only answers requests that come from
the gate computer itself (TERMINAL_ADDRESSES). The browser sends a short burst of camera frames; the SERVER decides
and records the result. A success message shown by the page can never open anything on its own.
"""
import time

from flask import Blueprint, current_app, jsonify, render_template, request

from ..db import get_db
from ..security import local_only
from ..services import biometrics, emailer, entries, guests, verification
from ..timeutil import format_local, to_text, utc_now, utc_now_text

bp = Blueprint("terminal", __name__, url_prefix="/terminal")

FRAME_COUNT = 30          # pictures per attempt ...
FRAME_INTERVAL_MS = 110   # ... one every 110 ms (about 9 per second), roughly 3.3 seconds in all.
                          # A blink lasts 150-300 ms, so this spacing catches it.


def _no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/")
@local_only
def index():
    return _no_cache(current_app.make_response(render_template(
        "terminal/index.html", title="Entry terminal", frame_count=FRAME_COUNT, interval=FRAME_INTERVAL_MS,
        key_problem=biometrics.key_problem(current_app.config["BIOMETRIC_KEY"]))))


@bp.post("/verify")
@local_only
def verify():
    cfg = current_app.config
    settings = verification.Settings.from_config(cfg)
    frames = [f.read() for f in request.files.getlist("frames")]
    if not settings.min_frames <= len(frames) <= settings.max_frames:
        return _no_cache(jsonify(ok=False, code="bad_request", message="The camera pictures could not be used. Please try again.")), 400
    problem = biometrics.key_problem(cfg["BIOMETRIC_KEY"])
    if problem:
        return _no_cache(jsonify(ok=False, code="no_key", message="The terminal is not configured yet. Contact the administrator.")), 503

    db = get_db()
    started = time.perf_counter()
    try:
        key = cfg["BIOMETRIC_KEY"]
        faces = biometrics.load_templates(db, key, "student", "face")
        eyes = biometrics.load_templates(db, key, "student", "eye") if settings.eye_fallback else {}
        guest_faces = biometrics.load_templates(db, key, "guest", "face")        # only guests with an approved visit
        extra = {"guest_faces": guest_faces} if guest_faces else {}
        decision = verification.verify(frames, faces, eyes, settings, **extra)
        decision = guests.resolve(db, decision, to_text(utc_now()))              # visit rules for a recognised guest
    except Exception:
        current_app.logger.exception("Entry verification failed")
        decision = verification.deny("processing_error")

    try:
        entry_id, attendance_info = entries.record_attempt(db, decision, cfg)
    except Exception:
        # If the attempt cannot be written to the log, nothing is granted: no log row means no entry.
        current_app.logger.exception("Could not record the entry attempt")
        return _no_cache(jsonify(ok=False, code="record_failed",
                                 message="The attempt could not be recorded, so access was not granted. Please try again.")), 500
    current_app.logger.info("Entry attempt #%s: %s via %s in %.1fs", entry_id, decision.code, decision.method,
                            time.perf_counter() - started)

    emailer.kick(current_app._get_current_object())                              # deliver any alert email in the background
    name, kind, valid_until = None, "student", None
    if decision.subject_type == "guest":
        kind = "guest"
        if decision.granted:
            visit = guests.get_request(db, decision.visit_request_id)
            name = visit["guest_name"]
            valid_until = format_local(visit["valid_to"], cfg["TIMEZONE"], "%I:%M %p")
    elif decision.granted:
        name = db.execute("SELECT full_name FROM student WHERE student_id = ?", (decision.student_id,)).fetchone()["full_name"]
    return _no_cache(jsonify(
        ok=True, granted=decision.granted, code=decision.code, message=decision.message, name=name, kind=kind, valid_until=valid_until,
        method=decision.method if decision.granted else None, checks=decision.checks, attendance=attendance_info,
        time=format_local(utc_now_text(), cfg["TIMEZONE"], "%d %b %Y, %I:%M:%S %p")))
