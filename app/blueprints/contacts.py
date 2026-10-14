"""Emergency contact management (SRS FR-15, UC-10). Administrator only."""
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from ..db import get_db
from ..security import role_required
from ..services import contacts

bp = Blueprint("contacts", __name__, url_prefix="/contacts")


def _get(contact_id):
    row = contacts.get(get_db(), contact_id)
    if row is None:
        abort(404)
    return row


def _form(mode, values, errors, contact_id=None, status=200):
    return render_template("contacts/form.html", mode=mode, v=values, errors=errors, contact_id=contact_id,
                           type_options=[(t, t) for t in contacts.TYPES], title="Add contact" if mode == "new" else "Edit contact"), status


@bp.get("/")
@role_required("admin")
def index():
    q = request.args.get("q", "").strip()[:100]
    page = request.args.get("page", 1, type=int)
    return render_template("contacts/index.html", title="Emergency contacts", result=contacts.search(get_db(), q, page), q=q)


@bp.route("/new", methods=["GET", "POST"])
@role_required("admin")
def create():
    if request.method == "GET":
        return _form("new", {}, {})
    clean, errors = contacts.validate(request.form)
    if errors:
        return _form("new", clean, errors, status=400)
    contacts.create(get_db(), clean, g.user["id"])
    flash(f"{clean['contact_name']} was added. They will receive emergency emails.", "success")
    return redirect(url_for("contacts.index"))


@bp.route("/<int:contact_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit(contact_id):
    row = _get(contact_id)
    if request.method == "GET":
        return _form("edit", dict(row), {}, contact_id)
    clean, errors = contacts.validate(request.form)
    if errors:
        return _form("edit", clean, errors, contact_id, status=400)
    contacts.update(get_db(), contact_id, clean)
    flash("Contact saved.", "success")
    return redirect(url_for("contacts.index"))


@bp.post("/<int:contact_id>/delete")
@role_required("admin")
def delete(contact_id):
    row = _get(contact_id)
    contacts.delete(get_db(), contact_id)
    current_app.logger.info("Emergency contact deleted: contact_id=%s by admin_id=%s", contact_id, g.user["id"])
    flash(f"{row['contact_name']} was removed and will no longer receive alert emails.", "success")
    return redirect(url_for("contacts.index"))