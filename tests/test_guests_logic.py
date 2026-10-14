"""Guest visits: request validation, status, warden decisions, visit rules, entry consumption, matching, logging."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.db import get_db
from app.services import entries, guests
from app.services import verification as V
from app.timeutil import to_text, utc_now

S = V.Settings()


def stamp(hours=0.0):
    return to_text(utc_now() + timedelta(hours=hours))


def local_today(app):
    from app.timeutil import hostel_today
    return hostel_today(app.config["TIMEZONE"], app.config["ATTENDANCE_DAY_START"])


def make_visit(app, student_id=1, status="Pending", start=-1, end=3, max_entries=1, used=0, name="Visitor", guest_id=None):
    """A guest visit whose window runs from `start` to `end` hours from now."""
    with app.app_context():
        db = get_db()
        if guest_id is None:
            guest_id = db.execute("INSERT INTO guest_profile (full_name, contact, host_student_id, created_at) VALUES (?, '0300 1234567', ?, ?)",
                                  (name, student_id, stamp())).lastrowid
        rid = db.execute(
            "INSERT INTO guest_visit_request (student_id, guest_id, visit_date, valid_from, valid_to, purpose, status, max_entries, entries_used, created_at, "
            "decided_by_warden_id, decided_at) VALUES (?, ?, '2026-10-14', ?, ?, 'family visit', ?, ?, ?, ?, ?, ?)",
            (student_id, guest_id, stamp(start), stamp(end), status, max_entries, used, stamp(-5),
             1 if status in ("Approved", "Rejected") else None, stamp(-4) if status in ("Approved", "Rejected") else None)).lastrowid
        db.commit()
        return rid, guest_id


def row_of(app, rid):
    with app.app_context():
        return guests.get_request(get_db(), rid)


# ================= request validation =================

FORM = {"guest_name": "Zara Guest", "guest_contact": "0300 1234567", "purpose": "Family visit", "from_time": "15:00", "to_time": "18:00"}


def validate(app, form=None, **kw):
    f = {**FORM, **(form or {})}
    f.setdefault("visit_date", local_today(app))
    # use a date that is certainly in the future so the window cannot have ended
    from datetime import date
    f["visit_date"] = (form or {}).get("visit_date", (date.fromisoformat(local_today(app)) + timedelta(days=1)).isoformat())
    with app.app_context():
        return guests.validate_request(get_db(), f, kw.get("student_id", 1), local_today(app), stamp(), app.config)


def test_a_valid_request(app, users):
    clean, errors = validate(app)
    assert errors == {} and clean["guest_name"] == "Zara Guest" and clean["valid_from"] < clean["valid_to"]
    assert clean["valid_from"].endswith("10:00:00")           # 15:00 Karachi is 10:00 UTC


@pytest.mark.parametrize("field,value,key", [
    ("guest_name", "", "guest_name"), ("guest_name", "A", "guest_name"), ("guest_name", "12345", "guest_name"),
    ("guest_contact", "", "guest_contact"), ("guest_contact", "abc", "guest_contact"), ("guest_contact", "123", "guest_contact"),
    ("purpose", "", "purpose"), ("purpose", "hi", "purpose"), ("purpose", "x" * 201, "purpose"),
    ("visit_date", "", "visit_date"), ("visit_date", "not-a-date", "visit_date"), ("visit_date", "2020-01-01", "visit_date"),
    ("visit_date", "2099-01-01", "visit_date"),
    ("from_time", "", "from_time"), ("from_time", "25:00", "from_time"), ("to_time", "9pm", "to_time"),
])
def test_invalid_fields_are_reported(app, users, field, value, key):
    _, errors = validate(app, {field: value})
    assert key in errors


@pytest.mark.parametrize("contact", ["0300 1234567", "+92 300 1234567", "0300-1234567", "zara@example.com"])
def test_phone_or_email_contacts_are_accepted(app, users, contact):
    assert validate(app, {"guest_contact": contact})[1] == {}


def test_end_must_be_after_start(app, users):
    assert "after the start" in validate(app, {"from_time": "18:00", "to_time": "15:00"})[1]["to_time"]
    assert "to_time" in validate(app, {"from_time": "18:00", "to_time": "18:00"})[1]


def test_a_visit_cannot_last_longer_than_the_limit(app, users):
    assert "at most 12 hours" in validate(app, {"from_time": "06:00", "to_time": "19:00"})[1]["to_time"]
    assert validate(app, {"from_time": "06:00", "to_time": "18:00"})[1] == {}


def test_a_window_that_already_ended_today_is_refused(app, users):
    today = local_today(app)
    _, errors = validate(app, {"visit_date": today, "from_time": "00:05", "to_time": "00:10"})
    assert "already ended" in errors.get("to_time", "") or "past" in errors.get("visit_date", "")


def test_returning_guest_uses_the_saved_details(app, users):
    _, gid = make_visit(app, 1, name="Old Friend")
    clean, errors = validate(app, {"previous_guest_id": str(gid), "guest_name": "IGNORED", "guest_contact": "ignored"})
    assert errors == {} and clean["guest_name"] == "Old Friend" and clean["previous_guest_id"] == gid


def test_cannot_reuse_another_students_guest(app, users):
    _, gid = make_visit(app, 2, name="Student Two's Guest")
    assert "previous_guest_id" in validate(app, {"previous_guest_id": str(gid)}, student_id=1)[1]
    assert "previous_guest_id" in validate(app, {"previous_guest_id": "abc"})[1]


def test_too_many_waiting_requests(app, users):
    for _ in range(5):
        make_visit(app, 1, "Pending")
    assert "already have 5 requests" in validate(app)[1]["purpose"]


def test_other_students_pending_requests_do_not_count(app, users):
    for _ in range(5):
        make_visit(app, 2, "Pending")
    assert validate(app, student_id=1)[1] == {}


def test_create_request_stores_a_pending_visit_for_the_host(app, users):
    clean, _ = validate(app)
    with app.app_context():
        rid, gid = guests.create_request(get_db(), 2, clean, stamp())
        row = guests.get_request(get_db(), rid)
    assert row["status"] == "Pending" and row["student_id"] == 2 and row["max_entries"] == 1 and row["entries_used"] == 0
    assert row["guest_name"] == "Zara Guest" and row["decided_by"] is None


# ================= displayed status =================

@pytest.mark.parametrize("kwargs,expected", [
    (dict(status="Pending", start=1, end=3), "Pending"),
    (dict(status="Pending", start=-5, end=-3), "Expired"),
    (dict(status="Approved", start=-1, end=3), "Approved"),
    (dict(status="Approved", start=-5, end=-3), "Expired"),
    (dict(status="Approved", start=-1, end=3, used=1), "Used"),
    (dict(status="Approved", start=-5, end=-3, used=1), "Used"),
    (dict(status="Rejected"), "Rejected"),
])
def test_display_status(app, users, kwargs, expected):
    rid, _ = make_visit(app, **kwargs)
    assert guests.display_status(row_of(app, rid), stamp()) == expected


# ================= warden decisions =================

def decide(app, rid, approve=True, entries_allowed=1):
    with app.app_context():
        return guests.decide(get_db(), rid, 1, approve, entries_allowed, stamp(), app.config["GUEST_MAX_ENTRIES"])


def test_approve_records_who_when_and_the_number_of_entries(app, users):
    rid, _ = make_visit(app)
    assert decide(app, rid, True, 2) == "approved"
    row = row_of(app, rid)
    assert row["status"] == "Approved" and row["max_entries"] == 2 and row["decided_by"] == "Wendy Warden" and row["decided_at"]


def test_reject(app, users):
    rid, _ = make_visit(app)
    assert decide(app, rid, False, 3) == "rejected"
    row = row_of(app, rid)
    assert row["status"] == "Rejected" and row["max_entries"] == 1 and row["decided_by"] == "Wendy Warden"


def test_a_request_can_only_be_decided_once(app, users):
    rid, _ = make_visit(app)
    assert decide(app, rid, True) == "approved"
    assert decide(app, rid, False) == "not_pending" and decide(app, rid, True, 2) == "not_pending"
    assert row_of(app, rid)["status"] == "Approved" and row_of(app, rid)["max_entries"] == 1


@pytest.mark.parametrize("n", [0, -1, 4, 99])
def test_the_number_of_entries_is_limited(app, users, n):
    rid, _ = make_visit(app)
    assert decide(app, rid, True, n) == "bad_entries" and row_of(app, rid)["status"] == "Pending"


def test_an_expired_request_cannot_be_approved(app, users):
    rid, _ = make_visit(app, start=-6, end=-2)
    assert decide(app, rid, True) == "expired" and row_of(app, rid)["status"] == "Pending"


def test_an_expired_request_can_still_be_rejected(app, users):
    rid, _ = make_visit(app, start=-6, end=-2)
    assert decide(app, rid, False) == "rejected"


def test_missing_request(app, users):
    assert decide(app, 999) == "missing"


# ================= may this guest enter now? =================

def check(app, gid):
    with app.app_context():
        row, code = guests.check_visit(get_db(), gid, stamp())
        return code


def test_an_approved_visit_in_its_window_is_usable(app, users):
    _, gid = make_visit(app, status="Approved")
    assert check(app, gid) is None


@pytest.mark.parametrize("kwargs,code", [
    (dict(status="Pending"), "guest_pending"),
    (dict(status="Rejected"), "guest_rejected"),
    (dict(status="Approved", start=2, end=5), "guest_not_yet"),
    (dict(status="Approved", start=-6, end=-2), "guest_expired"),
    (dict(status="Approved", used=1), "guest_used"),
    (dict(status="Pending", start=-6, end=-2), "guest_no_visit"),
])
def test_refusal_reasons(app, users, kwargs, code):
    _, gid = make_visit(app, **kwargs)
    assert check(app, gid) == code


def test_a_guest_without_any_request_is_refused(app, users):
    with app.app_context():
        gid = get_db().execute("INSERT INTO guest_profile (full_name, contact, host_student_id, created_at) VALUES ('X','1',1,?)", (stamp(),)).lastrowid
        get_db().commit()
    assert check(app, gid) == "guest_no_visit"


def test_a_deactivated_host_blocks_the_guest(app, users):
    _, gid = make_visit(app, status="Approved")
    with app.app_context():
        get_db().execute("UPDATE student SET is_active = 0 WHERE student_id = 1")
        get_db().commit()
    assert check(app, gid) == "guest_host_inactive"


def test_a_second_visit_is_picked_when_the_first_is_used_up(app, users):
    _, gid = make_visit(app, status="Approved", used=1)
    make_visit(app, status="Approved", guest_id=gid)
    assert check(app, gid) is None


def test_an_old_expired_visit_does_not_hide_a_valid_one(app, users):
    _, gid = make_visit(app, status="Approved", start=-30, end=-26)
    make_visit(app, status="Approved", guest_id=gid)
    assert check(app, gid) is None


# ================= spending an entry =================

def consume(app, rid, at=None):
    with app.app_context():
        ok = guests.consume_entry(get_db(), rid, at or stamp())
        get_db().commit()
        return ok


def test_one_time_entry_can_be_spent_once(app, users):
    rid, _ = make_visit(app, status="Approved")
    assert consume(app, rid) is True and consume(app, rid) is False
    assert row_of(app, rid)["entries_used"] == 1


def test_two_entries_allowed_means_exactly_two(app, users):
    rid, _ = make_visit(app, status="Approved", max_entries=2)
    assert [consume(app, rid) for _ in range(3)] == [True, True, False]


@pytest.mark.parametrize("kwargs", [dict(status="Pending"), dict(status="Rejected"), dict(status="Approved", start=2, end=5),
                                    dict(status="Approved", start=-6, end=-2)])
def test_entries_cannot_be_spent_on_invalid_visits(app, users, kwargs):
    rid, _ = make_visit(app, **kwargs)
    assert consume(app, rid) is False and row_of(app, rid)["entries_used"] == 0


def test_window_edges_are_inclusive(app, users):
    rid, _ = make_visit(app, status="Approved", start=-1, end=1)
    row = row_of(app, rid)
    assert consume(app, rid, row["valid_to"]) is True


# ================= turning a recognised face into a decision =================

def candidate(gid, distance=0.2):
    return V.Decision(False, "guest_candidate", "x", method="Face", distance=distance, liveness="passed", subject_type="guest", guest_id=gid)


def resolve(app, decision):
    with app.app_context():
        return guests.resolve(get_db(), decision, stamp())


def test_resolve_grants_a_valid_visit(app, users):
    rid, gid = make_visit(app, status="Approved")
    d = resolve(app, candidate(gid))
    assert d.granted and d.subject_type == "guest" and d.guest_id == gid and d.visit_request_id == rid and d.student_id is None


def test_resolve_refuses_and_keeps_the_guest_attached(app, users):
    rid, gid = make_visit(app, status="Approved", used=1)
    d = resolve(app, candidate(gid))
    assert not d.granted and d.code == "guest_used" and d.guest_id == gid and d.visit_request_id == rid and "already been used" in d.message


def test_resolve_leaves_other_decisions_alone(app, users):
    for d in (V.deny("liveness_failed"), V.deny("not_recognized"),
              V.Decision(True, "granted", "ok", student_id=1, method="Face")):
        assert resolve(app, d) is d


# ================= matching a face against students and guests =================

def vec(seed, scale=0.1):
    return np.random.default_rng(seed).normal(0, scale, 128)


def student_decision(encoding, gallery):
    return V.decide(encoding, None, gallery, {}, S)


def test_a_face_that_matches_only_a_guest_is_a_candidate_not_a_grant(app):
    students = {1: [vec(1)]}
    base = student_decision(vec(7), students)
    d = V.consider_guests(base, vec(7) + 0.002, {5: [vec(7)]}, S)
    assert d.code == "guest_candidate" and not d.granted and d.guest_id == 5 and d.subject_type == "guest" and d.student_id is None


def test_a_face_matching_both_a_student_and_a_guest_is_ambiguous(app):
    face = vec(3)
    base = student_decision(face + 0.001, {1: [face]})
    assert base.granted
    d = V.consider_guests(base, face + 0.001, {9: [face + 0.003]}, S)
    assert not d.granted and d.code == "ambiguous" and d.student_id is None and d.guest_id is None


def test_two_similar_guests_are_ambiguous(app):
    face = vec(4)
    base = student_decision(face, {1: [vec(1)]})
    twin = face.copy()
    twin[0] += 0.02                                              # distance 0.02 from the first guest: closer than FACE_MARGIN (0.04)
    d = V.consider_guests(base, face, {5: [face], 6: [twin]}, S)
    assert d.code == "ambiguous" and d.guest_id is None
    far = face.copy()
    far[0] += 0.3                                                # 0.3 apart: clearly different, so the closer guest wins
    assert V.consider_guests(base, face, {5: [face], 6: [far]}, S).guest_id == 5


def test_a_far_guest_changes_nothing(app):
    base = student_decision(vec(1) + 0.001, {1: [vec(1)]})
    assert V.consider_guests(base, vec(1) + 0.001, {5: [vec(77)]}, S) is base
    unknown = student_decision(vec(50), {1: [vec(1)]})
    assert V.consider_guests(unknown, vec(50), {5: [vec(77)]}, S) is unknown and unknown.code == "not_recognized"


def test_no_guests_enrolled_changes_nothing(app):
    base = student_decision(vec(50), {1: [vec(1)]})
    assert V.consider_guests(base, vec(50), {}, S) is base


def test_the_closest_guest_is_chosen(app):
    face = vec(8)
    base = student_decision(face, {1: [vec(1)]})
    d = V.consider_guests(base, face + 0.002, {5: [vec(60)], 6: [face], 7: [vec(61)]}, S)
    assert d.guest_id == 6


def test_a_guest_is_matched_even_when_no_student_is_enrolled(app):
    face = vec(8)
    base = student_decision(face, {})
    assert base.code == "no_enrolled"
    assert V.consider_guests(base, face, {6: [face]}, S).code == "guest_candidate"


def test_the_eye_fallback_is_never_used_for_guests(app):
    """A borderline face (between the match and fallback limits) must not become a guest through the eye template."""
    face = vec(8)
    probe = face.copy()
    probe[0] += 0.55
    base = student_decision(probe, {})
    assert V.consider_guests(base, probe, {6: [face]}, V.Settings(eye_fallback=True)) is base


# ================= the entry log for guests =================

def record(app, decision):
    with app.app_context():
        db = get_db()
        entry_id, info = entries.record_attempt(db, decision, app.config)
        return db.execute("SELECT * FROM entry_log WHERE entry_id = ?", (entry_id,)).fetchone(), info, decision


def granted_guest(rid, gid, distance=0.21):
    return V.Decision(True, "granted", "Guest access granted.", method="Face", distance=distance, liveness="passed",
                      subject_type="guest", guest_id=gid, visit_request_id=rid)


def test_granted_guest_entry_is_logged_and_spends_the_entry(app, users):
    rid, gid = make_visit(app, status="Approved")
    row, info, _ = record(app, granted_guest(rid, gid))
    assert (row["subject_type"], row["guest_id"], row["visit_request_id"], row["student_id"]) == ("guest", gid, rid, None)
    assert (row["result"], row["method"], row["door_status"], row["failure_code"]) == ("Granted", "Face", "Unlocked", None)
    assert info is None and row_of(app, rid)["entries_used"] == 1


def test_guest_entries_never_create_student_attendance(app, users):
    rid, gid = make_visit(app, status="Approved", student_id=1)
    record(app, granted_guest(rid, gid))
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == 0


def test_a_lost_race_becomes_a_refusal_not_a_grant(app, users):
    """The entry was spent between the check and the log write: nothing may be granted."""
    rid, gid = make_visit(app, status="Approved", used=1)
    row, _, decision = record(app, granted_guest(rid, gid))
    assert row["result"] == "Denied" and row["door_status"] == "Locked" and row["failure_code"] == "guest_used"
    assert not decision.granted and decision.code == "guest_used"                       # the caller sees what was really recorded
    assert row_of(app, rid)["entries_used"] == 1                                         # still 1, not 2


def test_refusal_for_a_recognised_guest_names_the_guest(app, users):
    rid, gid = make_visit(app, status="Approved", used=1)
    d = V.Decision(False, "guest_used", "x", method="Face", distance=0.2, liveness="passed", subject_type="guest", guest_id=gid, visit_request_id=rid)
    row, _, _ = record(app, d)
    assert (row["result"], row["subject_type"], row["guest_id"], row["student_id"], row["failure_code"]) == ("Denied", "guest", gid, None, "guest_used")
    assert row["failure_reason"] == entries.FAILURE_LABELS["guest_used"] and row_of(app, rid)["entries_used"] == 1


@pytest.mark.parametrize("code,liveness", [("liveness_failed", "failed"), ("not_recognized", "passed"), ("ambiguous", "passed")])
def test_unrecognised_attempts_are_never_attributed(app, users, code, liveness):
    row, _, _ = record(app, V.deny(code, liveness=liveness, distance=0.6))
    assert (row["subject_type"], row["student_id"], row["guest_id"], row["visit_request_id"]) == (None, None, None, None)


def test_a_guest_decision_cannot_grant_without_a_visit_request(app, users):
    _, gid = make_visit(app, status="Approved")
    d = V.Decision(True, "granted", "x", method="Face", distance=0.2, liveness="passed", subject_type="guest", guest_id=gid, visit_request_id=None)
    row, _, decision = record(app, d)
    assert row["result"] == "Denied" and not decision.granted


def test_students_are_logged_as_before(app, users):
    d = V.Decision(True, "granted", "ok", method="Face", student_id=2, distance=0.2, liveness="passed")
    row, info, _ = record(app, d)
    assert (row["subject_type"], row["student_id"], row["guest_id"]) == ("student", 2, None) and info["first_entry"]
