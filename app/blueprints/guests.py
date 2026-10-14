"""Guest visits (project requirement, not in the original SRS).

Student : ask for a visit, see the status of their own requests.
Warden  : approve or reject (and say how many entries are allowed), register the guest's face, record the guest's exit.
Admin   : can view everything and manage the guest's face data, but does not decide requests.
"""
from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for)

from ..db import get_db
from ..security import role_required
from ..services import biometrics, guests
from ..timeutil import parse_date, utc_now_text
from . import enrollment, filters

bp = Blueprint("guests", __name__, url_prefix="/guests")


def _now():
    return utc_now_text()


# =============================== student pages ===============================

@bp.get("/")
@role_required("student")
def mine():
    rows = guests.for_student(get_db(), g.user["id"])
    now = _now()
    return render_template("guests/mine.html", title="Guest requests", rows=rows, now=now, status_of=guests.display_status)


def _form(values, errors, status=200):
    db = get_db()
    return render_template("guests/new.html", title="Request a guest visit", v=values, errors=errors,
                           previous=guests.previous_guests(db, g.user["id"]), today=filters.today(),
                           cfg=current_app.config), status


@bp.route("/new", methods=["GET", "POST"])
@role_required("student")
def new():
    if request.method == "GET":
        return _form({}, {})
    db, cfg = get_db(), current_app.config
    now = _now()
    clean, errors = guests.validate_request(db, request.form, g.user["id"], filters.today(), now, cfg)
    if errors:
        return _form({**request.form.to_dict(), **{k: v for k, v in clean.items() if k in ("guest_name", "guest_contact")}}, errors, 400)
    request_id, _ = guests.create_request(db, g.user["id"], clean, now)
    current_app.logger.info("Guest visit requested: request_id=%s by student_id=%s", request_id, g.user["id"])
    flash("Your guest visit request was sent to the warden.", "success")
    return redirect(url_for("guests.mine_detail", request_id=request_id))


@bp.get("/<int:request_id>")
@role_required("student")
def mine_detail(request_id):
    row = guests.get_request(get_db(), request_id)
    if row is None or row["student_id"] != g.user["id"]:           # someone else's request looks exactly like a missing one
        abort(404)
    return _detail(row, staff=False)


# ================================ staff pages ================================

@bp.get("/requests")
@role_required("admin", "warden")
def requests_list():
    first, last = filters.date_range(request.args, default_days=0)
    if "from" not in request.args and "to" not in request.args:      # by default show a wide window so pending items are not missed
        first, last = "2000-01-01", "2100-01-01"
    status = request.args.get("status", "Pending")
    status = status if status in guests.STATUS_FILTERS else ("" if request.args.get("status") == "all" else "Pending")
    q = request.args.get("q", "").strip()[:100]
    page = request.args.get("page", 1, type=int)
    now = _now()
    result = guests.search(get_db(), status, first, last, q, now, page)
    args = {"status": status or "all", "q": q}
    if "from" in request.args or "to" in request.args:
        args.update({"from": first, "to": last})
    return render_template("guests/requests.html", title="Guest requests", result=result, args=args, now=now,
                           status_of=guests.display_status, statuses=guests.STATUS_FILTERS,
                           pending=guests.pending_count(get_db(), now))


@bp.get("/requests/<int:request_id>")
@role_required("admin", "warden")
def review(request_id):
    row = guests.get_request(get_db(), request_id)
    if row is None:
        abort(404)
    return _detail(row, staff=True)


def _detail(row, staff):
    db, now = get_db(), _now()
    entries, exits = guests.request_events(db, row["request_id"])
    return render_template("guests/detail.html", title="Guest visit", r=row, staff=staff, status=guests.display_status(row, now),
                           entries=entries, exits=exits, max_entries=current_app.config["GUEST_MAX_ENTRIES"])


@bp.post("/requests/<int:request_id>/decide")
@role_required("warden")
def decide(request_id):
    action = request.form.get("action")
    if action not in ("approve", "reject"):
        abort(400)
    entries = request.form.get("entries", 1, type=int)
    outcome = guests.decide(get_db(), request_id, g.user["id"], action == "approve", entries, _now(),
                            current_app.config["GUEST_MAX_ENTRIES"])
    messages = {
        "approved": ("Visit approved.", "success"), "rejected": ("Visit rejected.", "success"),
        "missing": ("That request does not exist.", "warning"),
        "not_pending": ("This request was already decided.", "info"),
        "expired": ("The visit's time window has already ended, so it cannot be approved.", "warning"),
        "bad_entries": (f"Choose between 1 and {current_app.config['GUEST_MAX_ENTRIES']} entries.", "warning"),
    }
    text, kind = messages[outcome]
    flash(text, kind)
    if outcome in ("approved", "rejected"):
        current_app.logger.info("Guest visit %s: request_id=%s by warden_id=%s", outcome, request_id, g.user["id"])
    return redirect(url_for("guests.review", request_id=request_id) if outcome != "missing" else url_for("guests.requests_list"))


# ============================ guest face enrollment ============================
# Wardens (and admins) register the guest's face at the gate, only for an APPROVED visit, using the same camera page and the
# same checks as student enrollment.

def _enrollable(request_id):
    row = guests.get_request(get_db(), request_id)
    if row is None:
        abort(404)
    return row


def _subject(row, status):
    rid = row["request_id"]
    return {
        "kind": "guest", "name": row["guest_name"], "sub": f"Guest of {row['host_name']}",
        "check_url": url_for("guests.enroll_check", request_id=rid), "save_url": url_for("guests.enroll_save", request_id=rid),
        "remove_url": url_for("guests.enroll_remove", request_id=rid),
        "back_url": url_for("guests.review", request_id=rid), "back_label": "Back to the visit",
        "remove_note": "The guest will not be recognised at the entry terminal until registered again.",
        "enrolled": status["enrolled"], "face_samples": status["face_samples"], "enrolled_at": status["enrolled_at"],
    }


@bp.get("/requests/<int:request_id>/enroll")
@role_required("admin", "warden")
def enroll(request_id):
    row = _enrollable(request_id)
    if row["status"] != "Approved":
        flash("A guest's face can be registered only after the visit is approved.", "warning")
        return redirect(url_for("guests.review", request_id=request_id))
    status = biometrics.enrollment_status(get_db(), "guest", row["guest_id"])
    return render_template("enrollment/capture.html", title="Register guest face", subject=_subject(row, status),
                           samples=current_app.config["ENROLLMENT_SAMPLES"], key_problem=enrollment.key_problem())


@bp.post("/requests/<int:request_id>/enroll/check")
@role_required("admin", "warden")
def enroll_check(request_id):
    _enrollable(request_id)
    return enrollment.check_one_image()


@bp.post("/requests/<int:request_id>/enroll/save")
@role_required("admin", "warden")
def enroll_save(request_id):
    row = _enrollable(request_id)
    if row["status"] != "Approved":
        return enrollment.fail(409, "not_approved", "A guest's face can be registered only after the visit is approved.")
    problem = enrollment.key_problem()
    if problem:
        return enrollment.fail(503, "no_key", problem)
    db, cfg = get_db(), current_app.config
    key, guest_id = cfg["BIOMETRIC_KEY"], row["guest_id"]

    def already_enrolled():
        if biometrics.enrollment_status(db, "guest", guest_id)["enrolled"] and request.form.get("replace") != "1":
            return enrollment.fail(409, "already_enrolled", "This guest already has a registered face. Confirm to replace it.")

    encodings, eye_templates, error = enrollment.analyze_samples("guest", before_analysis=already_enrolled)
    if error:
        return error

    # 1) A guest must never be able to hold a student's face.
    student_hit = biometrics.find_duplicate_in(biometrics.load_templates(db, key, "student", "face", only_active=False),
                                               encodings, cfg["FACE_THRESHOLD"])
    if student_hit:
        who = db.execute("SELECT full_name, registration_no FROM student WHERE student_id = ?", (student_hit[0],)).fetchone()
        return enrollment.fail(409, "is_student", f"This face belongs to the enrolled student {who['full_name']} "
                               f"({who['registration_no']}). Students do not need a guest visit.")

    # 2) A returning guest: the face matches a guest we already know. The match IS the verification, so reuse that profile.
    other = biometrics.find_duplicate_in(biometrics.load_templates(db, key, "guest", "face", only_active=False), encodings,
                                         cfg["FACE_THRESHOLD"], exclude_id=guest_id)
    if other:
        known = db.execute("SELECT full_name FROM guest_profile WHERE guest_id = ?", (other[0],)).fetchone()
        guests.reuse_guest(db, guest_id, other[0])
        flash(f"Returning guest recognised ({known['full_name']}). Their earlier profile is reused; nothing new was stored.", "success")
        current_app.logger.info("Guest profile reused: request_id=%s guest_id %s -> %s by %s_id=%s", request_id, guest_id, other[0],
                                g.user["role"], g.user["id"])
        return jsonify(ok=True, message="Returning guest recognised.", redirect=url_for("guests.review", request_id=request_id))

    try:
        biometrics.save_enrollment(db, key, "guest", guest_id, encodings, eye_templates)
    except Exception:
        current_app.logger.exception("Saving guest enrollment failed")
        return enrollment.fail(500, "save_failed", "The enrollment could not be saved. Nothing was changed.")
    current_app.logger.info("Guest enrollment saved: guest_id=%s by %s_id=%s", guest_id, g.user["role"], g.user["id"])
    flash(f"Face registered for {row['guest_name']}.", "success")
    return jsonify(ok=True, message="Guest face registered.", redirect=url_for("guests.review", request_id=request_id))


@bp.post("/requests/<int:request_id>/enroll/remove")
@role_required("admin", "warden")
def enroll_remove(request_id):
    row = _enrollable(request_id)
    removed = biometrics.remove_enrollment(get_db(), "guest", row["guest_id"])
    current_app.logger.info("Guest face data removed: guest_id=%s by %s_id=%s rows=%s", row["guest_id"], g.user["role"], g.user["id"], removed)
    flash("The guest's face data was deleted." if removed else "There was no face data to delete.", "success" if removed else "info")
    return redirect(url_for("guests.review", request_id=request_id))
