"""Covered face (niqab): enrollment of the eye slit and the 1:1 check at the gate (registration number + eyes).

The pipeline tests use a SIMULATED niqab (dark cloth over a real face picture) and a simulated blink. They prove the steps work;
they say nothing about real-world accuracy (docs/recognition-limits.md, section 3b)."""
import io

import numpy as np
import pytest

from app.db import get_db
from app.services import biometrics, covered, entries, recognition
from app.services import verification as V
from conftest import login, token_when_logged_in
from imaging import blink_burst, face_rgb, jpeg, paint_eyes_closed
from test_covered_face import covered as cover_up

S = V.Settings()


@pytest.fixture(scope="module")
def pictures():
    base = face_rgb()
    return cover_up(base), cover_up(paint_eyes_closed(base)), base


@pytest.fixture(scope="module")
def own_template(pictures):
    opened, _, _ = pictures
    encoding, _ = covered.analyze_covered_image(jpeg(opened))
    return [encoding]


# ---------- the pipeline ----------

def test_enrollment_accepts_a_covered_face_and_rejects_a_visible_one(pictures):
    opened, _, base = pictures
    assert covered.quick_check(jpeg(opened))["face_px"] > 90
    with pytest.raises(recognition.ImageError) as err:
        covered.quick_check(jpeg(base))
    assert err.value.code == "not_covered"


def test_the_right_student_blinking_is_granted_and_flagged(pictures, own_template):
    opened, closed, _ = pictures
    d = covered.verify_covered(blink_burst(opened, closed), 7, own_template, S, claimed="REG-007")
    assert d.granted and d.student_id == 7 and d.method == "Eye-Region" and d.needs_review
    assert d.distance < S.covered_threshold


def test_a_different_person_with_the_right_number_is_refused(pictures):
    opened, closed, _ = pictures
    other = [np.random.default_rng(5).normal(0, 0.1, 128)]
    d = covered.verify_covered(blink_burst(opened, closed), 7, other, S, claimed="REG-007")
    assert not d.granted and d.code == "covered_not_matched" and d.student_id is None and d.claimed == "REG-007"


def test_a_still_picture_without_a_blink_is_refused(pictures, own_template):
    opened, _, _ = pictures
    d = covered.verify_covered([jpeg(opened)] * 16, 7, own_template, S)
    assert not d.granted and d.code == "liveness_failed" and d.student_id is None


def test_a_visible_face_cannot_use_the_weaker_covered_check(pictures, own_template):
    _, _, base = pictures
    d = covered.verify_covered([jpeg(base)] * 16, 7, own_template, S)
    assert not d.granted and d.code == "face_visible"


def test_no_covered_enrollment_is_refused_with_the_warden_message():
    d = covered.verify_covered([b"x"] * 12, 7, [], S)
    assert not d.granted and d.code == "covered_not_enrolled" and "warden" in d.message


def test_failure_codes_have_log_labels_and_count_towards_alerts():
    from app.services import alerts
    for code in ("covered_not_matched", "covered_not_enrolled", "face_visible"):
        assert code in entries.FAILURE_LABELS
    assert "covered_not_matched" in alerts.COUNTED_CODES


# ---------- the gate route ----------

def post_verify(client, registration_no=None, n=12):
    client.get("/terminal/")
    with client.session_transaction() as s:
        token = s["_csrf"]
    data = {"frames": [(io.BytesIO(b"frame"), f"f{i}.jpg") for i in range(n)]}
    if registration_no is not None:
        data["registration_no"] = registration_no
    return client.post("/terminal/verify", content_type="multipart/form-data", data=data,
                       headers={"X-CSRF-Token": token}, environ_overrides={"REMOTE_ADDR": "127.0.0.1"})


def enroll_covered(app, student_id=1):
    with app.app_context():
        biometrics.save_covered_enrollment(get_db(), app.config["BIOMETRIC_KEY"], student_id, [np.zeros(128), np.ones(128) * 0.01])


def entry_rows(app):
    with app.app_context():
        return get_db().execute("SELECT * FROM entry_log ORDER BY entry_id").fetchall()


def test_gate_compares_only_the_claimed_students_templates(app, client, users, monkeypatch):
    enroll_covered(app, 1)
    seen = {}

    def fake(frames, student_id, templates, settings, claimed=None):
        seen.update(student_id=student_id, count=len(templates), claimed=claimed)
        return V.Decision(True, "granted", "Access granted.", method="Eye-Region", student_id=student_id, distance=0.2,
                          liveness="passed", needs_review=True, claimed=claimed,
                          checks={"face": "ok", "liveness": "ok", "recognition": "ok"})
    monkeypatch.setattr(covered, "verify_covered", fake)
    body = post_verify(client, "reg-001").get_json()          # case does not matter
    assert body["granted"] and body["name"] == "Student 1" and body["method"] == "Eye-Region"
    assert seen == {"student_id": 1, "count": 2, "claimed": "reg-001"}
    row = entry_rows(app)[0]
    assert row["result"] == "Granted" and row["student_id"] == 1 and row["method"] == "Eye-Region" and row["needs_review"] == 1


def test_unknown_number_and_unenrolled_student_look_the_same(app, client, users):
    enroll_covered(app, 1)
    unknown = post_verify(client, "NOPE").get_json()
    not_enrolled = post_verify(client, "REG-002").get_json()                      # exists, but no covered enrollment
    assert unknown["message"] == not_enrolled["message"] and not unknown["granted"] and not not_enrolled["granted"]
    assert [r["failure_code"] for r in entry_rows(app)] == ["covered_not_enrolled", "covered_not_enrolled"]


def test_a_deactivated_student_cannot_use_the_covered_gate(app, client, users):
    enroll_covered(app, 1)
    with app.app_context():
        get_db().execute("UPDATE student SET is_active = 0 WHERE student_id = 1")
        get_db().commit()
    assert not post_verify(client, "REG-001").get_json()["granted"]


def test_a_denial_is_logged_with_the_typed_number_but_not_linked_to_a_student(app, client, users, monkeypatch):
    enroll_covered(app, 1)
    monkeypatch.setattr(covered, "verify_covered", lambda frames, sid, t, s, claimed=None:
                        V.deny("covered_not_matched", distance=0.7, liveness="passed", claimed=claimed))
    body = post_verify(client, "REG-001").get_json()
    assert not body["granted"] and "warden" in body["message"]
    row = entry_rows(app)[0]
    assert row["student_id"] is None and row["result"] == "Denied" and "REG-001" in row["failure_reason"]


def test_without_a_number_the_normal_path_is_used(app, client, users, monkeypatch):
    called = []
    monkeypatch.setattr(V, "verify", lambda frames, faces, eyes, settings, **kw: called.append(1) or V.deny("no_face"))
    monkeypatch.setattr(covered, "verify_covered", lambda *a, **k: pytest.fail("covered path must not run"))
    assert post_verify(client).get_json()["code"] == "no_face" and called


# ---------- covered enrollment pages ----------

def fake_covered_analyze(data, limits=None):
    if data.startswith(b"ERR"):
        raise recognition.ImageError("not_covered", "The face is visible.")
    return np.random.default_rng(int(data.decode())).normal(0, 0.05, 128) * 0 + 0.01 * int(data.decode()), {"brightness": 120, "sharpness": 90, "face_px": 150}


def upload(client, student_id, payloads, **extra):
    token = token_when_logged_in(client)
    data = {"images": [(io.BytesIO(p), f"s{i}.jpg") for i, p in enumerate(payloads)], "consent": "1", **extra}
    return client.post(f"/enrollment/student/{student_id}/covered/save", content_type="multipart/form-data", data=data,
                       headers={"X-CSRF-Token": token})


@pytest.fixture
def admin(client, users, monkeypatch):
    monkeypatch.setattr(covered, "analyze_covered_image", fake_covered_analyze)
    login(client, "admin@x.com")
    return client


def enroll_normal(app, student_id=1):
    with app.app_context():
        biometrics.save_enrollment(get_db(), app.config["BIOMETRIC_KEY"], "student", student_id, [np.zeros(128)] * 2, [np.zeros(80)] * 2)


def test_covered_page_needs_the_normal_enrollment_first(app, admin):
    assert admin.get("/enrollment/student/1/covered").status_code == 302
    enroll_normal(app)
    assert admin.get("/enrollment/student/1/covered").status_code == 200


def test_covered_enrollment_saves_five_samples_and_can_be_replaced_and_removed(app, admin):
    enroll_normal(app)
    assert upload(admin, 1, [b"1", b"2", b"3", b"4", b"5"]).get_json()["ok"]
    with app.app_context():
        assert biometrics.covered_status(get_db(), 1)["samples"] == 5
    assert upload(admin, 1, [b"1", b"2", b"3", b"4", b"5"]).status_code == 409           # needs confirmation to replace
    assert upload(admin, 1, [b"1", b"2", b"3", b"4", b"5"], replace="1").get_json()["ok"]
    token = token_when_logged_in(admin)
    admin.post("/enrollment/student/1/covered/remove", data={"csrf_token": token})
    with app.app_context():
        assert not biometrics.covered_status(get_db(), 1)["enrolled"]


def test_covered_enrollment_rejects_a_bad_sample_and_inconsistent_people(app, admin):
    enroll_normal(app)
    bad = upload(admin, 1, [b"1", b"ERR", b"3", b"4", b"5"])
    assert bad.status_code == 422 and bad.get_json()["sample"] == 2
    far = upload(admin, 1, [b"1", b"2", b"3", b"4", b"90"])                              # 0.01 * 90 apart: not one person
    assert far.status_code == 422 and far.get_json()["code"] == "inconsistent"


def test_only_the_admin_can_enroll_covered_faces(app, client, users, monkeypatch):
    monkeypatch.setattr(covered, "analyze_covered_image", fake_covered_analyze)
    login(client, "warden@x.com")
    assert client.get("/enrollment/student/1/covered").status_code == 403
