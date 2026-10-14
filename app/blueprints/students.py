"""Student management (SRS FR-02, UC-02).

Admin: list, add, view, edit, deactivate/reactivate.   Warden: list and view only.   Student: no access.
"""
import sqlite3

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..db import get_db
from ..security import role_required
from ..services import biometrics, people

bp = Blueprint("students", __name__, url_prefix="/students")

STATUS_CHOICES = ("active", "inactive", "all")


def _get_student(student_id):
    row = get_db().execute(
        "SELECT s.*, w.full_name AS warden_name, w.is_active AS warden_active FROM student s "
        "LEFT JOIN warden w ON w.warden_id = s.warden_id WHERE s.student_id = ?", (student_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _form_page(mode, values, errors, student_id=None, status=200):
    db = get_db()
    wardens = people.active_wardens(db, values.get("warden_id") or None)
    warden_options = [(w["warden_id"], w["full_name"] + ("" if w["is_active"] else " (deactivated)")) for w in wardens]
    return render_template(
        "students/form.html", mode=mode, v=values, errors=errors, student_id=student_id,
        warden_options=warden_options, gender_options=[(g, g) for g in people.GENDERS],
        title="Add student" if mode == "new" else "Edit student"), status


@bp.get("/")
@role_required("admin", "warden")
def index():
    q = request.args.get("q", "").strip()[:100]
    status = request.args.get("status", "active")
    status = status if status in STATUS_CHOICES else "active"
    department = request.args.get("department", "").strip()
    page = request.args.get("page", 1, type=int)
    db = get_db()
    result = people.search_students(db, q, status, department, page)
    filters = {"q": q, "status": status, "department": department}
    return render_template("students/list.html", title="Students", result=result, filters=filters,
                           departments=people.departments(db))


@bp.route("/new", methods=["GET", "POST"])
@role_required("admin")
def create():
    if request.method == "GET":
        return _form_page("new", {}, {})
    db = get_db()
    data, errors = people.validate_student(db, request.form)
    if not errors:
        try:
            student_id = people.create_student(db, data)
        except sqlite3.IntegrityError:  # two admins saving the same value at the same moment
            errors["registration_no"] = "This registration number or email already exists."
        else:
            flash(f"Student {data['full_name']} was added.", "success")
            return redirect(url_for("students.detail", student_id=student_id))
    return _form_page("new", data, errors, status=400)


@bp.get("/<int:student_id>")
@role_required("admin", "warden")
def detail(student_id):
    return render_template("students/detail.html", s=_get_student(student_id), title="Student details",
                           bio=biometrics.enrollment_status(get_db(), "student", student_id))


@bp.route("/<int:student_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit(student_id):
    student = _get_student(student_id)
    if request.method == "GET":
        values = {k: student[k] for k in student.keys() if k != "password_hash"}
        return _form_page("edit", values, {}, student_id)
    db = get_db()
    data, errors = people.validate_student(db, request.form, student_id)
    if not errors:
        try:
            people.update_student(db, student_id, data)
        except sqlite3.IntegrityError:
            errors["registration_no"] = "This registration number or email already exists."
        else:
            flash("Student details were saved.", "success")
            return redirect(url_for("students.detail", student_id=student_id))
    return _form_page("edit", data, errors, student_id, status=400)


@bp.post("/<int:student_id>/deactivate")
@role_required("admin")
def deactivate(student_id):
    student = _get_student(student_id)
    people.set_active(get_db(), "student", student_id, False)
    flash(f"{student['full_name']} was deactivated and can no longer log in.", "success")
    return redirect(url_for("students.detail", student_id=student_id))


@bp.post("/<int:student_id>/activate")
@role_required("admin")
def activate(student_id):
    student = _get_student(student_id)
    people.set_active(get_db(), "student", student_id, True)
    flash(f"{student['full_name']} was reactivated.", "success")
    return redirect(url_for("students.detail", student_id=student_id))
