"""Enrollment routes: permissions, validation, saving, replacing, removing.

Most tests replace the image analysis with a small fake so every outcome can be controlled and the tests stay
fast. Two tests at the end run the REAL pipeline on a real face.
"""
import io
import re

import numpy as np
import pytest
from cryptography.fernet import Fernet

from app import create_app
from app.db import get_db
from app.services import biometrics, recognition
from conftest import get_token, login, token_when_logged_in
from imaging import face_variants, jpeg

MESSAGES = {
    "no_face": "No face detected.", "multiple_faces": "More than one face.", "too_dark": "Too dark.",
    "blurry": "Blurry.", "face_too_small": "Too far.", "not_image": "Not an image.",
}
PEOPLE = {name: np.random.default_rng(seed).normal(0, 0.1, 128) for name, seed in (("A", 1), ("B", 2), ("C", 3))}


def fake_encoding(person, variant):
    return PEOPLE[person] + variant * 0.002


def fake_analyze(data, limits=None, jitters=1):
    """Payloads: b'A:1' = person A, variant 1.  b'ERR:blurry' = an ImageError with that code."""
    text = data.decode()
    if text.startswith("ERR:"):
        code = text[4:]
        raise recognition.ImageError(code, MESSAGES[code])
    person, variant = text.split(":")
    eye = np.random.default_rng(int(variant)).random(80)
    return recognition.Analysis(fake_encoding(person, int(variant)), (eye / eye.sum()).astype(np.float32),
                                {"brightness": 120, "sharpness": 200, "face_px": 150})


def fake_quick_check(data, limits=None):
    text = data.decode()
    if text.startswith("ERR:"):
        raise recognition.ImageError(text[4:], MESSAGES[text[4:]])
    return {"brightness": 120, "sharpness": 200, "face_px": 150}


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(recognition, "analyze_image", fake_analyze)
    monkeypatch.setattr(recognition, "quick_check", fake_quick_check)


@pytest.fixture
def admin(client, users, fake):
    login(client, "admin@x.com")
    return client


def upload(client, student_id, payloads, consent="1", replace=None, token=None):
    token = token or token_when_logged_in(client)
    data = {"images": [(io.BytesIO(p), f"s{i}.jpg") for i, p in enumerate(payloads)]}
    if consent is not None:
        data["consent"] = consent
    if replace:
        data["replace"] = replace
    return client.post(f"/enrollment/student/{student_id}/save", data=data, content_type="multipart/form-data",
                       headers={"X-CSRF-Token": token})


def check(client, student_id, payload, token=None):
    token = token or token_when_logged_in(client)
    return client.post(f"/enrollment/student/{student_id}/check", content_type="multipart/form-data",
                       data={"image": (io.BytesIO(payload), "s.jpg")}, headers={"X-CSRF-Token": token})


GOOD_A = [f"A:{i}".encode() for i in range(1, 6)]
GOOD_B = [f"B:{i}".encode() for i in range(1, 6)]


def count(app, where="1=1", params=()):
    with app.app_context():
        return get_db().execute(f"SELECT COUNT(*) FROM biometric_profile WHERE {where}", params).fetchone()[0]


# ================= permissions =================

@pytest.mark.parametrize("method,path", [
    ("GET", "/enrollment/"), ("GET", "/enrollment/student/1"), ("POST", "/enrollment/student/1/check"),
    ("POST", "/enrollment/student/1/save"), ("POST", "/enrollment/student/1/remove"),
])
@pytest.mark.parametrize("who,status", [("warden@x.com", 403), ("s1@x.com", 403), (None, 302)])
def test_only_admin_can_reach_enrollment(app, client, users, fake, method, path, who, status):
    if who:
        login(client, who)
        token = token_when_logged_in(client)
    else:
        token = get_token(client)          # an anonymous visitor can obtain a valid token from the login page
    resp = client.open(path, method=method, headers={"X-CSRF-Token": token})
    assert resp.status_code == status
    assert count(app) == 0


def test_a_student_cannot_enroll_themselves_or_others(app, client, users, fake):
    login(client, "s1@x.com")
    token = token_when_logged_in(client)
    for sid in (1, 2):
        assert upload(client, sid, GOOD_A, token=token).status_code == 403
    assert count(app) == 0


def test_missing_csrf_token_is_rejected(app, admin):
    resp = admin.post("/enrollment/student/1/save", data={"consent": "1"})
    assert resp.status_code == 400 and count(app) == 0
    assert admin.post("/enrollment/student/1/check", data={}).status_code == 400
    assert admin.post("/enrollment/student/1/remove").status_code == 400


def test_camera_permission_policy_header(admin):
    assert admin.get("/enrollment/student/1").headers["Permissions-Policy"] == "camera=(self), microphone=(), geolocation=()"


# ================= check (one picture) =================

def test_good_picture_passes_and_nothing_is_stored(app, admin):
    resp = check(admin, 1, b"A:1")
    body = resp.get_json()
    assert resp.status_code == 200 and body["ok"] and body["metrics"]["face_px"] == 150
    assert count(app) == 0


@pytest.mark.parametrize("code", ["no_face", "multiple_faces", "too_dark", "blurry", "face_too_small", "not_image"])
def test_bad_pictures_get_a_clear_message(admin, code):
    resp = check(admin, 1, f"ERR:{code}".encode())
    body = resp.get_json()
    assert resp.status_code == 422 and body["ok"] is False and body["code"] == code and body["message"] == MESSAGES[code]


def test_check_without_a_file(admin):
    resp = admin.post("/enrollment/student/1/check", data={}, headers={"X-CSRF-Token": token_when_logged_in(admin)})
    assert resp.status_code == 400 and resp.get_json()["code"] == "no_image"


def test_unexpected_crash_does_not_leak_details(admin, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internal path C:\\secrets")
    monkeypatch.setattr(recognition, "quick_check", boom)
    resp = check(admin, 1, b"A:1")
    assert resp.status_code == 500 and "secret" not in resp.get_data(as_text=True)
    assert resp.get_json()["code"] == "processing_error"


def test_unknown_student_is_404(admin):
    assert check(admin, 999, b"A:1").status_code == 404
    assert upload(admin, 999, GOOD_A).status_code == 404


# ================= save =================

def test_successful_enrollment(app, admin):
    resp = upload(admin, 1, GOOD_A)
    body = resp.get_json()
    assert resp.status_code == 200 and body["ok"] and body["redirect"].endswith("/students/1")
    assert count(app, "owner_id = 1 AND kind = 'face'") == 5 and count(app, "owner_id = 1 AND kind = 'eye'") == 5
    assert "Face enrollment saved" in admin.get("/students/1").get_data(as_text=True)      # flash on the next page


def test_stored_templates_are_encrypted_with_the_app_key(app, admin):
    upload(admin, 1, GOOD_A)
    key = app.config["BIOMETRIC_KEY"]
    with app.app_context():
        faces = biometrics.load_templates(get_db(), key, "student", "face")
        raw = get_db().execute("SELECT template FROM biometric_profile").fetchall()
    assert np.allclose(faces[1][0], fake_encoding("A", 1), atol=1e-5)
    assert all(fake_encoding("A", 1).astype("<f4").tobytes() not in bytes(r["template"]) for r in raw)


def test_response_never_contains_template_values(admin):
    text = upload(admin, 1, GOOD_A).get_data(as_text=True)
    assert str(float(fake_encoding("A", 1)[0]))[:6] not in text and "encoding" not in text


def test_wrong_number_of_samples(app, admin):
    for n in (0, 1, 4, 6):
        resp = upload(admin, 1, GOOD_A[:n] if n <= 5 else GOOD_A + [b"A:6"])
        assert resp.status_code == 400 and resp.get_json()["code"] == "wrong_count"
    assert count(app) == 0


def test_consent_is_required(app, admin):
    assert upload(admin, 1, GOOD_A, consent=None).get_json()["code"] == "no_consent"
    assert upload(admin, 1, GOOD_A, consent="0").get_json()["code"] == "no_consent"
    assert count(app) == 0


def test_one_bad_sample_blocks_everything_and_names_the_sample(app, admin):
    resp = upload(admin, 1, [GOOD_A[0], GOOD_A[1], b"ERR:blurry", GOOD_A[3], GOOD_A[4]])
    body = resp.get_json()
    assert resp.status_code == 422 and body["sample"] == 3 and body["message"].startswith("Sample 3:")
    assert count(app) == 0


def test_samples_of_different_people_are_rejected(app, admin):
    resp = upload(admin, 1, [b"A:1", b"A:2", b"B:1", b"A:4", b"A:5"])
    assert resp.status_code == 422 and resp.get_json()["code"] == "inconsistent" and count(app) == 0


def test_same_face_cannot_be_enrolled_for_two_students(app, admin):
    assert upload(admin, 1, GOOD_A).status_code == 200
    resp = upload(admin, 2, GOOD_A)
    body = resp.get_json()
    assert resp.status_code == 409 and body["code"] == "duplicate_face" and "Student 1" in body["message"]
    assert count(app, "owner_id = 2") == 0


def test_a_different_face_can_be_enrolled_for_another_student(app, admin):
    upload(admin, 1, GOOD_A)
    assert upload(admin, 2, GOOD_B).status_code == 200 and count(app) == 20


def test_re_enrollment_needs_explicit_confirmation(app, admin):
    upload(admin, 1, GOOD_A)
    resp = upload(admin, 1, GOOD_A)
    assert resp.status_code == 409 and resp.get_json()["code"] == "already_enrolled"
    assert count(app) == 10


def test_re_enrollment_replaces_old_templates(app, admin):
    upload(admin, 1, GOOD_A)
    resp = upload(admin, 1, GOOD_B, replace="1")                  # the same student gets new samples (not "duplicate")
    assert resp.status_code == 200 and count(app) == 10
    with app.app_context():
        faces = biometrics.load_templates(get_db(), app.config["BIOMETRIC_KEY"], "student", "face")
    assert np.allclose(faces[1][0], fake_encoding("B", 1), atol=1e-5)


def test_failed_re_enrollment_keeps_the_old_one(app, admin):
    upload(admin, 1, GOOD_A)
    assert upload(admin, 1, [b"B:1", b"B:2", b"ERR:no_face", b"B:4", b"B:5"], replace="1").status_code == 422
    with app.app_context():
        faces = biometrics.load_templates(get_db(), app.config["BIOMETRIC_KEY"], "student", "face")
    assert np.allclose(faces[1][0], fake_encoding("A", 1), atol=1e-5)


def test_deactivated_students_cannot_be_enrolled(app, admin):
    with app.app_context():
        get_db().execute("UPDATE student SET is_active = 0 WHERE student_id = 1")
        get_db().commit()
    assert upload(admin, 1, GOOD_A).status_code == 409 and count(app) == 0
    resp = admin.get("/enrollment/student/1")
    assert resp.status_code == 302 and "/students/1" in resp.headers["Location"]


def test_enrollment_is_switched_off_without_a_key(tmp_path, users, monkeypatch):
    monkeypatch.setattr(recognition, "analyze_image", fake_analyze)
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / "nokey.db"), "BIOMETRIC_KEY": ""})
    from werkzeug.security import generate_password_hash
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO admin (full_name,email,phone,password_hash) VALUES ('A','a@x.com','1',?)", (generate_password_hash("pw-long-enough"),))
        db.execute("INSERT INTO student (registration_no,full_name,email,phone,room_no,department,session,gender,parent_name,parent_email,password_hash) "
                   "VALUES ('REG-001','S','s@x.com','1','1','CS','2022','F','P','p@x.com','h')")
        db.commit()
    client = app.test_client()
    login(client, "a@x.com", "pw-long-enough")
    resp = upload(client, 1, GOOD_A)
    assert resp.status_code == 503 and resp.get_json()["code"] == "no_key"
    page = client.get("/enrollment/student/1").get_data(as_text=True)
    assert "Enrollment is switched off" in page and 'data-disabled="1"' in page
    assert "Enrollment is switched off" in client.get("/enrollment/").get_data(as_text=True)


def test_oversized_upload_is_refused(app, admin):
    token = token_when_logged_in(admin)
    big = io.BytesIO(b"\xff\xd8\xff" + b"0" * (13 * 1024 * 1024))
    resp = admin.post("/enrollment/student/1/check", data={"image": (big, "huge.jpg")},
                      content_type="multipart/form-data", headers={"X-CSRF-Token": token})
    assert resp.status_code == 413 and b"Traceback" not in resp.data


def test_a_crash_during_analysis_changes_nothing_and_hides_details(app, admin, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(recognition, "analyze_image", boom)
    resp = upload(admin, 1, GOOD_A)
    assert resp.status_code == 500 and "secret" not in resp.get_data(as_text=True) and count(app) == 0


# ================= remove =================

def test_remove_enrollment(app, admin):
    upload(admin, 1, GOOD_A)
    upload(admin, 2, GOOD_B)
    resp = admin.post("/enrollment/student/1/remove", data={"csrf_token": token_when_logged_in(admin)})
    assert resp.status_code == 302 and count(app, "owner_id = 1") == 0 and count(app, "owner_id = 2") == 10


def test_removing_a_missing_enrollment_is_harmless(admin):
    resp = admin.post("/enrollment/student/1/remove", data={"csrf_token": token_when_logged_in(admin)}, follow_redirects=True)
    assert resp.status_code == 200 and b"no enrollment to remove" in resp.data


# ================= pages =================

def test_capture_page_content(admin):
    html = admin.get("/enrollment/student/1").get_data(as_text=True)
    assert "Student 1" in html and 'id="cam-video"' in html and 'data-samples="5"' in html and 'data-replace="0"' in html
    assert html.count('class="sample-slot"') == 5 and "agreed to have their face enrolled" in html
    assert "/enrollment/student/1/check" in html and "js/enroll.js" in html
    assert 'data-disabled="0"' in html


def test_capture_page_for_enrolled_student_offers_replace_and_remove(admin):
    upload(admin, 1, GOOD_A)
    html = admin.get("/enrollment/student/1").get_data(as_text=True)
    assert "Already enrolled" in html and 'data-replace="1"' in html and "Remove enrollment" in html
    assert "replaces" in html


def test_capture_page_never_embeds_biometric_data(admin):
    upload(admin, 1, GOOD_A)
    html = admin.get("/enrollment/student/1").get_data(as_text=True)
    assert "gAAAA" not in html                                    # Fernet tokens always start like this


def test_enrollment_list(admin):
    upload(admin, 1, GOOD_A)
    html = admin.get("/enrollment/").get_data(as_text=True)
    assert "Student 1" in html and "Student 2" in html
    assert html.count("Re-enroll") == 1                                   # student 1 is enrolled
    assert re.search(r"</svg>\s*Enroll\s*</a>", html)                      # student 2 gets a plain Enroll button
    assert "5 samples" in html
    assert "Student 2" not in admin.get("/enrollment/?show=enrolled").get_data(as_text=True)
    assert "Student 1" not in admin.get("/enrollment/?show=not_enrolled").get_data(as_text=True)
    assert b"No students match" in admin.get("/enrollment/?q=zzz").data
    assert admin.get("/enrollment/?show=bogus").status_code == 200


def test_student_detail_shows_enrollment_status(app, client, users, fake):
    login(client, "admin@x.com")
    html = client.get("/students/1").get_data(as_text=True)
    assert "Not enrolled" in html and "Enroll face" in html
    upload(client, 1, GOOD_A)
    html = client.get("/students/1").get_data(as_text=True)
    assert ">Enrolled<" in html and "Re-enroll" in html
    warden = app.test_client()
    login(warden, "warden@x.com")
    html = warden.get("/students/1").get_data(as_text=True)
    assert ">Enrolled<" in html and "Re-enroll" not in html and "/enrollment/" not in html     # status only, no controls


def test_sidebar_shows_enrollment_for_admin_only(client, users):
    login(client, "admin@x.com")
    assert 'href="/enrollment/"' in client.get("/admin/").get_data(as_text=True)
    other = client.application.test_client()
    login(other, "warden@x.com")
    assert "/enrollment/" not in other.get("/warden/").get_data(as_text=True)


# ================= real pipeline, real face =================

def test_real_face_enrollment_end_to_end(app, client, users):
    """No fakes: real detection, real encodings, real eye-region templates, real encryption."""
    login(client, "admin@x.com")
    pictures = [jpeg(v) for v in face_variants()]
    assert check(client, 1, pictures[0]).get_json()["ok"]
    resp = upload(client, 1, pictures)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    with app.app_context():
        key = app.config["BIOMETRIC_KEY"]
        faces = biometrics.load_templates(get_db(), key, "student", "face")[1]
        eyes = biometrics.load_templates(get_db(), key, "student", "eye")[1]
    assert len(faces) == 5 and faces[0].shape == (128,) and len(eyes) == 5 and eyes[0].shape == (80,)
    assert recognition.max_pairwise_distance(faces) < 0.35          # five pictures of one person agree
    assert count(app) == 10


def test_real_pipeline_rejects_a_blank_frame_and_stores_nothing(app, client, users):
    login(client, "admin@x.com")
    blank = jpeg(np.full((480, 640, 3), 128, np.uint8))
    assert check(client, 1, blank).get_json()["code"] == "no_face"
    resp = upload(client, 1, [blank] * 5)
    assert resp.status_code == 422 and resp.get_json()["sample"] == 1 and count(app) == 0


def test_real_pipeline_blocks_the_same_face_on_a_second_account(app, client, users):
    login(client, "admin@x.com")
    pictures = [jpeg(v) for v in face_variants()]
    assert upload(client, 1, pictures).status_code == 200
    resp = upload(client, 2, pictures)
    assert resp.status_code == 409 and resp.get_json()["code"] == "duplicate_face"
