"""Encrypted template storage: encryption, atomic replace, loading, duplicate detection."""
import numpy as np
import pytest
from cryptography.fernet import Fernet

from app.db import get_db
from app.services import biometrics as B
from app.services import recognition

KEY = Fernet.generate_key().decode()
RNG = np.random.default_rng(7)


def face(seed):
    return np.random.default_rng(seed).normal(0, 0.1, 128)


def eye(seed):
    h = np.random.default_rng(seed).random(80)
    return (h / h.sum()).astype(np.float32)


def enroll(db, student_id, seed, key=KEY, n=5):
    B.save_enrollment(db, key, "student", student_id, [face(seed) + i * 0.001 for i in range(n)], [eye(seed + i) for i in range(n)])


def rows(app, where="1=1"):
    with app.app_context():
        return get_db().execute(f"SELECT * FROM biometric_profile WHERE {where}").fetchall()


# ---------- encryption ----------

def test_round_trip_keeps_values_to_float32_precision():
    values = face(1)
    back = B.decrypt_template(B.encrypt_template(values, KEY), KEY)
    assert back.shape == (128,) and np.allclose(back, values, atol=1e-6)


def test_ciphertext_does_not_contain_the_numbers():
    values = face(2)
    blob = B.encrypt_template(values, KEY)
    assert values.astype("<f4").tobytes() not in blob
    assert len(set(blob)) > 40                            # looks random, not a repeated pattern


def test_encrypting_twice_gives_different_ciphertext():
    assert B.encrypt_template(face(3), KEY) != B.encrypt_template(face(3), KEY)


def test_wrong_key_cannot_decrypt():
    blob = B.encrypt_template(face(4), KEY)
    with pytest.raises(B.BiometricKeyError):
        B.decrypt_template(blob, Fernet.generate_key().decode())


def test_tampered_ciphertext_is_detected():
    blob = bytearray(B.encrypt_template(face(5), KEY))
    blob[len(blob) // 2] ^= 0x01
    with pytest.raises(B.BiometricKeyError):
        B.decrypt_template(bytes(blob), KEY)


@pytest.mark.parametrize("bad", ["", None, "not-a-key", "short"])
def test_missing_or_invalid_key_is_reported(bad):
    assert B.key_problem(bad)
    with pytest.raises(B.BiometricKeyError):
        B.encrypt_template(face(1), bad)


def test_valid_key_has_no_problem():
    assert B.key_problem(KEY) is None and B.key_problem(Fernet.generate_key()) is None


# ---------- saving, status, removing ----------

def test_enrollment_stores_encrypted_rows_only(app, users):
    with app.app_context():
        enroll(get_db(), 1, 10)
    stored = rows(app)
    assert len(stored) == 10 and {r["kind"] for r in stored} == {"face", "eye"}
    assert sorted(r["sample_no"] for r in stored if r["kind"] == "face") == [1, 2, 3, 4, 5]
    for r in stored:
        assert r["owner_type"] == "student" and r["owner_id"] == 1
        assert face(10).astype("<f4").tobytes() not in bytes(r["template"])
        assert B.decrypt_template(r["template"], KEY).size in (128, 80)


def test_status_reports_counts_but_no_template_data(app, users):
    with app.app_context():
        db = get_db()
        assert B.enrollment_status(db, "student", 1) == {"enrolled": False, "face_samples": 0, "eye_samples": 0, "enrolled_at": None}
        enroll(db, 1, 10)
        status = B.enrollment_status(db, "student", 1)
    assert status["enrolled"] and status["face_samples"] == 5 and status["eye_samples"] == 5
    assert set(status) == {"enrolled", "face_samples", "eye_samples", "enrolled_at"}


def test_status_is_per_student(app, users):
    with app.app_context():
        enroll(get_db(), 1, 10)
        assert B.enrollment_status(get_db(), "student", 2)["enrolled"] is False


def test_re_enrolling_replaces_the_old_templates(app, users):
    with app.app_context():
        db = get_db()
        enroll(db, 1, 10)
        old = {r["profile_id"] for r in db.execute("SELECT profile_id FROM biometric_profile")}
        enroll(db, 1, 99)
    new = rows(app)
    assert len(new) == 10 and not old & {r["profile_id"] for r in new}


def test_failed_replace_keeps_the_old_enrollment(app, users, monkeypatch):
    with app.app_context():
        db = get_db()
        enroll(db, 1, 10)
        before = [bytes(r["template"]) for r in db.execute("SELECT template FROM biometric_profile ORDER BY profile_id")]
        calls = {"n": 0}
        real = B.encrypt_template

        def flaky(values, key):
            calls["n"] += 1
            if calls["n"] == 4:
                raise RuntimeError("disk on fire")
            return real(values, key)

        monkeypatch.setattr(B, "encrypt_template", flaky)
        with pytest.raises(RuntimeError):
            enroll(db, 1, 99)
        after = [bytes(r["template"]) for r in db.execute("SELECT template FROM biometric_profile ORDER BY profile_id")]
    assert after == before


def test_failure_during_insert_rolls_everything_back(app, users, monkeypatch):
    with app.app_context():
        db = get_db()
        enroll(db, 1, 10)
        monkeypatch.setattr(B, "utc_now_text", lambda: None)       # created_at is NOT NULL -> the INSERT fails
        with pytest.raises(Exception):
            enroll(db, 1, 99)
        monkeypatch.undo()
        assert B.enrollment_status(db, "student", 1)["face_samples"] == 5     # old rows survived


def test_samples_must_come_in_pairs(app, users):
    with app.app_context(), pytest.raises(ValueError):
        B.save_enrollment(get_db(), KEY, "student", 1, [face(1)] * 5, [eye(1)] * 4)


def test_remove_enrollment(app, users):
    with app.app_context():
        db = get_db()
        enroll(db, 1, 10)
        enroll(db, 2, 20)
        assert B.remove_enrollment(db, "student", 1) == 10
        assert B.remove_enrollment(db, "student", 1) == 0
        assert B.enrollment_status(db, "student", 2)["enrolled"]


def test_sample_numbers_are_unique_per_person(app, users):
    with app.app_context():
        db = get_db()
        enroll(db, 1, 10)
        with pytest.raises(Exception):
            db.execute("INSERT INTO biometric_profile (owner_type,owner_id,kind,sample_no,template,created_at) "
                       "VALUES ('student',1,'face',1,x'00','2026-01-01T00:00:00')")


# ---------- loading and duplicate detection ----------

def test_load_templates_groups_by_owner(app, users):
    with app.app_context():
        db = get_db()
        enroll(db, 1, 10)
        enroll(db, 2, 20)
        faces = B.load_templates(db, KEY, "student", "face")
        eyes = B.load_templates(db, KEY, "student", "eye")
    assert set(faces) == {1, 2} and len(faces[1]) == 5 and faces[1][0].shape == (128,)
    assert eyes[2][0].shape == (80,)


def test_deactivated_students_are_never_loaded_for_matching(app, users):
    with app.app_context():
        db = get_db()
        enroll(db, 1, 10)
        enroll(db, 2, 20)
        db.execute("UPDATE student SET is_active = 0 WHERE student_id = 2")
        db.commit()
        assert set(B.load_templates(db, KEY, "student", "face")) == {1}
        assert set(B.load_templates(db, KEY, "student", "face", only_active=False)) == {1, 2}


def test_load_with_the_wrong_key_fails_loudly(app, users):
    with app.app_context():
        enroll(get_db(), 1, 10)
        with pytest.raises(B.BiometricKeyError):
            B.load_templates(get_db(), Fernet.generate_key().decode(), "student", "face")


def test_duplicate_face_found_for_a_different_student(app, users):
    with app.app_context():
        db = get_db()
        enroll(db, 1, 10)
        same_person = [face(10) + 0.002 for _ in range(5)]
        assert B.find_duplicate_face(db, KEY, same_person, 0.5)[0] == 1


def test_duplicate_check_ignores_the_student_themselves(app, users):
    with app.app_context():
        db = get_db()
        enroll(db, 1, 10)
        assert B.find_duplicate_face(db, KEY, [face(10)] * 5, 0.5, exclude_student_id=1) is None


def test_different_face_is_not_a_duplicate(app, users):
    with app.app_context():
        db = get_db()
        enroll(db, 1, 10)
        other = [face(12345)] * 5
        assert recognition.face_distance(face(10), face(12345))[0] > 0.5          # sanity: really different
        assert B.find_duplicate_face(db, KEY, other, 0.5) is None


def test_duplicate_check_ignores_deactivated_students(app, users):
    with app.app_context():
        db = get_db()
        enroll(db, 1, 10)
        db.execute("UPDATE student SET is_active = 0 WHERE student_id = 1")
        db.commit()
        assert B.find_duplicate_face(db, KEY, [face(10)] * 5, 0.5) is None


# ---------- enrollment overview ----------

@pytest.fixture
def overview(app, users):
    with app.app_context():
        db = get_db()
        for n in range(3, 16):
            db.execute("INSERT INTO student (registration_no,full_name,email,phone,room_no,department,session,gender,"
                       "parent_name,parent_email,password_hash,is_active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f"REG-{n:03d}", f"Name {n:02d}", f"s{n}@x.com", "1", f"B-{n}", "CS", "2022", "F", "P", "p@x.com", "h",
                        0 if n == 15 else 1))
        db.commit()
        enroll(db, 1, 10)
        enroll(db, 3, 30)
    return app


def test_overview_lists_active_students_with_counts(overview):
    with overview.app_context():
        db = get_db()
        assert B.enrollment_overview(db)["total"] == 14       # 2 fixture students + 12 active new ones (REG-015 is deactivated)
        result = B.enrollment_overview(db, q="Student")         # the two fixture students
    by_id = {r["student_id"]: r for r in result["rows"]}
    assert by_id[1]["face_samples"] == 5 and by_id[1]["enrolled_at"] and by_id[2]["face_samples"] == 0 and by_id[2]["enrolled_at"] is None


def test_overview_filters(overview):
    with overview.app_context():
        db = get_db()
        enrolled = B.enrollment_overview(db, which="enrolled")
        missing = B.enrollment_overview(db, which="not_enrolled")
    assert enrolled["total"] == 2 and missing["total"] == 12


def test_overview_search_and_paging(overview):
    with overview.app_context():
        db = get_db()
        assert B.enrollment_overview(db, q="name 07")["total"] == 1
        assert B.enrollment_overview(db, q="%")["total"] == 0              # wildcard characters are literal
        page2 = B.enrollment_overview(db, page=2)
    assert page2["page"] == 2 and len(page2["rows"]) == 4        # 14 students: 10 + 4
