"""Storing biometric templates: encryption, saving, loading, removing.

A template is a list of numbers (never a photograph). Before it reaches the database it is packed as
    1 version byte + little-endian float32 values
and encrypted with Fernet (AES + integrity check) using BIOMETRIC_KEY. Anyone who copies the database file
without the key cannot read the templates. Nothing in this module logs template values.
"""
import numpy as np
from cryptography.fernet import Fernet, InvalidToken

from ..timeutil import utc_now_text
from . import recognition
from .people import like_pattern, paginate


class BiometricKeyError(Exception):
    """BIOMETRIC_KEY is missing or not a valid Fernet key, or does not match the stored data."""


def new_key():
    return Fernet.generate_key().decode()


def _fernet(key):
    if not key:
        raise BiometricKeyError("BIOMETRIC_KEY is not set. Run: flask --app run generate-biometric-key")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError):
        raise BiometricKeyError("BIOMETRIC_KEY is not a valid key.") from None


def key_problem(key):
    """None if the key is usable, otherwise a sentence for the admin."""
    try:
        _fernet(key)
    except BiometricKeyError as err:
        return str(err)
    return None


def encrypt_template(values, key):
    payload = bytes([recognition.TEMPLATE_VERSION]) + np.asarray(values, dtype="<f4").tobytes()
    return _fernet(key).encrypt(payload)


def decrypt_template(blob, key):
    try:
        payload = _fernet(key).decrypt(bytes(blob))
    except InvalidToken:
        raise BiometricKeyError("A stored template could not be decrypted (wrong BIOMETRIC_KEY?).") from None
    if payload[0] != recognition.TEMPLATE_VERSION:
        raise BiometricKeyError("A stored template has an unsupported version.")
    return np.frombuffer(payload[1:], dtype="<f4").astype(np.float64)


# ---------- saving and reading ----------

def save_enrollment(db, key, owner_type, owner_id, encodings, eye_templates):
    """Replace this person's templates with the new samples, all or nothing."""
    if len(encodings) != len(eye_templates):
        raise ValueError("Every sample needs both a face and an eye-region template.")
    now = utc_now_text()
    rows = [(owner_type, owner_id, "face", n, encrypt_template(e, key), now) for n, e in enumerate(encodings, 1)]
    rows += [(owner_type, owner_id, "eye", n, encrypt_template(t, key), now) for n, t in enumerate(eye_templates, 1)]
    try:
        db.execute("DELETE FROM biometric_profile WHERE owner_type = ? AND owner_id = ?", (owner_type, owner_id))
        db.executemany(
            "INSERT INTO biometric_profile (owner_type, owner_id, kind, sample_no, template, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", rows)
        db.commit()
    except Exception:
        db.rollback()
        raise


def remove_enrollment(db, owner_type, owner_id):
    cur = db.execute("DELETE FROM biometric_profile WHERE owner_type = ? AND owner_id = ?", (owner_type, owner_id))
    db.commit()
    return cur.rowcount


def enrollment_status(db, owner_type, owner_id):
    """Counts only. Never returns template data."""
    row = db.execute(
        "SELECT SUM(kind = 'face') AS face, SUM(kind = 'eye') AS eye, MAX(created_at) AS enrolled_at "
        "FROM biometric_profile WHERE owner_type = ? AND owner_id = ?", (owner_type, owner_id)).fetchone()
    face = row["face"] or 0
    return {"enrolled": face > 0, "face_samples": face, "eye_samples": row["eye"] or 0,
            "enrolled_at": row["enrolled_at"]}


def load_templates(db, key, owner_type, kind, only_active=True):
    """{owner_id: [numpy arrays]} for one kind of template. Used by duplicate checks now and by verification later.
    Deactivated students are left out, so they can never be matched."""
    sql = ("SELECT b.owner_id, b.template FROM biometric_profile b "
           "WHERE b.owner_type = ? AND b.kind = ? ")
    if only_active and owner_type == "student":
        sql += "AND EXISTS (SELECT 1 FROM student s WHERE s.student_id = b.owner_id AND s.is_active = 1) "
    if only_active and owner_type == "guest":
        # Only guests who have an approved visit can ever be matched; everyone else is simply unknown.
        sql += ("AND EXISTS (SELECT 1 FROM guest_visit_request r WHERE r.guest_id = b.owner_id AND r.status = 'Approved') ")
    sql += "ORDER BY b.owner_id, b.sample_no"
    found = {}
    for row in db.execute(sql, (owner_type, kind)):
        found.setdefault(row["owner_id"], []).append(decrypt_template(row["template"], key))
    return found


def find_duplicate_in(gallery, encodings, threshold, exclude_id=None):
    """Does this face match anyone in `gallery` ({owner_id: [templates]})? Returns (owner_id, distance) or None."""
    mean = np.mean(np.asarray(encodings, dtype=np.float64), axis=0)
    best = None
    for owner_id, templates in gallery.items():
        if owner_id == exclude_id:
            continue
        distance = float(recognition.face_distance(templates, mean).min())
        if distance < threshold and (best is None or distance < best[1]):
            best = (owner_id, distance)
    return best


def find_duplicate_face(db, key, encodings, threshold, exclude_student_id=None):
    """Is this face already enrolled for a different student? Returns (student_id, distance) or None."""
    mean = np.mean(np.asarray(encodings, dtype=np.float64), axis=0)
    best = None
    for student_id, templates in load_templates(db, key, "student", "face").items():
        if student_id == exclude_student_id:
            continue
        distance = float(recognition.face_distance(templates, mean).min())
        if distance < threshold and (best is None or distance < best[1]):
            best = (student_id, distance)
    return best


# ---------- enrollment list ----------

ENROLLMENT_FILTERS = ("all", "enrolled", "not_enrolled")


def enrollment_overview(db, q="", which="all", page=1):
    where, params = "WHERE s.is_active = 1", []
    face_exists = ("EXISTS (SELECT 1 FROM biometric_profile b WHERE b.owner_type = 'student' "
                   "AND b.owner_id = s.student_id AND b.kind = 'face')")
    if which == "enrolled":
        where += f" AND {face_exists}"
    elif which == "not_enrolled":
        where += f" AND NOT {face_exists}"
    if q:
        where += (" AND (s.full_name LIKE ? ESCAPE '\\' OR s.registration_no LIKE ? ESCAPE '\\'"
                  " OR s.room_no LIKE ? ESCAPE '\\')")
        params += [like_pattern(q)] * 3
    return paginate(
        db,
        f"SELECT COUNT(*) FROM student s {where}",
        "SELECT s.student_id, s.full_name, s.registration_no, s.department, s.room_no, "
        "(SELECT COUNT(*) FROM biometric_profile b WHERE b.owner_type='student' AND b.owner_id=s.student_id "
        " AND b.kind='face') AS face_samples, "
        "(SELECT MAX(created_at) FROM biometric_profile b WHERE b.owner_type='student' AND b.owner_id=s.student_id) "
        f"AS enrolled_at FROM student s {where} ORDER BY s.full_name COLLATE NOCASE, s.student_id",
        params, page)
