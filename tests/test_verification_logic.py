"""Blink maths and the match decision, tested with plain numbers (no camera, no dlib)."""
import numpy as np
import pytest

from app import create_app
from app.services import verification as V

S = V.Settings()
OPEN = 0.30


def eye(width=30.0, height=9.0):
    """Six landmark points of an eye (dlib order) with the given width and opening."""
    h = height / 2
    return [(0, 0), (width * 0.3, -h), (width * 0.7, -h), (width, 0), (width * 0.7, h), (width * 0.3, h)]


def person(seed, scale=0.1):
    return np.random.default_rng(seed).normal(0, scale, 128)


# ================= eye aspect ratio =================

def test_ear_of_open_and_closed_eye():
    assert V.eye_aspect_ratio(eye(30, 9)) == pytest.approx(0.30, abs=0.01)
    assert V.eye_aspect_ratio(eye(30, 2)) < 0.08


def test_ear_does_not_depend_on_picture_size():
    assert V.eye_aspect_ratio(eye(30, 9)) == pytest.approx(V.eye_aspect_ratio(eye(90, 27)))


# ================= blink detection =================

def blinks(series, **kw):
    return V.blink_detected(series, V.Settings(**kw))


def test_normal_blink_is_detected():
    ok, reason = blinks([OPEN] * 6 + [0.12, 0.10] + [OPEN] * 8)
    assert ok and reason == "blink"


def test_a_one_frame_blink_counts():
    assert blinks([OPEN] * 7 + [0.11] + [OPEN] * 7)[0]


def test_two_blinks_count():
    assert blinks([OPEN] * 4 + [0.1] + [OPEN] * 4 + [0.12, 0.1] + [OPEN] * 4)[0]


def test_still_photo_has_no_blink():
    ok, reason = blinks([OPEN] * 20)
    assert not ok and reason == "no_blink"


def test_small_landmark_jitter_is_not_a_blink():
    rng = np.random.default_rng(3)
    jitter = list(OPEN * (1 + rng.normal(0, 0.03, 30)))             # +-3% wobble, like a shaky photo
    assert not blinks(jitter)[0]


def test_a_drop_that_is_not_closed_enough_is_not_a_blink():
    assert not blinks([0.30] * 7 + [0.25, 0.26] + [0.30] * 7)[0]   # 17% drop: squinting at most


def test_absolute_limit_also_applies():
    wide = 0.45                                                     # very wide eyes: a 25% dip is still open
    assert not blinks([wide] * 8 + [0.34] + [wide] * 8)[0]
    assert blinks([wide] * 8 + [0.15] + [wide] * 8)[0]


def test_eyes_closed_at_the_start_or_end_are_not_a_blink():
    assert not blinks([0.10, 0.10] + [OPEN] * 14)[0]                # nothing open before it
    assert not blinks([OPEN] * 14 + [0.10, 0.10])[0]                # nothing open after it


def test_eyes_shut_for_too_long_is_not_a_blink():
    assert not blinks([OPEN] * 4 + [0.10] * 12 + [OPEN] * 4)[0]


def test_eyes_that_are_never_open_fail():
    ok, reason = blinks([0.10] * 20)
    assert not ok and reason == "eyes_not_open"


def test_too_few_measurements():
    assert blinks([OPEN] * 4 + [0.1] + [OPEN] * 3) == (False, "too_few_measurements")
    assert blinks([None] * 20 + [OPEN] * 5)[1] == "too_few_measurements"


def test_missing_frames_do_not_break_the_check():
    series = [OPEN, None, OPEN, OPEN, None, 0.1, None, OPEN, OPEN, OPEN, OPEN, None, OPEN, OPEN]
    assert blinks(series)[0]


def test_tall_and_narrow_eyes_work_through_the_relative_rule():
    narrow = 0.22                                                   # naturally narrow eyes
    assert blinks([narrow] * 8 + [0.12] + [narrow] * 8)[0]


# ================= the match decision =================

def gallery(**people):
    return {i: [v + n * 0.001 for n in range(3)] for i, v in people.items()}


def test_clear_match_is_granted_with_face_method():
    g = gallery(**{"1": person(1)})
    g = {1: g["1"]}
    d = V.decide(person(1) + 0.002, None, g, {}, S)
    assert d.granted and d.method == "Face" and d.student_id == 1 and d.code == "granted" and not d.needs_review
    assert d.distance < 0.1 and d.checks == {"face": "ok", "liveness": "ok", "recognition": "ok"}


def test_unknown_face_is_denied_and_not_attributed():
    g = {1: [person(1)], 2: [person(2)]}
    d = V.decide(person(99), None, g, {}, S)
    assert not d.granted and d.code == "not_recognized" and d.student_id is None and d.method == "None"
    assert d.distance > S.face_threshold


def test_the_closest_of_several_students_wins():
    g = {1: [person(1)], 2: [person(2)], 3: [person(3)]}
    assert V.decide(person(2) + 0.003, None, g, {}, S).student_id == 2


def test_nobody_enrolled():
    d = V.decide(person(1), None, {}, {}, S)
    assert not d.granted and d.code == "no_enrolled"


def test_threshold_boundary():
    base = person(5)
    step = np.zeros(128)
    step[0] = 1.0
    just_in = base + step * (S.face_threshold - 0.01)
    just_out = base + step * (S.face_threshold + 0.01)
    assert V.decide(just_in, None, {1: [base]}, {}, S).granted
    assert not V.decide(just_out, None, {1: [base]}, {}, S).granted


def test_threshold_is_configurable():
    base = person(5)
    probe = base.copy()
    probe[0] += 0.3
    assert V.decide(probe, None, {1: [base]}, {}, V.Settings(face_threshold=0.5)).granted
    assert not V.decide(probe, None, {1: [base]}, {}, V.Settings(face_threshold=0.2)).granted


def test_two_students_that_look_alike_are_not_guessed():
    base = person(7)
    twin = base.copy()
    twin[0] += 0.02                                                 # nearly identical encodings
    d = V.decide(base + 0.001, None, {1: [base], 2: [twin]}, {}, S)
    assert not d.granted and d.code == "ambiguous" and d.student_id is None


def test_a_distant_second_student_does_not_cause_ambiguity():
    d = V.decide(person(1) + 0.001, None, {1: [person(1)], 2: [person(2)]}, {}, S)
    assert d.granted and d.student_id == 1


def test_decision_never_carries_a_student_when_denied():
    for probe in (person(50), person(51)):
        d = V.decide(probe, None, {1: [person(1)]}, {}, S)
        assert not d.granted and d.student_id is None


# ================= eye-region fallback rules =================

def hist(seed):
    h = np.random.default_rng(seed).random(80)
    return (h / h.sum()).astype(np.float32)


def borderline_probe(distance=0.55):
    base = person(5)
    probe = base.copy()
    probe[0] += distance
    return base, probe


FALLBACK_ON = V.Settings(eye_fallback=True)


def test_fallback_is_off_by_default():
    base, probe = borderline_probe()
    d = V.decide(probe, hist(1), {1: [base]}, {1: [hist(1)]}, S)
    assert not d.granted and d.code == "not_recognized"


def test_fallback_can_confirm_a_borderline_match_and_flags_it_for_review():
    base, probe = borderline_probe(0.55)                            # between 0.5 (match) and 0.6 (fallback limit)
    d = V.decide(probe, hist(1), {1: [base]}, {1: [hist(1)]}, FALLBACK_ON)
    assert d.granted and d.method == "Eye-Region" and d.needs_review and d.student_id == 1


def test_fallback_cannot_identify_someone_whose_face_is_far_off():
    base, probe = borderline_probe(0.9)                             # face clearly not this student
    d = V.decide(probe, hist(1), {1: [base]}, {1: [hist(1)]}, FALLBACK_ON)
    assert not d.granted and d.code == "not_recognized"


def test_fallback_needs_a_close_eye_template():
    base, probe = borderline_probe(0.55)
    d = V.decide(probe, hist(2), {1: [base]}, {1: [hist(1)]}, FALLBACK_ON)
    assert not d.granted


def test_fallback_refuses_when_the_eyes_point_to_a_different_student():
    base, probe = borderline_probe(0.55)
    faces = {1: [base], 2: [person(8)]}
    eyes = {1: [hist(1)], 2: [hist(2)]}
    d = V.decide(probe, hist(2), faces, eyes, FALLBACK_ON)           # eyes match student 2, face is nearest to 1
    assert not d.granted


def test_fallback_refuses_when_two_students_have_similar_eyes():
    base, probe = borderline_probe(0.55)
    faces = {1: [base], 2: [person(8)]}
    eyes = {1: [hist(1)], 2: [hist(1) * 0.999 + hist(3) * 0.001]}
    assert not V.decide(probe, hist(1), faces, eyes, FALLBACK_ON).granted


def test_fallback_without_stored_eye_templates_is_denied():
    base, probe = borderline_probe(0.55)
    assert not V.decide(probe, hist(1), {1: [base]}, {}, FALLBACK_ON).granted
    assert not V.decide(probe, None, {1: [base]}, {1: [hist(1)]}, FALLBACK_ON).granted


def test_a_clear_face_match_never_uses_the_fallback():
    d = V.decide(person(1) + 0.001, hist(9), {1: [person(1)]}, {1: [hist(1)]}, FALLBACK_ON)
    assert d.granted and d.method == "Face" and not d.needs_review


# ================= settings =================

def test_settings_come_from_config(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / "s.db"),
                      "FACE_THRESHOLD": 0.42, "EYE_FALLBACK_ENABLED": True})
    s = V.Settings.from_config(app.config)
    assert s.face_threshold == 0.42 and s.eye_fallback is True and s.min_frames == 10


def test_default_configuration_is_the_safe_one(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "k", "DATABASE_PATH": str(tmp_path / "s.db")})
    assert app.config["EYE_FALLBACK_ENABLED"] is False
    assert app.config["TERMINAL_ADDRESSES"] == ["127.0.0.1", "::1"]
    assert app.config["FACE_THRESHOLD"] == 0.55


def test_deny_helper_uses_the_standard_messages():
    d = V.deny("liveness_failed")
    assert not d.granted and "blink" in d.message and d.student_id is None and d.method == "None"
    assert V.deny("something_new", "Custom text").message == "Custom text"
