"""The whole attempt (frames -> liveness -> recognition) on real pictures. No mocks except where stated.

A 'blink' here is the real face with the eyes painted shut for a couple of frames (see imaging.paint_eyes_closed).
That exercises the detector but is NOT the same as a real person blinking: the final check of blink sensitivity has
to be done with a real webcam.
"""
import numpy as np
import pytest

from app.services import recognition as R
from app.services import verification as V
from imaging import blink_burst, face_rgb, face_variants, jpeg, paint_eyes_closed

S = V.Settings()


@pytest.fixture(scope="module")
def open_img():
    return face_rgb()


@pytest.fixture(scope="module")
def closed_img(open_img):
    return paint_eyes_closed(open_img)


@pytest.fixture(scope="module")
def gallery():
    """Student 1 enrolled with five real pictures of the face (the same pipeline the enrollment page uses)."""
    analyses = [R.analyze_image(jpeg(v)) for v in face_variants()]
    return {1: [a.encoding for a in analyses]}, {1: [a.eye_template for a in analyses]}


def run(frames, gallery, settings=S):
    faces, eyes = gallery
    return V.verify(frames, faces, eyes, settings)


# ---------- the good case ----------

def test_enrolled_person_who_blinks_is_granted(open_img, closed_img, gallery):
    d = run(blink_burst(open_img, closed_img), gallery)
    assert d.granted and d.code == "granted" and d.method == "Face" and d.student_id == 1
    assert d.liveness == "passed" and d.distance < 0.2 and not d.needs_review
    assert d.checks == {"face": "ok", "liveness": "ok", "recognition": "ok"}


def test_blink_anywhere_in_the_burst_works(open_img, closed_img, gallery):
    for pattern in ("OOOCCOOOOOOOOOOOOOOOOOOO", "OOOOOOOOOOOOOOOOOOOOCCOO", "OOOOOOOOOOOCOOOOOOOOOOOO"):
        assert run(blink_burst(open_img, closed_img, pattern), gallery).granted, pattern


def test_decision_is_fast_enough(open_img, closed_img, gallery):
    import time
    started = time.perf_counter()
    run(blink_burst(open_img, closed_img, "OOOOOOOOCCOOOOOOOOOOOOOO"), gallery)
    assert time.perf_counter() - started < 6          # SRS target is 3 s on good hardware; allow slack for slow CI


# ---------- liveness: a still picture must fail ----------

def test_still_photo_is_denied_for_liveness(open_img, closed_img, gallery):
    d = run(blink_burst(open_img, closed_img, "O" * 20), gallery)
    assert not d.granted and d.code == "liveness_failed" and d.liveness == "failed"
    assert d.student_id is None and d.distance is None                      # nobody is named, no score computed
    assert d.checks == {"face": "ok", "liveness": "failed", "recognition": "skipped"}


@pytest.mark.parametrize("pattern", ["CCOOOOOOOOOOOOOOOOOO", "OOOOOOOOOOOOOOOOOOCC", "C" * 20])
def test_eyes_closed_at_an_edge_or_all_the_time_is_denied(open_img, closed_img, gallery, pattern):
    assert run(blink_burst(open_img, closed_img, pattern), gallery).code == "liveness_failed"


def test_recognition_is_never_reached_when_liveness_fails(open_img, closed_img, gallery, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("recognition must not run without a blink")
    monkeypatch.setattr(V, "decide", forbidden)
    assert run(blink_burst(open_img, closed_img, "O" * 20), gallery).code == "liveness_failed"


# ---------- what the camera sees ----------

def test_too_few_frames(open_img, closed_img, gallery):
    assert run(blink_burst(open_img, closed_img, "OOCCOO"), gallery).code == "bad_frames"


def test_empty_scene_is_denied(gallery):
    frames = [jpeg(np.full((480, 640, 3), 128, np.uint8))] * 16
    d = run(frames, gallery)
    assert not d.granted and d.code == "no_face" and d.checks["face"] == "failed"


def test_two_people_in_view_are_denied(open_img, gallery):
    d = run([jpeg(np.hstack([open_img, open_img]))] * 16, gallery)
    assert not d.granted and d.code == "multiple_faces"


def test_a_few_broken_frames_do_not_ruin_the_attempt(open_img, closed_img, gallery):
    frames = blink_burst(open_img, closed_img, "OOOOOOOOCCOOOOOOOOOOOOOO")
    frames[0], frames[5] = b"garbage", b"\xff\xd8\xff broken"
    assert run(frames, gallery).granted


def test_only_broken_frames(gallery):
    assert run([b"garbage"] * 20, gallery).code == "bad_frames"


def test_dark_picture_is_denied_with_a_quality_message(open_img, closed_img, gallery):
    dark_open, dark_closed = (open_img * 0.18).astype(np.uint8), (closed_img * 0.18).astype(np.uint8)
    d = run(blink_burst(dark_open, dark_closed, "OOOOOOOOCCOOOOOOOOOOOOOO"), gallery)
    assert not d.granted and d.code in ("too_dark", "no_face")


def test_blurry_picture_is_denied(open_img, closed_img, gallery):
    import cv2
    blur = lambda im: cv2.GaussianBlur(im, (0, 0), 6)
    d = run(blink_burst(blur(open_img), blur(closed_img), "OOOOOOOOCCOOOOOOOOOOOOOO"), gallery)
    assert not d.granted


# ---------- recognition ----------

def test_unknown_face_is_denied_and_not_attributed(open_img, closed_img, gallery, monkeypatch):
    """The face passes liveness but its encoding is not that of any enrolled student."""
    import face_recognition
    stranger = np.random.default_rng(42).normal(0, 0.1, 128)
    monkeypatch.setattr(face_recognition, "face_encodings", lambda *a, **k: [stranger])
    d = run(blink_burst(open_img, closed_img), gallery)
    assert not d.granted and d.code == "not_recognized" and d.student_id is None and d.method == "None"
    assert d.liveness == "passed" and d.distance > S.face_threshold
    assert d.checks == {"face": "ok", "liveness": "ok", "recognition": "failed"}


def test_nobody_enrolled_means_nobody_enters(open_img, closed_img):
    d = V.verify(blink_burst(open_img, closed_img), {}, {}, S)
    assert not d.granted and d.code == "no_enrolled"


def test_a_stricter_threshold_can_refuse_the_enrolled_person(open_img, closed_img, gallery):
    d = run(blink_burst(open_img, closed_img), gallery, V.Settings(face_threshold=0.0001))
    assert not d.granted and d.code == "not_recognized"


def test_eye_fallback_switched_on_cannot_override_a_clear_failure(open_img, closed_img, gallery, monkeypatch):
    """Even with the fallback ON, a face that is nowhere near the enrolled one must be refused."""
    import face_recognition
    monkeypatch.setattr(face_recognition, "face_encodings", lambda *a, **k: [np.random.default_rng(7).normal(0, 0.1, 128)])
    d = run(blink_burst(open_img, closed_img), gallery, V.Settings(eye_fallback=True))
    assert not d.granted and d.method == "None"


def test_eye_fallback_cannot_bypass_liveness(open_img, closed_img, gallery):
    d = run(blink_burst(open_img, closed_img, "O" * 20), gallery, V.Settings(eye_fallback=True))
    assert not d.granted and d.code == "liveness_failed"


def test_eye_fallback_cannot_bypass_multiple_faces(open_img, gallery):
    d = run([jpeg(np.hstack([open_img, open_img]))] * 16, gallery, V.Settings(eye_fallback=True))
    assert not d.granted and d.code == "multiple_faces"
