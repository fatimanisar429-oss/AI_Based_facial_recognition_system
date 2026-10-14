"""Students and wardens: validation, saving and searching.

Routes call these functions; they never build SQL from user text (everything is parameterised).
Validation returns (clean_values, errors). If errors is not empty, nothing is saved.
"""
import math
import re

from werkzeug.security import generate_password_hash

from ..timeutil import utc_now_text

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9 \-]*$")
REGISTRATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_/]*$")
GENDERS = ("Female", "Male")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
PER_PAGE = 10

# Every table that can log in. The login form looks an email up in all three, so an email
# must be unique across them, not only inside one table.
ACCOUNT_TABLES = (("admin", "admin_id"), ("warden", "warden_id"), ("student", "student_id"))


# ---------- validation ----------

def email_in_use(db, email, exclude=None):
    """True if any account (admin, warden or student) already uses this email.
    exclude=("student", 4) ignores that one row, so editing a person can keep their own email."""
    for table, pk in ACCOUNT_TABLES:
        row = db.execute(f"SELECT {pk} FROM {table} WHERE email = ? COLLATE NOCASE", (email,)).fetchone()
        if row is not None and (table, row[0]) != exclude:
            return True
    return False


def _text(form, name):
    return (form.get(name) or "").strip()


def _length(value, name, label, errors, low, high):
    if not value:
        errors[name] = f"{label} is required."
    elif not low <= len(value) <= high:
        errors[name] = f"{label} must be {low}-{high} characters."


def _check_name(form, data, errors, name="full_name", label="Full name"):
    data[name] = _text(form, name)
    _length(data[name], name, label, errors, 2, 100)
    if name not in errors and not any(c.isalpha() for c in data[name]):
        errors[name] = f"{label} must contain letters."


def _check_email(db, form, data, errors, exclude, name="email", label="Email", must_be_unique=True):
    data[name] = _text(form, name).lower()
    value = data[name]
    if not value:
        errors[name] = f"{label} is required."
    elif len(value) > 100 or not EMAIL_PATTERN.match(value):
        errors[name] = f"Enter a valid {label.lower()} address."
    elif must_be_unique and email_in_use(db, value, exclude):
        errors[name] = "This email is already used by another account."


def _check_phone(form, data, errors):
    data["phone"] = _text(form, "phone")
    digits = re.sub(r"\D", "", data["phone"])
    if not data["phone"]:
        errors["phone"] = "Phone number is required."
    elif not PHONE_PATTERN.match(data["phone"]) or not 7 <= len(digits) <= 15:
        errors["phone"] = "Enter a valid phone number (7-15 digits, may start with +)."


def _check_gender(form, data, errors):
    data["gender"] = _text(form, "gender")
    if data["gender"] not in GENDERS:
        errors["gender"] = "Choose a gender from the list."


def _check_password(form, data, errors, required):
    """Stores a hash in data['password_hash'] when a password was given. Never stores the plain text."""
    password = form.get("password") or ""
    if not password:
        if required:
            errors["password"] = "A password is required."
        return
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        errors["password"] = f"Password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters."
    else:
        data["password_hash"] = generate_password_hash(password)


def validate_warden(db, form, warden_id=None):
    data, errors = {}, {}
    exclude = ("warden", warden_id) if warden_id else None
    _check_name(form, data, errors)
    _check_email(db, form, data, errors, exclude)
    _check_phone(form, data, errors)
    _check_gender(form, data, errors)
    _check_password(form, data, errors, required=warden_id is None)
    return data, errors


def validate_student(db, form, student_id=None):
    data, errors = {}, {}
    exclude = ("student", student_id) if student_id else None
    _check_name(form, data, errors)

    data["registration_no"] = _text(form, "registration_no").upper()
    _length(data["registration_no"], "registration_no", "Registration number", errors, 3, 30)
    if "registration_no" not in errors:
        if not REGISTRATION_PATTERN.match(data["registration_no"]):
            errors["registration_no"] = "Use letters, numbers, - _ / only."
        else:
            row = db.execute("SELECT student_id FROM student WHERE registration_no = ?",
                             (data["registration_no"],)).fetchone()
            if row is not None and row[0] != student_id:
                errors["registration_no"] = "This registration number already exists."

    _check_email(db, form, data, errors, exclude)
    _check_phone(form, data, errors)
    _check_gender(form, data, errors)

    for field, label, low, high in (("room_no", "Room number", 1, 20), ("department", "Department", 2, 100),
                                    ("session", "Session", 4, 20), ("parent_name", "Parent name", 2, 100)):
        data[field] = _text(form, field)
        _length(data[field], field, label, errors, low, high)
    # The parent's email may legitimately match a sibling's, so it is not checked for uniqueness.
    _check_email(db, form, data, errors, None, name="parent_email", label="Parent email", must_be_unique=False)

    raw_warden = _text(form, "warden_id")
    data["warden_id"] = None
    if raw_warden:
        row = db.execute("SELECT warden_id FROM warden WHERE warden_id = ? AND is_active = 1",
                         (raw_warden if raw_warden.isdigit() else -1,)).fetchone()
        if row is None:
            errors["warden_id"] = "Choose a warden from the list."
        else:
            data["warden_id"] = row[0]

    _check_password(form, data, errors, required=student_id is None)
    return data, errors


# ---------- saving ----------

def create_warden(db, d):
    cur = db.execute(
        "INSERT INTO warden (full_name, email, phone, gender, password_hash) VALUES (?, ?, ?, ?, ?)",
        (d["full_name"], d["email"], d["phone"], d["gender"], d["password_hash"]))
    db.commit()
    return cur.lastrowid


def update_warden(db, warden_id, d):
    db.execute("UPDATE warden SET full_name=?, email=?, phone=?, gender=? WHERE warden_id=?",
               (d["full_name"], d["email"], d["phone"], d["gender"], warden_id))
    if "password_hash" in d:
        db.execute("UPDATE warden SET password_hash=? WHERE warden_id=?", (d["password_hash"], warden_id))
    db.commit()


_STUDENT_FIELDS = ("registration_no", "full_name", "email", "phone", "room_no", "department",
                   "session", "gender", "parent_name", "parent_email", "warden_id")


def create_student(db, d):
    cols = _STUDENT_FIELDS + ("password_hash", "created_at")
    cur = db.execute(f"INSERT INTO student ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [d[c] for c in _STUDENT_FIELDS + ("password_hash",)] + [utc_now_text()])
    db.commit()
    return cur.lastrowid


def update_student(db, student_id, d):
    sets = ", ".join(f"{c} = ?" for c in _STUDENT_FIELDS)
    db.execute(f"UPDATE student SET {sets} WHERE student_id = ?", [d[c] for c in _STUDENT_FIELDS] + [student_id])
    if "password_hash" in d:
        db.execute("UPDATE student SET password_hash=? WHERE student_id=?", (d["password_hash"], student_id))
    db.commit()


def set_active(db, table, record_id, active):
    """Deactivate or reactivate an account. Accounts are never deleted, so history stays intact."""
    pk = dict(ACCOUNT_TABLES)[table]          # only the three known table names can reach the SQL
    db.execute(f"UPDATE {table} SET is_active = ? WHERE {pk} = ?", (1 if active else 0, record_id))
    db.commit()


# ---------- searching ----------

def like_pattern(text):
    """Turn user text into a LIKE pattern where % and _ are ordinary characters."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def paginate(db, count_sql, rows_sql, params, page):
    """Run a count query and a LIMIT/OFFSET query; returns rows, total, page, pages."""
    total = db.execute(count_sql, params).fetchone()[0]
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(max(page, 1), pages)
    rows = db.execute(rows_sql + " LIMIT ? OFFSET ?", params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
    return {"rows": rows, "total": total, "page": page, "pages": pages}


def _status_clause(status):
    return {"active": " AND {t}.is_active = 1", "inactive": " AND {t}.is_active = 0"}.get(status, "")


def search_students(db, q="", status="active", department="", page=1):
    where, params = "WHERE 1=1" + _status_clause(status).format(t="s"), []
    if q:
        where += (" AND (s.full_name LIKE ? ESCAPE '\\' OR s.registration_no LIKE ? ESCAPE '\\'"
                  " OR s.email LIKE ? ESCAPE '\\' OR s.room_no LIKE ? ESCAPE '\\')")
        params += [like_pattern(q)] * 4
    if department:
        where += " AND s.department = ?"
        params.append(department)
    return paginate(
        db,
        f"SELECT COUNT(*) FROM student s {where}",
        "SELECT s.*, w.full_name AS warden_name FROM student s "
        f"LEFT JOIN warden w ON w.warden_id = s.warden_id {where} ORDER BY s.full_name COLLATE NOCASE, s.student_id",
        params, page)


def search_wardens(db, q="", status="active", page=1):
    where, params = "WHERE 1=1" + _status_clause(status).format(t="w"), []
    if q:
        where += " AND (w.full_name LIKE ? ESCAPE '\\' OR w.email LIKE ? ESCAPE '\\')"
        params += [like_pattern(q)] * 2
    return paginate(
        db,
        f"SELECT COUNT(*) FROM warden w {where}",
        "SELECT w.*, (SELECT COUNT(*) FROM student s WHERE s.warden_id = w.warden_id AND s.is_active = 1)"
        f" AS student_count FROM warden w {where} ORDER BY w.full_name COLLATE NOCASE, w.warden_id",
        params, page)


def departments(db):
    return [r[0] for r in db.execute("SELECT DISTINCT department FROM student ORDER BY department COLLATE NOCASE")]


def active_wardens(db, include_id=None):
    """Wardens that can be chosen on the student form (plus the currently assigned one, even if inactive)."""
    return db.execute(
        "SELECT warden_id, full_name, is_active FROM warden WHERE is_active = 1 OR warden_id = ? "
        "ORDER BY full_name COLLATE NOCASE", (include_id,)).fetchall()
