"""The entry terminal routes and the entry log. Most tests replace verification.verify with a scripted answer so
every outcome can be produced on demand; the last ones run the REAL pipeline from enrollment to entry."""
import io
import sqlite3

import pytest

from app import create_app
from app.db import get_db
from app.services import verification as V
from conftest import login, token_when_logged_in
from imaging import blink_burst, face_rgb, face_variants, jpeg, paint_eyes_closed


# ---------- helpers ----------

def terminal_token(client):
    client.get("/terminal/")
    with client.session_transaction() as s:
        return s["_csrf"]


def attempt(client, n=12, token=None, remote="127.0.0.1", frames=None):
    token = token or terminal_token(client)
    frames = frames if frames is not None else [b"frame"] * n
    return client.post("/terminal/verify", content_type="multipart/form-data",
                       data={"frames": [(io.BytesIO(f), f"f{i}.jpg") for i, f in enumerate(frames)]},
                       headers={"X-CSRF-Token": token}, environ_overrides={"REMOTE_ADDR": remote})


def entries(app):
    with app.app_context():
        return get_db().execute("SELECT * FROM entry_log ORDER BY entry_id").fetchall()


def script(monkeypatch, decision):
    monkeypatch.setattr(V, "verify", lambda frames, faces, eyes, settings: decision)


def granted(student_id=1, method="Face", **kw):
    return V.Decision(True, "granted", V.MESSAGES["granted"], method=method, student_id=student_id, distance=0.31,
                      liveness="passed", checks={"face": "ok", "liveness": "ok", "recognition": "ok"}, **kw)


def denied(code="not_recognized", **kw):
    kw.setdefault("liveness", "passed")
    return V.deny(code, distance=0.7, **kw)


# ================= who may use it =================

def test_terminal_page_needs_no_login_on_the_gate_computer(client):
    resp = client.get("/terminal/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Start verification" in html and 'id="cam-video"' in html and "Staff login" in html
    for label in ("Camera", "Face detected", "Liveness", "Recognition", "Method"):
        assert label in html


def test_terminal_refuses_other_computers(client):
    assert client.get("/terminal/", environ_overrides={"REMOTE_ADDR": "192.168.1.50"}).status_code == 403
    assert client.get("/terminal/", environ_overrides={"REMOTE_ADDR": "8.8.8.8"}).status_code == 403


def test_verify_refuses_other_computers_and_logs_nothing(app, client, users, monkeypatch):
    script(monkeypatch, granted())
    token = terminal_token(client)
    assert attempt(client, token=token, remote="192.168.1.50").status_code == 403
    assert entries(app) == []


def test_ipv6_loopback_is_allowed(client):
    assert client.get("/terminal/", environ_overrides={"REMOTE_ADDR": "::1"}).status_code == 200


def test_allowed_addresses_are_configurable(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / "a.db"),
                      "TERMINAL_ADDRESSES": ["10.0.0.7"]})
    c = app.test_client()
    assert c.get("/terminal/", environ_overrides={"REMOTE_ADDR": "10.0.0.7"}).status_code == 200
    assert c.get("/terminal/", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).status_code == 403


def test_csrf_token_is_required(app, client, users, monkeypatch):
    script(monkeypatch, granted())
    resp = client.post("/terminal/verify", data={"frames": [(io.BytesIO(b"x"), "f.jpg")] * 12}, content_type="multipart/form-data")
    assert resp.status_code == 400 and entries(app) == []


def test_responses_are_never_cached(client, users, monkeypatch):
    script(monkeypatch, granted())
    assert client.get("/terminal/").headers["Cache-Control"] == "no-store"
    assert attempt(client).headers["Cache-Control"] == "no-store"


def test_page_only_asks_for_the_camera_for_this_site(client):
    assert client.get("/terminal/").headers["Permissions-Policy"].startswith("camera=(self)")


# ================= the server decides =================

def test_granted_attempt_returns_name_and_unlocks(app, client, users, monkeypatch):
    script(monkeypatch, granted(student_id=2))
    body = attempt(client).get_json()
    assert body["ok"] and body["granted"] and body["name"] == "Student 2" and body["method"] == "Face"
    assert body["code"] == "granted" and body["time"] and body["checks"]["recognition"] == "ok"


def test_denied_attempt_names_nobody(app, client, users, monkeypatch):
    script(monkeypatch, denied())
    body = attempt(client).get_json()
    assert body["ok"] and body["granted"] is False and body["name"] is None and body["method"] is None
    assert "Student" not in str(body)


def test_response_never_contains_biometric_numbers_or_scores(client, users, monkeypatch):
    script(monkeypatch, granted())
    text = attempt(client).get_data(as_text=True)
    assert "0.31" not in text and "distance" not in text and "encoding" not in text


def test_the_page_cannot_grant_access_by_itself(app, client, users, monkeypatch):
    """A tampered browser can claim anything; the log only ever reflects what the server decided."""
    script(monkeypatch, denied("liveness_failed", liveness="failed"))
    resp = client.post("/terminal/verify", content_type="multipart/form-data", headers={"X-CSRF-Token": terminal_token(client)},
                       data={"frames": [(io.BytesIO(b"x"), "f.jpg")] * 12, "granted": "1", "student_id": "1", "result": "Granted"})
    assert resp.get_json()["granted"] is False
    rows = entries(app)
    assert len(rows) == 1 and rows[0]["result"] == "Denied" and rows[0]["student_id"] is None


@pytest.mark.parametrize("count", [0, 1, 9, 41, 100])
def test_wrong_number_of_frames_is_rejected_and_not_logged(app, client, users, monkeypatch, count):
    script(monkeypatch, granted())
    resp = attempt(client, n=count)
    assert resp.status_code == 400 and resp.get_json()["code"] == "bad_request" and entries(app) == []


def test_terminal_without_a_biometric_key_is_unavailable(tmp_path, monkeypatch):
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / "n.db"), "BIOMETRIC_KEY": ""})
    c = app.test_client()
    assert "Terminal not configured" in c.get("/terminal/").get_data(as_text=True)
    resp = attempt(c)
    assert resp.status_code == 503 and resp.get_json()["code"] == "no_key"
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM entry_log").fetchone()[0] == 0


# ================= the entry log =================

def test_granted_entry_is_logged(app, client, users, monkeypatch):
    script(monkeypatch, granted(student_id=1))
    attempt(client)
    (row,) = entries(app)
    assert row["result"] == "Granted" and row["subject_type"] == "student" and row["student_id"] == 1
    assert row["method"] == "Face" and row["door_status"] == "Unlocked" and row["score"] == 0.31
    assert row["failure_reason"] is None and row["failure_code"] is None and row["liveness"] == "passed"
    assert row["needs_review"] == 0 and row["attempted_at"]


def test_eye_region_entry_is_flagged_for_review(app, client, users, monkeypatch):
    script(monkeypatch, granted(method="Eye-Region", needs_review=True))
    attempt(client)
    (row,) = entries(app)
    assert row["method"] == "Eye-Region" and row["needs_review"] == 1


@pytest.mark.parametrize("code,liveness", [("liveness_failed", "failed"), ("not_recognized", "passed"), ("ambiguous", "passed"),
                                           ("no_face", "not_checked"), ("multiple_faces", "not_checked"), ("blurry", "passed")])
def test_every_failure_is_logged_without_naming_anyone(app, client, users, monkeypatch, code, liveness):
    script(monkeypatch, V.deny(code, liveness=liveness, distance=0.62))
    attempt(client)
    (row,) = entries(app)
    assert row["result"] == "Denied" and row["door_status"] == "Locked" and row["method"] == "None"
    assert row["student_id"] is None and row["guest_id"] is None and row["subject_type"] is None   # never attributed
    assert row["failure_code"] == code and row["failure_reason"] and row["liveness"] == liveness


def test_a_crash_is_logged_as_a_denied_attempt_and_hides_details(app, client, users, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internal path C:\\secrets")
    monkeypatch.setattr(V, "verify", boom)
    resp = attempt(client)
    assert resp.status_code == 200 and "secret" not in resp.get_data(as_text=True)
    assert resp.get_json()["granted"] is False and resp.get_json()["code"] == "processing_error"
    (row,) = entries(app)
    assert row["result"] == "Denied" and row["failure_code"] == "processing_error" and row["student_id"] is None


def test_each_attempt_adds_exactly_one_row(app, client, users, monkeypatch):
    script(monkeypatch, granted())
    for _ in range(3):
        attempt(client)
    assert len(entries(app)) == 3


def test_log_rows_hold_no_biometric_data(app, client, users, monkeypatch):
    script(monkeypatch, granted())
    attempt(client)
    (row,) = entries(app)
    text = " ".join(str(row[k]) for k in row.keys())
    assert "gAAAA" not in text and "[" not in text


def test_timestamps_are_utc_text(app, client, users, monkeypatch):
    script(monkeypatch, granted())
    attempt(client)
    import re
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", entries(app)[0]["attempted_at"])


def test_dashboards_show_the_attempts(app, client, users, monkeypatch):
    script(monkeypatch, granted(student_id=1))
    attempt(client)
    script(monkeypatch, denied("liveness_failed", liveness="failed"))
    attempt(client)
    login(client, "admin@x.com")
    html = client.get("/admin/").get_data(as_text=True)
    assert "Student 1" in html and "Unidentified" in html and "Liveness check failed" in html


# ================= what the gallery contains =================

def test_only_active_enrolled_students_are_offered_for_matching(app, client, users, monkeypatch):
    import numpy as np
    from app.services import biometrics
    key = app.config["BIOMETRIC_KEY"]
    with app.app_context():
        db = get_db()
        for sid, seed in ((1, 1), (2, 2)):
            biometrics.save_enrollment(db, key, "student", sid, [np.random.default_rng(seed).normal(0, 0.1, 128)] * 5,
                                       [np.ones(80, dtype=np.float32) / 80] * 5)
        db.execute("UPDATE student SET is_active = 0 WHERE student_id = 2")
        db.commit()
    seen = {}

    def spy(frames, faces, eyes, settings):
        seen["faces"], seen["eyes"] = set(faces), set(eyes)
        return denied()
    monkeypatch.setattr(V, "verify", spy)
    attempt(client)
    assert seen["faces"] == {1}                       # the deactivated student can never be matched
    assert seen["eyes"] == set()                      # eye templates are not even loaded while the fallback is off


def test_eye_templates_are_loaded_only_when_the_fallback_is_on(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / "f.db"),
                      "BIOMETRIC_KEY": Fernet.generate_key().decode(), "EYE_FALLBACK_ENABLED": True})
    seen = {}
    monkeypatch.setattr(V, "verify", lambda frames, faces, eyes, settings: seen.update(on=settings.eye_fallback) or denied())
    attempt(app.test_client())
    assert seen["on"] is True


# ================= navigation and old databases =================

def test_sidebar_links_to_the_terminal_for_staff_only(client, users):
    login(client, "admin@x.com")
    assert 'href="/terminal/"' in client.get("/admin/").get_data(as_text=True)
    warden = client.application.test_client()
    login(warden, "warden@x.com")
    assert 'href="/terminal/"' in warden.get("/warden/").get_data(as_text=True)
    student = client.application.test_client()
    login(student, "s1@x.com")
    assert "/terminal/" not in student.get("/student/").get_data(as_text=True)


def test_old_database_gets_the_new_columns_without_losing_data(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE entry_log (entry_id INTEGER PRIMARY KEY AUTOINCREMENT, subject_type TEXT, student_id INTEGER, "
                "guest_id INTEGER, visit_request_id INTEGER, attempted_at TEXT NOT NULL, method TEXT NOT NULL DEFAULT 'None', "
                "result TEXT NOT NULL, score REAL, failure_reason TEXT, door_status TEXT NOT NULL, needs_review INTEGER NOT NULL DEFAULT 0)")
    con.execute("INSERT INTO entry_log (attempted_at, result, door_status) VALUES ('2026-01-01T10:00:00', 'Denied', 'Locked')")
    con.commit()
    con.close()
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(path)})
    with app.app_context():
        db = get_db()
        cols = {r[1] for r in db.execute("PRAGMA table_info(entry_log)")}
        assert {"failure_code", "liveness"} <= cols
        assert db.execute("SELECT COUNT(*) FROM entry_log").fetchone()[0] == 1
        from app.db import init_db
        init_db()                                               # running it again is harmless
        assert db.execute("SELECT COUNT(*) FROM entry_log").fetchone()[0] == 1


# ================= real pipeline: enrollment -> entry =================

@pytest.fixture(scope="module")
def frames():
    open_img = face_rgb()
    return {"blink": blink_burst(open_img, paint_eyes_closed(open_img), "OOOOOOOOCCOOOOOOOOOOOOOO"),
            "photo": blink_burst(open_img, open_img, "O" * 20)}


def enroll_for_real(client, student_id):
    token = token_when_logged_in(client)
    pictures = [jpeg(v) for v in face_variants()]
    resp = client.post(f"/enrollment/student/{student_id}/save", content_type="multipart/form-data",
                       data={"images": [(io.BytesIO(p), f"s{i}.jpg") for i, p in enumerate(pictures)], "consent": "1"},
                       headers={"X-CSRF-Token": token})
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_real_end_to_end_enrolled_student_enters(app, client, users, frames):
    login(client, "admin@x.com")
    enroll_for_real(client, 1)
    gate = app.test_client()                                    # the terminal is not logged in
    body = attempt(gate, frames=frames["blink"]).get_json()
    assert body["granted"] and body["name"] == "Student 1" and body["method"] == "Face", body
    (row,) = entries(app)
    assert row["result"] == "Granted" and row["student_id"] == 1 and row["liveness"] == "passed" and 0 <= row["score"] < 0.2


def test_real_end_to_end_photo_attack_is_denied_and_logged(app, client, users, frames):
    login(client, "admin@x.com")
    enroll_for_real(client, 1)
    gate = app.test_client()
    body = attempt(gate, frames=frames["photo"]).get_json()
    assert body["granted"] is False and body["code"] == "liveness_failed" and body["name"] is None
    (row,) = entries(app)
    assert row["result"] == "Denied" and row["student_id"] is None and row["failure_code"] == "liveness_failed" and row["liveness"] == "failed"


def test_real_end_to_end_deactivated_student_is_refused(app, client, users, frames):
    login(client, "admin@x.com")
    enroll_for_real(client, 1)
    with app.app_context():
        get_db().execute("UPDATE student SET is_active = 0 WHERE student_id = 1")
        get_db().commit()
    body = attempt(app.test_client(), frames=frames["blink"]).get_json()
    assert body["granted"] is False and body["code"] == "no_enrolled"
    assert entries(app)[0]["student_id"] is None
