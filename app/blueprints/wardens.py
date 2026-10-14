"""Warden management (SRS FR-02). Admin only: wardens cannot create or edit other wardens."""
import sqlite3

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..db import get_db
from ..security import role_required
from ..services import people

bp = Blueprint("wardens", __name__, url_prefix="/wardens")

STATUS_CHOICES = ("active", "inactive", "all")


def _get_warden(warden_id):
    row = get_db().execute("SELECT * FROM warden WHERE warden_id = ?", (warden_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _form_page(mode, values, errors, warden_id=None, status=200):
    return render_template("wardens/form.html", mode=mode, v=values, errors=errors, warden_id=warden_id,
                           gender_options=[(g, g) for g in people.GENDERS], title="Add warden" if mode == "new" else "Edit warden"), status


@bp.get("/")
@role_required("admin")
def index():
    q = request.args.get("q", "").strip()[:100]
    status = request.args.get("status", "active")
    status = status if status in STATUS_CHOICES else "active"
    page = request.args.get("page", 1, type=int)
    result = people.search_wardens(get_db(), q, status, page)
    return render_template("wardens/list.html", title="Wardens", result=result,
                           filters={"q": q, "status": status})


@bp.route("/new", methods=["GET", "POST"])
@role_required("admin")
def create():
    if request.method == "GET":
        return _form_page("new", {}, {})
    db = get_db()
    data, errors = people.validate_warden(db, request.form)
    if not errors:
        try:
            warden_id = people.create_warden(db, data)
        except sqlite3.IntegrityError:
            errors["email"] = "This email is already used by another account."
        else:
            flash(f"Warden {data['full_name']} was added.", "success")
            return redirect(url_for("wardens.detail", warden_id=warden_id))
    return _form_page("new", data, errors, status=400)


@bp.get("/<int:warden_id>")
@role_required("admin")
def detail(warden_id):
    db = get_db()
    students = db.execute(
        "SELECT student_id, full_name, registration_no, room_no FROM student "
        "WHERE warden_id = ? AND is_active = 1 ORDER BY full_name COLLATE NOCASE", (warden_id,)).fetchall()
    return render_template("wardens/detail.html", w=_get_warden(warden_id), students=students,
                           title="Warden details")


@bp.route("/<int:warden_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit(warden_id):
    warden = _get_warden(warden_id)
    if request.method == "GET":
        values = {k: warden[k] for k in warden.keys() if k != "password_hash"}
        return _form_page("edit", values, {}, warden_id)
    db = get_db()
    data, errors = people.validate_warden(db, request.form, warden_id)
    if not errors:
        try:
            people.update_warden(db, warden_id, data)
        except sqlite3.IntegrityError:
            errors["email"] = "This email is already used by another account."
        else:
            flash("Warden details were saved.", "success")
            return redirect(url_for("wardens.detail", warden_id=warden_id))
    return _form_page("edit", data, errors, warden_id, status=400)


@bp.post("/<int:warden_id>/deactivate")
@role_required("admin")
def deactivate(warden_id):
    warden = _get_warden(warden_id)
    people.set_active(get_db(), "warden", warden_id, False)
    flash(f"{warden['full_name']} was deactivated and can no longer log in.", "success")
    return redirect(url_for("wardens.detail", warden_id=warden_id))


@bp.post("/<int:warden_id>/activate")
@role_required("admin")
def activate(warden_id):
    warden = _get_warden(warden_id)
    people.set_active(get_db(), "warden", warden_id, True)
    flash(f"{warden['full_name']} was reactivated.", "success")
    return redirect(url_for("wardens.detail", warden_id=warden_id))
