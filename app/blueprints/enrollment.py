"""Biometric enrollment of STUDENTS (SRS FR-03, UC-03). Admin only (SRS NFR-04: biometric data is managed by the Administrator).
Guests are enrolled by wardens in blueprints/guests.py using the same helpers below.

Flow: the browser camera captures 5 pictures. Each one is sent to /check for an instant quality verdict (nothing is
stored). When all 5 are good the browser sends them together to /save, which extracts the numeric templates and
stores them encrypted. The pictures themselves are never written to disk or the database.
"""
from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   url_for)

from ..db import get_db
from ..security import role_required
from ..services import biometrics, recognition

bp = Blueprint("enrollment", __name__, url_prefix="/enrollment")


# ---------- helpers shared with the guest enrollment pages ----------

def limits():
    return recognition.Limits.from_config(current_app.config)


def fail(status, code, message, **extra):
    return jsonify(ok=False, code=code, message=message, **extra), status


def key_problem():
    """None if enrollment can proceed, otherwise a short explanation for the admin."""
    return biometrics.key_problem(current_app.config["BIOMETRIC_KEY"])


def check_one_image():
    """JSON verdict for the single picture in request.files['image']. Stores nothing."""
    image = request.files.get("image")
    if image is None:
        return fail(400, "no_image", "No image was received.")
    try:
        metrics = recognition.quick_check(image.read(), limits())
    except recognition.ImageError as err:
        return fail(422, err.code, err.message)
    except Exception:
        current_app.logger.exception("Image check failed")
        return fail(500, "processing_error", "The image could not be processed. Please try again.")
    return jsonify(ok=True, message="Good sample.", metrics=metrics)


def analyze_samples(who, before_analysis=None):
    """Validate the uploaded samples. Returns (encodings, eye_templates, None) or (None, None, error_response).
    `who` ("student" / "guest") only changes the wording of messages. `before_analysis` is an optional cheap check that
    may return an error response (it runs before the slow image analysis)."""
    needed = current_app.config["ENROLLMENT_SAMPLES"]
    images = request.files.getlist("images")
    if len(images) != needed:
        return None, None, fail(400, "wrong_count", f"Exactly {needed} samples are required.")
    if request.form.get("consent") != "1":
        return None, None, fail(400, "no_consent", f"Confirm that the {who} agreed to biometric enrollment.")
    early = before_analysis() if before_analysis else None
    if early:
        return None, None, early
    encodings, eye_templates = [], []
    for number, image in enumerate(images, 1):
        try:
            analysis = recognition.analyze_image(image.read(), limits(), jitters=1)
        except recognition.ImageError as err:
            return None, None, fail(422, err.code, f"Sample {number}: {err.message}", sample=number)
        except Exception:
            current_app.logger.exception("Enrollment analysis failed")
            return None, None, fail(500, "processing_error", "The samples could not be processed. Please try again.")
        encodings.append(analysis.encoding)
        eye_templates.append(analysis.eye_template)
    if recognition.max_pairwise_distance(encodings) > current_app.config["ENROLL_CONSISTENCY_MAX"]:
        return None, None, fail(422, "inconsistent", "The samples do not look like the same person. "
                                f"Make sure only the {who} is in front of the camera and capture all samples again.")
    return encodings, eye_templates, None


def _get_student(student_id):
    row = get_db().execute("SELECT * FROM student WHERE student_id = ?", (student_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def subject_for_student(student, status):
    """What the shared capture page needs to know about who is being enrolled."""
    sid = student["student_id"]
    return {
        "kind": "student", "name": student["full_name"], "sub": student["registration_no"],
        "check_url": url_for("enrollment.check", student_id=sid), "save_url": url_for("enrollment.save", student_id=sid),
        "remove_url": url_for("enrollment.remove", student_id=sid),
        "back_url": url_for("enrollment.index"), "back_label": "Back to enrollment list",
        "remove_note": "They will not be recognised at the entry terminal until enrolled again.",
        "enrolled": status["enrolled"], "face_samples": status["face_samples"], "enrolled_at": status["enrolled_at"],
    }


# ---------- student pages ----------

@bp.get("/")
@role_required("admin")
def index():
    q = request.args.get("q", "").strip()[:100]
    which = request.args.get("show", "all")
    which = which if which in biometrics.ENROLLMENT_FILTERS else "all"
    page = request.args.get("page", 1, type=int)
    result = biometrics.enrollment_overview(get_db(), q, which, page)
    return render_template("enrollment/index.html", title="Biometric enrollment", result=result,
                           filters={"q": q, "show": which}, key_problem=key_problem())


@bp.get("/student/<int:student_id>")
@role_required("admin")
def capture(student_id):
    student = _get_student(student_id)
    if not student["is_active"]:
        flash("This account is deactivated. Reactivate it before enrolling.", "warning")
        return redirect(url_for("students.detail", student_id=student_id))
    status = biometrics.enrollment_status(get_db(), "student", student_id)
    return render_template("enrollment/capture.html", title="Enroll face", subject=subject_for_student(student, status),
                           samples=current_app.config["ENROLLMENT_SAMPLES"], key_problem=key_problem())


@bp.post("/student/<int:student_id>/check")
@role_required("admin")
def check(student_id):
    _get_student(student_id)
    return check_one_image()


@bp.post("/student/<int:student_id>/save")
@role_required("admin")
def save(student_id):
    student = _get_student(student_id)
    if not student["is_active"]:
        return fail(409, "inactive", "This account is deactivated.")
    problem = key_problem()
    if problem:
        return fail(503, "no_key", problem)
    db = get_db()
    key = current_app.config["BIOMETRIC_KEY"]

    def already_enrolled():
        if biometrics.enrollment_status(db, "student", student_id)["enrolled"] and request.form.get("replace") != "1":
            return fail(409, "already_enrolled", "This student is already enrolled. Confirm to replace the enrollment.")

    encodings, eye_templates, error = analyze_samples("student", before_analysis=already_enrolled)
    if error:
        return error

    duplicate = biometrics.find_duplicate_face(db, key, encodings, current_app.config["DUPLICATE_FACE_THRESHOLD"],
                                               exclude_student_id=student_id)
    if duplicate:
        other = db.execute("SELECT full_name, registration_no FROM student WHERE student_id = ?",
                           (duplicate[0],)).fetchone()
        return fail(409, "duplicate_face", f"This face is already enrolled for {other['full_name']} "
                    f"({other['registration_no']}). One person can have only one student account.")
    # A guest with an approved visit must not be able to become a student by enrolling the same face.
    if biometrics.find_duplicate_in(biometrics.load_templates(db, key, "guest", "face"), encodings,
                                    current_app.config["DUPLICATE_FACE_THRESHOLD"]):
        return fail(409, "duplicate_face", "This face is already registered for a guest visit. Remove the guest's "
                    "face data first if this person is now a student.")

    try:
        biometrics.save_enrollment(db, key, "student", student_id, encodings, eye_templates)
    except Exception:
        current_app.logger.exception("Saving enrollment failed")
        return fail(500, "save_failed", "The enrollment could not be saved. Nothing was changed.")

    # Audit trail without any biometric content.
    needed = current_app.config["ENROLLMENT_SAMPLES"]
    current_app.logger.info("Enrollment saved: student_id=%s by admin_id=%s samples=%s", student_id, g.user["id"], needed)
    flash(f"Face enrollment saved for {student['full_name']} ({needed} samples).", "success")
    return jsonify(ok=True, message="Enrollment saved.", redirect=url_for("students.detail", student_id=student_id))


@bp.post("/student/<int:student_id>/remove")
@role_required("admin")
def remove(student_id):
    student = _get_student(student_id)
    removed = biometrics.remove_enrollment(get_db(), "student", student_id)
    current_app.logger.info("Enrollment removed: student_id=%s by admin_id=%s rows=%s", student_id, g.user["id"], removed)
    flash(f"Face enrollment removed for {student['full_name']}." if removed else "There was no enrollment to remove.",
          "success" if removed else "info")
    return redirect(url_for("students.detail", student_id=student_id))
