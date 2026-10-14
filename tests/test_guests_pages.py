"""Guest visits through the website: requests, decisions, face registration, the entry terminal, exits."""
import io
from datetime import date, timedelta

import numpy as np
import pytest

from app.db import get_db
from app.services import biometrics, guests, recognition
from app.services import verification as V
from conftest import get_token, login, token_when_logged_in
from test_enrollment import GOOD_A, GOOD_B, fake_analyze, fake_encoding, fake_quick_check
from test_guests_logic import local_today, make_visit, row_of, stamp
from imaging import blink_burst, face_rgb, face_variants, jpeg, paint_eyes_closed


def future(app, days=1):
    return (date.fromisoformat(local_today(app)) + timedelta(days=days)).isoformat()


def client_as(app, email):
    c = app.test_client()
    login(c, email)
    return c


def student(app, email="s1@x.com"):
    return client_as(app, email)


def warden(app):
    return client_as(app, "warden@x.com")


def admin_c(app):
    return client_as(app, "admin@x.com")


def form(app, **over):
    return {"guest_name": "Zara Guest", "guest_contact": "0300 1234567", "purpose": "Family visit", "visit_date": future(app),
            "from_time": "15:00", "to_time": "18:00", **over}


def send(client, data, path="/guests/new"):
    return client.post(path, data={**data, "csrf_token": token_when_logged_in(client)})


def count(app, table, where="1=1"):
    with app.app_context():
        return get_db().execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


# ================= who can open what =================

@pytest.mark.parametrize("path", ["/guests/", "/guests/new"])
def test_student_pages_are_for_students_only(app, users, path):
    assert student(app).get(path).status_code == 200
    assert warden(app).get(path).status_code == 403 and admin_c(app).get(path).status_code == 403


@pytest.mark.parametrize("path", ["/guests/requests", "/guests/requests/1"])
def test_staff_pages_are_for_staff_only(app, users, path):
    make_visit(app)
    assert warden(app).get(path).status_code == 200 and admin_c(app).get(path).status_code == 200
    assert student(app).get(path).status_code == 403


@pytest.mark.parametrize("path", ["/guests/", "/guests/new", "/guests/requests", "/guests/1", "/guests/requests/1/enroll"])
def test_anonymous_visitors_are_sent_to_login(app, users, path):
    resp = app.test_client().get(path)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


# ================= a student asks for a visit =================

def test_student_creates_a_request(app, users):
    c = student(app)
    resp = send(c, form(app))
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/guests/1")
    with app.app_context():
        r = get_db().execute("SELECT * FROM guest_visit_request").fetchone()
        g = get_db().execute("SELECT * FROM guest_profile").fetchone()
    assert r["status"] == "Pending" and r["student_id"] == 1 and r["max_entries"] == 1 and r["entries_used"] == 0
    assert g["full_name"] == "Zara Guest" and g["host_student_id"] == 1
    assert "Zara Guest" in c.get("/guests/").get_data(as_text=True) and "Pending" in c.get("/guests/").get_data(as_text=True)


def test_a_student_cannot_request_on_behalf_of_another_student(app, users):
    c = student(app, "s1@x.com")
    send(c, {**form(app), "student_id": "2", "host_student_id": "2", "status": "Approved", "entries_used": "9", "max_entries": "9"})
    with app.app_context():
        r = get_db().execute("SELECT * FROM guest_visit_request").fetchone()
        g = get_db().execute("SELECT * FROM guest_profile").fetchone()
    assert (r["student_id"], g["host_student_id"], r["status"], r["max_entries"], r["entries_used"]) == (1, 1, "Pending", 1, 0)


def test_invalid_requests_show_errors_and_save_nothing(app, users):
    c = student(app)
    resp = send(c, form(app, guest_name="", from_time="18:00", to_time="15:00", visit_date="2020-01-01"))
    html = resp.get_data(as_text=True)
    assert resp.status_code == 400 and html.count("invalid-feedback") >= 3 and "is-invalid" in html
    assert count(app, "guest_visit_request") == 0 and count(app, "guest_profile") == 0


def test_form_keeps_what_was_typed(app, users):
    html = send(student(app), form(app, guest_name="Keep Me", purpose="")).get_data(as_text=True)
    assert 'value="Keep Me"' in html


def test_csrf_is_required(app, users):
    c = student(app)
    c.get("/guests/new")
    assert c.post("/guests/new", data=form(app)).status_code == 400 and count(app, "guest_visit_request") == 0


def test_a_returning_guest_can_be_chosen(app, users):
    _, gid = make_visit(app, 1, "Approved", name="Old Friend")
    c = student(app)
    assert "Old Friend" in c.get("/guests/new").get_data(as_text=True)
    send(c, {**form(app), "previous_guest_id": str(gid), "guest_name": "", "guest_contact": ""})
    assert count(app, "guest_profile") == 1 and count(app, "guest_visit_request", f"guest_id = {gid}") == 2


def test_another_students_guest_is_not_offered_or_accepted(app, users):
    _, gid = make_visit(app, 2, name="Other Guest")
    c = student(app, "s1@x.com")
    assert "Other Guest" not in c.get("/guests/new").get_data(as_text=True)
    resp = send(c, {**form(app), "previous_guest_id": str(gid)})
    assert resp.status_code == 400 and count(app, "guest_visit_request") == 1


def test_students_see_only_their_own_requests(app, users):
    rid1, _ = make_visit(app, 1, name="Mine")
    rid2, _ = make_visit(app, 2, name="Theirs")
    c = student(app, "s1@x.com")
    html = c.get("/guests/").get_data(as_text=True)
    assert "Mine" in html and "Theirs" not in html
    assert c.get(f"/guests/{rid1}").status_code == 200
    assert c.get(f"/guests/{rid2}").status_code == 404                       # looks exactly like a request that does not exist
    assert c.get("/guests/9999").status_code == 404


def test_students_do_not_see_host_details_or_staff_controls(app, users):
    rid, _ = make_visit(app, 1)
    html = student(app).get(f"/guests/{rid}").get_data(as_text=True)
    assert "Host student" not in html and "Approve" not in html and "Register face" not in html


def test_empty_states(app, users):
    assert "No guest requests yet" in student(app).get("/guests/").get_data(as_text=True)
    assert "No guest requests found" in warden(app).get("/guests/requests").get_data(as_text=True)


# ================= the warden decides =================

def decide(c, rid, action, entries=None):
    data = {"action": action, "csrf_token": token_when_logged_in(c)}
    if entries is not None:
        data["entries"] = entries
    return c.post(f"/guests/requests/{rid}/decide", data=data)


def test_warden_approves_with_the_default_of_one_entry(app, users):
    rid, _ = make_visit(app)
    assert decide(warden(app), rid, "approve", 1).status_code == 302
    r = row_of(app, rid)
    assert (r["status"], r["max_entries"], r["decided_by"]) == ("Approved", 1, "Wendy Warden") and r["decided_at"]


def test_warden_can_allow_more_entries_explicitly(app, users):
    rid, _ = make_visit(app)
    decide(warden(app), rid, "approve", 3)
    assert row_of(app, rid)["max_entries"] == 3


@pytest.mark.parametrize("n", [0, 4, -2])
def test_invalid_entry_counts_leave_the_request_pending(app, users, n):
    rid, _ = make_visit(app)
    decide(warden(app), rid, "approve", n)
    assert row_of(app, rid)["status"] == "Pending"


def test_warden_rejects(app, users):
    rid, _ = make_visit(app)
    decide(warden(app), rid, "reject")
    r = row_of(app, rid)
    assert r["status"] == "Rejected" and r["decided_by"] == "Wendy Warden"


def test_a_decision_is_final(app, users):
    rid, _ = make_visit(app)
    c = warden(app)
    decide(c, rid, "approve", 1)
    page = c.get(decide(c, rid, "reject").headers["Location"]).get_data(as_text=True)
    assert "already decided" in page and row_of(app, rid)["status"] == "Approved"


def test_admin_and_students_cannot_decide(app, users):
    rid, _ = make_visit(app)
    assert decide(admin_c(app), rid, "approve", 1).status_code == 403
    assert decide(student(app), rid, "approve", 1).status_code == 403
    assert row_of(app, rid)["status"] == "Pending"


def test_decisions_need_csrf_and_a_valid_action(app, users):
    rid, _ = make_visit(app)
    c = warden(app)
    assert c.post(f"/guests/requests/{rid}/decide", data={"action": "approve"}).status_code == 400
    assert decide(c, rid, "delete").status_code == 400 and decide(c, rid, "").status_code == 400
    assert row_of(app, rid)["status"] == "Pending"


def test_unknown_request_is_404_or_a_message(app, users):
    assert warden(app).get("/guests/requests/999").status_code == 404
    c = warden(app)
    assert decide(c, 999, "approve", 1).status_code == 302 and count(app, "guest_visit_request") == 0


def test_an_expired_request_cannot_be_approved_from_the_page(app, users):
    rid, _ = make_visit(app, start=-6, end=-2)
    c = warden(app)
    page = c.get(decide(c, rid, "approve", 1).headers["Location"]).get_data(as_text=True)
    assert row_of(app, rid)["status"] == "Pending" and "no longer be approved" in page


def test_review_page_shows_host_details_and_the_decision_form_to_wardens(app, users):
    rid, _ = make_visit(app, 2, name="Zara")
    html = warden(app).get(f"/guests/requests/{rid}").get_data(as_text=True)
    assert "Host student" in html and "Student 2" in html and "REG-002" in html and "Entries allowed" in html and ">Approve<" in html
    assert 'data-confirm="Reject this visit request for Zara?' in html


def test_admin_sees_the_visit_but_has_no_decision_buttons(app, users):
    rid, _ = make_visit(app)
    html = admin_c(app).get(f"/guests/requests/{rid}").get_data(as_text=True)
    assert "Host student" in html and ">Approve<" not in html and "Waiting for the warden" in html


def test_previous_decisions_are_listed_with_who_and_when(app, users):
    rid, _ = make_visit(app, status="Approved")
    make_visit(app, status="Rejected", name="Nope")
    c = warden(app)
    assert "Approved" in c.get("/guests/requests?status=Approved").get_data(as_text=True)
    html = c.get(f"/guests/requests/{rid}").get_data(as_text=True)
    assert "Decided by" in html and "Wendy Warden" in html


def test_list_defaults_to_pending_and_filters(app, users):
    make_visit(app, status="Pending", name="Waiting One")
    make_visit(app, status="Approved", name="Approved One")
    make_visit(app, status="Rejected", name="Rejected One")
    make_visit(app, status="Approved", used=1, name="Used One")
    make_visit(app, status="Approved", start=-6, end=-2, name="Expired One")
    c = warden(app)
    default = c.get("/guests/requests").get_data(as_text=True)
    assert "Waiting One" in default and "Approved One" not in default
    expected = {"Pending": "Waiting One", "Approved": "Approved One", "Rejected": "Rejected One", "Used": "Used One", "Expired": "Expired One"}
    for status, name in expected.items():
        html = c.get(f"/guests/requests?status={status}").get_data(as_text=True)
        assert name in html and sum(n in html for n in expected.values()) == 1, status
    everything = c.get("/guests/requests?status=all").get_data(as_text=True)
    assert all(n in everything for n in expected.values())


def test_search_by_guest_or_host(app, users):
    make_visit(app, 1, name="Alice Guest")
    make_visit(app, 2, name="Bob Guest")
    c = warden(app)
    assert "Bob Guest" not in c.get("/guests/requests?status=all&q=alice").get_data(as_text=True)
    assert "Bob Guest" in c.get("/guests/requests?status=all&q=Student 2").get_data(as_text=True)
    assert "No guest requests found" in c.get("/guests/requests?status=all&q=%25").get_data(as_text=True)
    for q in ("' OR 1=1 --", "<script>alert(1)</script>"):
        resp = c.get("/guests/requests", query_string={"q": q, "status": "all"})
        assert resp.status_code == 200 and b"<script>alert(1)</script>" not in resp.data


def test_names_are_escaped(app, users):
    make_visit(app, name="<b>Bold</b>")
    assert "&lt;b&gt;Bold" in warden(app).get("/guests/requests").get_data(as_text=True)


def test_bad_filter_values_are_ignored(app, users):
    c = warden(app)
    assert c.get("/guests/requests?status=bogus&page=-3&from=x").status_code == 200


# ================= registering the guest's face =================

@pytest.fixture
def fake_images(monkeypatch):
    monkeypatch.setattr(recognition, "analyze_image", fake_analyze)
    monkeypatch.setattr(recognition, "quick_check", fake_quick_check)


def upload(c, rid, payloads, consent="1", replace=None):
    data = {"images": [(io.BytesIO(p), f"s{i}.jpg") for i, p in enumerate(payloads)], "csrf_token": token_when_logged_in(c)}
    if consent is not None:
        data["consent"] = consent
    if replace:
        data["replace"] = replace
    return c.post(f"/guests/requests/{rid}/enroll/save", data=data, content_type="multipart/form-data")


def guest_templates(app, guest_id):
    with app.app_context():
        return biometrics.enrollment_status(get_db(), "guest", guest_id)


def test_warden_registers_the_face_of_an_approved_guest(app, users, fake_images):
    rid, gid = make_visit(app, status="Approved")
    resp = upload(warden(app), rid, GOOD_A)
    assert resp.status_code == 200 and resp.get_json()["ok"]
    status = guest_templates(app, gid)
    assert status["enrolled"] and status["face_samples"] == 5 and status["eye_samples"] == 5


def test_templates_are_stored_encrypted_under_the_guest(app, users, fake_images):
    rid, gid = make_visit(app, status="Approved")
    upload(warden(app), rid, GOOD_A)
    with app.app_context():
        rows = get_db().execute("SELECT owner_type, owner_id, template FROM biometric_profile").fetchall()
        faces = biometrics.load_templates(get_db(), app.config["BIOMETRIC_KEY"], "guest", "face")
    assert {(r["owner_type"], r["owner_id"]) for r in rows} == {("guest", gid)}
    assert all(bytes(r["template"]).startswith(b"gAAAA") for r in rows) and gid in faces


@pytest.mark.parametrize("status", ["Pending", "Rejected"])
def test_a_face_cannot_be_registered_before_approval(app, users, fake_images, status):
    rid, gid = make_visit(app, status=status)
    c = warden(app)
    assert upload(c, rid, GOOD_A).status_code == 409
    assert guest_templates(app, gid)["enrolled"] is False
    assert c.get(f"/guests/requests/{rid}/enroll").status_code == 302


def test_students_cannot_register_guest_faces(app, users, fake_images):
    rid, gid = make_visit(app, status="Approved")
    c = student(app)
    assert upload(c, rid, GOOD_A).status_code == 403 and c.get(f"/guests/requests/{rid}/enroll").status_code == 403
    assert c.post(f"/guests/requests/{rid}/enroll/check", data={"csrf_token": token_when_logged_in(c)}).status_code == 403


def test_admin_may_register_and_delete_guest_face_data(app, users, fake_images):
    rid, gid = make_visit(app, status="Approved")
    c = admin_c(app)
    assert upload(c, rid, GOOD_A).status_code == 200
    assert c.post(f"/guests/requests/{rid}/enroll/remove", data={"csrf_token": token_when_logged_in(c)}).status_code == 302
    assert guest_templates(app, gid)["enrolled"] is False


def test_consent_count_and_bad_samples_are_enforced(app, users, fake_images):
    rid, gid = make_visit(app, status="Approved")
    c = warden(app)
    assert upload(c, rid, GOOD_A, consent=None).get_json()["code"] == "no_consent"
    assert upload(c, rid, GOOD_A[:3]).get_json()["code"] == "wrong_count"
    bad = upload(c, rid, [GOOD_A[0], b"ERR:blurry", *GOOD_A[2:]])
    assert bad.status_code == 422 and bad.get_json()["sample"] == 2
    assert upload(c, rid, [b"A:1", b"A:2", b"B:1", b"A:4", b"A:5"]).get_json()["code"] == "inconsistent"
    assert guest_templates(app, gid)["enrolled"] is False


def test_replacing_needs_confirmation(app, users, fake_images):
    rid, gid = make_visit(app, status="Approved")
    c = warden(app)
    upload(c, rid, GOOD_A)
    assert upload(c, rid, GOOD_A).get_json()["code"] == "already_enrolled"
    assert upload(c, rid, GOOD_A, replace="1").status_code == 200


def test_a_students_face_cannot_become_a_guest(app, users, fake_images):
    with app.app_context():
        biometrics.save_enrollment(get_db(), app.config["BIOMETRIC_KEY"], "student", 1,
                                   [fake_encoding("A", i) for i in range(1, 6)], [np.ones(80, dtype=np.float32) / 80] * 5)
    rid, gid = make_visit(app, 2, status="Approved")
    resp = upload(warden(app), rid, GOOD_A)
    assert resp.status_code == 409 and resp.get_json()["code"] == "is_student" and "Student 1" in resp.get_json()["message"]
    assert guest_templates(app, gid)["enrolled"] is False


def test_a_deactivated_students_face_is_still_protected(app, users, fake_images):
    with app.app_context():
        biometrics.save_enrollment(get_db(), app.config["BIOMETRIC_KEY"], "student", 1,
                                   [fake_encoding("A", i) for i in range(1, 6)], [np.ones(80, dtype=np.float32) / 80] * 5)
        get_db().execute("UPDATE student SET is_active = 0 WHERE student_id = 1")
        get_db().commit()
    rid, _ = make_visit(app, 2, status="Approved")
    assert upload(warden(app), rid, GOOD_A).status_code == 409


def test_a_returning_guest_is_recognised_and_their_profile_is_reused(app, users, fake_images):
    rid_old, gid_old = make_visit(app, 1, status="Approved", name="Returning Rita")
    c = warden(app)
    upload(c, rid_old, GOOD_A)
    rid_new, gid_new = make_visit(app, 2, status="Approved", name="Rita Again")           # a second host registered her under a new profile
    resp = upload(c, rid_new, GOOD_A)
    body = resp.get_json()
    assert resp.status_code == 200 and body["ok"] and "Returning" in body["message"]
    assert row_of(app, rid_new)["guest_id"] == gid_old                                     # the visit now points at the old profile
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM guest_profile WHERE guest_id = ?", (gid_new,)).fetchone()[0] == 0
        assert get_db().execute("SELECT COUNT(*) FROM biometric_profile WHERE owner_id = ?", (gid_new,)).fetchone()[0] == 0
    assert guest_templates(app, gid_old)["face_samples"] == 5                              # nothing new was stored
    assert row_of(app, rid_new)["guest_name"] == "Returning Rita"


def test_two_different_guests_stay_separate(app, users, fake_images):
    rid1, gid1 = make_visit(app, 1, status="Approved", name="One")
    rid2, gid2 = make_visit(app, 1, status="Approved", name="Two")
    c = warden(app)
    assert upload(c, rid1, GOOD_A).status_code == 200 and upload(c, rid2, GOOD_B).status_code == 200
    assert guest_templates(app, gid1)["enrolled"] and guest_templates(app, gid2)["enrolled"] and gid1 != gid2


def test_only_guests_with_an_approved_visit_can_be_matched(app, users, fake_images):
    rid, gid = make_visit(app, status="Approved")
    upload(warden(app), rid, GOOD_A)
    with app.app_context():
        key = app.config["BIOMETRIC_KEY"]
        assert gid in biometrics.load_templates(get_db(), key, "guest", "face")
        get_db().execute("UPDATE guest_visit_request SET status = 'Rejected'")
        get_db().commit()
        assert biometrics.load_templates(get_db(), key, "guest", "face") == {}
        assert gid in biometrics.load_templates(get_db(), key, "guest", "face", only_active=False)


def test_enrollment_page_is_reused_for_guests(app, users):
    rid, _ = make_visit(app, status="Approved", name="Zara")
    html = warden(app).get(f"/guests/requests/{rid}/enroll").get_data(as_text=True)
    assert "Face enrollment (guest)" in html and "Zara" in html and f"/guests/requests/{rid}/enroll/save" in html
    assert "Student 1" in html and "Guest of" in html and 'id="cam-video"' in html


def test_guest_enrollment_needs_csrf(app, users, fake_images):
    rid, gid = make_visit(app, status="Approved")
    c = warden(app)
    c.get("/guests/requests")
    resp = c.post(f"/guests/requests/{rid}/enroll/save", data={"images": [(io.BytesIO(p), "s.jpg") for p in GOOD_A], "consent": "1"},
                  content_type="multipart/form-data")
    assert resp.status_code == 400 and guest_templates(app, gid)["enrolled"] is False


# ================= the entry terminal =================

def gate(app, monkeypatch, decision, remote="127.0.0.1"):
    monkeypatch.setattr(V, "verify", lambda frames, faces, eyes, settings, **kw: decision)
    c = app.test_client()
    c.get("/terminal/")
    with c.session_transaction() as s:
        token = s["_csrf"]
    return c.post("/terminal/verify", content_type="multipart/form-data", headers={"X-CSRF-Token": token},
                  data={"frames": [(io.BytesIO(b"x"), f"f{i}.jpg") for i in range(12)]}, environ_overrides={"REMOTE_ADDR": remote}).get_json()


def candidate(gid):
    return V.Decision(False, "guest_candidate", "x", method="Face", distance=0.21, liveness="passed", subject_type="guest", guest_id=gid)


def entry_rows(app):
    with app.app_context():
        return get_db().execute("SELECT * FROM entry_log ORDER BY entry_id").fetchall()


def test_an_approved_guest_is_let_in_once(app, users, monkeypatch):
    rid, gid = make_visit(app, status="Approved", name="Zara Guest")
    first = gate(app, monkeypatch, candidate(gid))
    assert first["granted"] and first["kind"] == "guest" and first["name"] == "Zara Guest" and first["valid_until"] and first["attendance"] is None
    second = gate(app, monkeypatch, candidate(gid))
    assert second["granted"] is False and second["code"] == "guest_used" and second["name"] is None and "already been used" in second["message"]
    rows = entry_rows(app)
    assert [(r["result"], r["failure_code"]) for r in rows] == [("Granted", None), ("Denied", "guest_used")]
    assert row_of(app, rid)["entries_used"] == 1
    assert count(app, "attendance") == 0                                                  # a guest never counts as student attendance


def test_the_log_row_links_the_guest_and_the_visit(app, users, monkeypatch):
    rid, gid = make_visit(app, status="Approved")
    gate(app, monkeypatch, candidate(gid))
    row = entry_rows(app)[0]
    assert (row["subject_type"], row["guest_id"], row["visit_request_id"], row["student_id"], row["method"], row["door_status"]) == \
        ("guest", gid, rid, None, "Face", "Unlocked")


@pytest.mark.parametrize("kwargs,code", [
    (dict(status="Pending"), "guest_pending"), (dict(status="Rejected"), "guest_rejected"),
    (dict(status="Approved", start=2, end=5), "guest_not_yet"), (dict(status="Approved", start=-6, end=-2), "guest_expired"),
])
def test_guests_without_a_valid_visit_are_denied(app, users, monkeypatch, kwargs, code):
    rid, gid = make_visit(app, **kwargs)
    body = gate(app, monkeypatch, candidate(gid))
    assert body["granted"] is False and body["code"] == code and body["kind"] == "guest" and body["name"] is None
    (row,) = entry_rows(app)
    assert (row["result"], row["door_status"], row["failure_code"]) == ("Denied", "Locked", code)
    assert row["student_id"] is None and row["guest_id"] == gid                           # recognised, so named; the rule said no
    assert row_of(app, rid)["entries_used"] == 0


def test_a_deactivated_host_blocks_the_guest_at_the_gate(app, users, monkeypatch):
    rid, gid = make_visit(app, status="Approved")
    with app.app_context():
        get_db().execute("UPDATE student SET is_active = 0 WHERE student_id = 1")
        get_db().commit()
    assert gate(app, monkeypatch, candidate(gid))["code"] == "guest_host_inactive" and row_of(app, rid)["entries_used"] == 0


def test_two_entries_allowed_means_two_and_no_more(app, users, monkeypatch):
    rid, gid = make_visit(app, status="Approved", max_entries=2)
    results = [gate(app, monkeypatch, candidate(gid))["granted"] for _ in range(3)]
    assert results == [True, True, False] and row_of(app, rid)["entries_used"] == 2


def test_a_second_approved_visit_allows_a_second_entry(app, users, monkeypatch):
    rid, gid = make_visit(app, status="Approved")
    gate(app, monkeypatch, candidate(gid))
    assert gate(app, monkeypatch, candidate(gid))["granted"] is False
    make_visit(app, status="Approved", guest_id=gid)
    assert gate(app, monkeypatch, candidate(gid))["granted"] is True


def test_the_terminal_never_shows_host_or_other_people(app, users, monkeypatch):
    _, gid = make_visit(app, status="Approved", name="Zara Guest")
    text = str(gate(app, monkeypatch, candidate(gid)))
    assert "Student 1" not in text and "REG-001" not in text and "0300" not in text


def test_a_student_still_enters_normally_and_gets_attendance(app, users, monkeypatch):
    d = V.Decision(True, "granted", "ok", method="Face", student_id=1, distance=0.2, liveness="passed")
    body = gate(app, monkeypatch, d)
    assert body["granted"] and body["kind"] == "student" and body["name"] == "Student 1" and body["attendance"]["first_entry"]


def test_liveness_failures_are_not_attributed_to_a_guest(app, users, monkeypatch):
    make_visit(app, status="Approved")
    gate(app, monkeypatch, V.deny("liveness_failed", liveness="failed"))
    (row,) = entry_rows(app)
    assert (row["subject_type"], row["guest_id"], row["student_id"]) == (None, None, None)


def test_only_guests_with_approved_visits_are_given_to_the_verifier(app, users, monkeypatch, fake_images):
    rid_ok, gid_ok = make_visit(app, status="Approved", name="Approved Guest")
    rid_wait, gid_wait = make_visit(app, status="Pending", name="Waiting Guest")
    with app.app_context():
        for gid, seed in ((gid_ok, 1), (gid_wait, 2)):
            biometrics.save_enrollment(get_db(), app.config["BIOMETRIC_KEY"], "guest", gid,
                                       [np.random.default_rng(seed).normal(0, 0.1, 128)] * 5, [np.ones(80, dtype=np.float32) / 80] * 5)
    seen = {}

    def spy(frames, faces, eyes, settings, **kw):
        seen["guests"] = set(kw.get("guest_faces", {}))
        return V.deny("not_recognized")
    monkeypatch.setattr(V, "verify", spy)
    c = app.test_client()
    c.get("/terminal/")
    with c.session_transaction() as s:
        token = s["_csrf"]
    c.post("/terminal/verify", content_type="multipart/form-data", headers={"X-CSRF-Token": token},
           data={"frames": [(io.BytesIO(b"x"), f"f{i}.jpg") for i in range(12)]})
    assert seen["guests"] == {gid_ok}


# ================= exits =================

def test_guest_exit_flow(app, users, monkeypatch):
    rid, gid = make_visit(app, status="Approved", name="Zara Guest")
    c = warden(app)
    assert "No guests inside" in c.get("/exits/").get_data(as_text=True)
    resp = c.post("/exits/record-guest", data={"guest_id": gid, "csrf_token": token_when_logged_in(c)})
    assert resp.status_code == 302 and count(app, "exit_record") == 0                    # nobody is inside yet
    gate(app, monkeypatch, candidate(gid))
    html = c.get("/exits/").get_data(as_text=True)
    assert "Zara Guest" in html and "guest of Student 1" in html
    c.post("/exits/record-guest", data={"guest_id": gid, "csrf_token": token_when_logged_in(c)})
    assert count(app, "exit_record", "subject_type = 'guest'") == 1
    page = c.get("/exits/").get_data(as_text=True)
    assert "(guest)" in page and "Wendy Warden" in page and "No guests inside" in page   # listed in the table, and no longer 'inside'


def test_guest_exit_cannot_be_recorded_twice_or_for_strangers(app, users, monkeypatch):
    rid, gid = make_visit(app, status="Approved")
    gate(app, monkeypatch, candidate(gid))
    c = warden(app)
    for _ in range(2):
        c.post("/exits/record-guest", data={"guest_id": gid, "csrf_token": token_when_logged_in(c)})
    assert count(app, "exit_record") == 1
    for bad in ("999", "abc", ""):
        c.post("/exits/record-guest", data={"guest_id": bad, "csrf_token": token_when_logged_in(c)})
    assert count(app, "exit_record") == 1


def test_students_cannot_record_guest_exits(app, users, monkeypatch):
    _, gid = make_visit(app, status="Approved")
    gate(app, monkeypatch, candidate(gid))
    c = student(app)
    assert c.post("/exits/record-guest", data={"guest_id": gid, "csrf_token": token_when_logged_in(c)}).status_code == 403
    assert count(app, "exit_record") == 0


def test_the_entry_log_page_shows_guest_entries(app, users, monkeypatch):
    _, gid = make_visit(app, status="Approved", name="Zara Guest")
    gate(app, monkeypatch, candidate(gid))
    html = admin_c(app).get("/logs/").get_data(as_text=True)
    assert "Zara Guest" in html and "(guest)" in html


# ================= dashboards and menus =================

def test_student_dashboard_shows_guest_requests(app, users):
    make_visit(app, 1, "Approved", name="Zara Guest")
    html = student(app).get("/student/").get_data(as_text=True)
    assert "Guest visits" in html and "Zara Guest" in html and "Request a visit" in html


def test_warden_dashboard_lists_waiting_requests_and_counts_only_open_ones(app, users):
    make_visit(app, 1, "Pending", name="Open Request")
    make_visit(app, 1, "Pending", start=-6, end=-2, name="Stale Request")
    html = warden(app).get("/warden/").get_data(as_text=True)
    assert "Open Request" in html and "Stale Request" not in html and 'href="/guests/requests/' in html
    assert 'stat-value">1<' in html.replace(" ", "").replace("\n", "") or ">1<" in html


def test_menus_link_to_the_right_pages(app, users):
    assert 'href="/guests/"' in student(app).get("/student/").get_data(as_text=True)
    assert 'href="/guests/requests"' in warden(app).get("/warden/").get_data(as_text=True)
    assert 'href="/guests/requests"' in admin_c(app).get("/admin/").get_data(as_text=True)


# ================= real pipeline: register a guest and let them in =================

def test_real_pipeline_guest_visit_end_to_end(app, users):
    rid, gid = make_visit(app, status="Approved", name="Zara Guest")
    w = warden(app)
    pictures = [jpeg(v) for v in face_variants()]
    resp = upload(w, rid, pictures)
    assert resp.status_code == 200, resp.get_data(as_text=True)

    open_img = face_rgb()
    blink = blink_burst(open_img, paint_eyes_closed(open_img), "OOOOOOOOCCOOOOOOOOOOOOOO")
    photo = blink_burst(open_img, open_img, "O" * 20)

    def attempt(frames):
        c = app.test_client()
        c.get("/terminal/")
        with c.session_transaction() as s:
            token = s["_csrf"]
        return c.post("/terminal/verify", content_type="multipart/form-data", headers={"X-CSRF-Token": token},
                      data={"frames": [(io.BytesIO(f), f"f{i}.jpg") for i, f in enumerate(frames)]}).get_json()

    attack = attempt(photo)
    assert attack["granted"] is False and attack["code"] == "liveness_failed" and row_of(app, rid)["entries_used"] == 0
    first = attempt(blink)
    assert first["granted"] and first["kind"] == "guest" and first["name"] == "Zara Guest", first
    again = attempt(blink)
    assert again["granted"] is False and again["code"] == "guest_used"
    assert row_of(app, rid)["entries_used"] == 1 and count(app, "attendance") == 0


def test_real_pipeline_a_student_face_cannot_be_registered_as_a_guest(app, users):
    admin = admin_c(app)
    pictures = [jpeg(v) for v in face_variants()]
    token = token_when_logged_in(admin)
    resp = admin.post("/enrollment/student/1/save", content_type="multipart/form-data", headers={"X-CSRF-Token": token},
                      data={"images": [(io.BytesIO(p), f"s{i}.jpg") for i, p in enumerate(pictures)], "consent": "1"})
    assert resp.status_code == 200
    rid, gid = make_visit(app, 2, status="Approved")
    resp = upload(warden(app), rid, pictures)
    assert resp.status_code == 409 and resp.get_json()["code"] == "is_student"
    assert guest_templates(app, gid)["enrolled"] is False
