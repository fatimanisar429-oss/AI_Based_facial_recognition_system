"""Emergency contacts (SRS FR-15, UC-10): people and offices that receive emergency emails. Administrator only.
Listing a contact here only means they get an email; it does not contact the police or anyone else automatically."""
import re

from .people import EMAIL_PATTERN, PHONE_PATTERN, like_pattern, paginate

TYPES = ("Security", "Police", "Other")


def validate(form):
    clean, errors = {}, {}
    clean["contact_name"] = (form.get("contact_name") or "").strip()
    if not 2 <= len(clean["contact_name"]) <= 100:
        errors["contact_name"] = "Enter a name (2-100 characters)."
    clean["contact_type"] = (form.get("contact_type") or "").strip()
    if clean["contact_type"] not in TYPES:
        errors["contact_type"] = "Choose a type from the list."
    clean["phone"] = (form.get("phone") or "").strip()
    digits = re.sub(r"\D", "", clean["phone"])
    if not PHONE_PATTERN.match(clean["phone"]) or not 7 <= len(digits) <= 15:
        errors["phone"] = "Enter a valid phone number (7-15 digits, may start with +)."
    clean["email"] = (form.get("email") or "").strip().lower()
    if not clean["email"] or len(clean["email"]) > 100 or not EMAIL_PATTERN.match(clean["email"]):
        errors["email"] = "Enter a valid email address."
    return clean, errors


def search(db, q="", page=1):
    where, params = "WHERE 1=1", []
    if q:
        where += " AND (contact_name LIKE ? ESCAPE '\\' OR email LIKE ? ESCAPE '\\')"
        params += [like_pattern(q)] * 2
    return paginate(db, f"SELECT COUNT(*) FROM emergency_contact {where}",
                    f"SELECT * FROM emergency_contact {where} ORDER BY contact_name COLLATE NOCASE, contact_id", params, page)


def get(db, contact_id):
    return db.execute("SELECT * FROM emergency_contact WHERE contact_id = ?", (contact_id,)).fetchone()


def create(db, clean, admin_id):
    cur = db.execute("INSERT INTO emergency_contact (contact_name, contact_type, phone, email, admin_id) VALUES (?, ?, ?, ?, ?)",
                     (clean["contact_name"], clean["contact_type"], clean["phone"], clean["email"], admin_id))
    db.commit()
    return cur.lastrowid


def update(db, contact_id, clean):
    db.execute("UPDATE emergency_contact SET contact_name = ?, contact_type = ?, phone = ?, email = ? WHERE contact_id = ?",
               (clean["contact_name"], clean["contact_type"], clean["phone"], clean["email"], contact_id))
    db.commit()


def delete(db, contact_id):
    cur = db.execute("DELETE FROM emergency_contact WHERE contact_id = ?", (contact_id,))
    db.commit()
    return cur.rowcount == 1