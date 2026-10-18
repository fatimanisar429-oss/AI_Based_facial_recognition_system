"""A covered face (niqab: only the eyes show) is never searched against the gallery; the terminal asks for a registration number (1:1 check, see test_covered_1to1.py)."""
import numpy as np
import pytest

from app.services import alerts, entries
from app.services import verification as V
from imaging import face_rgb, jpeg

S = V.Settings()


def covered(rgb):
    """Dark cloth over the head and lower face, leaving a slit from the eyebrows to just under the eyes."""
    import face_recognition
    box = face_recognition.face_locations(rgb)[0]
    marks = face_recognition.face_landmarks(rgb, [box])[0]
    top, right, bottom, left = box
    width = right - left
    out = rgb.copy()
    out[max(top - int(.45 * width), 0):bottom + int(.8 * width), max(left - int(.35 * width), 0):right + int(.35 * width)] = (25, 25, 28)
    pts = np.array(marks["left_eye"] + marks["right_eye"] + marks["left_eyebrow"] + marks["right_eyebrow"])
    x0, y0 = pts.min(0)
    x1, _ = pts.max(0)
    eye_bottom = max(p[1] for p in marks["left_eye"] + marks["right_eye"])
    sy0, sy1, sx0, sx1 = int(y0 - .05 * width), int(eye_bottom + .07 * width), int(x0 - .1 * width), int(x1 + .1 * width)
    out[sy0:sy1, sx0:sx1] = rgb[sy0:sy1, sx0:sx1]
    return np.ascontiguousarray(out)


@pytest.fixture(scope="module")
def veiled():
    return covered(face_rgb())


def attempt(image, count=12):
    return V.verify([jpeg(image)] * count, {1: [np.zeros(128)]}, {}, S)


def test_dlib_alone_cannot_see_a_covered_face(veiled):
    import face_recognition
    assert face_recognition.face_locations(veiled) == []


def test_a_covered_face_is_asked_for_a_registration_number_and_never_identified(veiled):
    d = attempt(veiled)
    assert not d.granted and d.code == "face_covered"
    assert d.student_id is None and d.method == "None" and "registration number" in d.message
    assert d.checks["face"] == "failed"


def test_an_empty_scene_is_still_just_no_face():
    d = attempt(np.full((480, 640, 3), 120, np.uint8))
    assert d.code == "no_face" and not d.needs_review


def test_a_normal_face_is_not_called_covered():
    d = V.verify([jpeg(face_rgb())] * 12, {1: [np.zeros(128)]}, {}, S)
    assert d.code != "face_covered"                      # dlib finds the face, so the covered branch is never reached


def test_covered_attempts_do_not_raise_security_alerts():
    assert "face_covered" not in alerts.COUNTED_CODES


def test_the_log_label_says_the_terminal_asked_for_a_number():
    assert "registration number" in entries.FAILURE_LABELS["face_covered"]
