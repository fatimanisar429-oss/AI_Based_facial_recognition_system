"""Covered face (niqab): enrollment of the eye-slit picture and the 1:1 check at the gate. No Flask code here.

Why 1:1. Matching only the eye slit against the WHOLE gallery let far too many strangers through (docs/recognition-limits.md,
section 3b). So the gate asks the student for their registration number first and compares the live eyes with that ONE
student's covered-face templates. A stranger now needs the number AND a similar-looking eye area, and the attempt is
flagged for the warden either way.

How a covered face is handled. dlib's detector cannot find a covered face, so YuNet (OpenCV) finds the head and the two eye
positions. The face is aligned by those eye positions and dlib's recognition network turns it into a 128-number encoding
(covered_encoding). Enrollment and the gate use the same steps, so a covered sample is compared with a covered sample.

Blink check. dlib's eye landmarks are not reliable on a covered face (in our simulated niqab a painted-shut eye still read as
open), so the blink is found differently: the eye band is cut out of every frame and compared with the "typical" (median)
band of the burst. Eyes are open most of the time, so the median band is the open-eyed one; a blink shows as a short run of
frames that differ clearly from it, with unchanged frames before and after. A still photograph never differs. A moving head
differs for a long time and does not return, so it is not mistaken for a blink.

Honest limits: eye-slit matching is weak, and both COVERED_THRESHOLD and COVERED_BLINK_MIN_CHANGE were set from simulated
pictures only. Test them with real students on the real camera before relying on them.
"""
import cv2
import face_recognition
import numpy as np
from face_recognition import api as face_recognition_api

from . import recognition as R
from . import verification as V


# ---------- finding the covered face ----------

def detect_covered(rgb, side=640):
    """[(box, eyes)] for every face YuNet finds. box = (top, right, bottom, left) in `rgb` coordinates, eyes = ((x, y), (x, y))."""
    small, up = V._shrink(rgb, side)
    height, width = small.shape[:2]
    detector = V._yunet_detector()
    detector.setInputSize((width, height))
    _, found = detector.detect(cv2.cvtColor(small, cv2.COLOR_RGB2BGR))
    out = []
    h, w = rgb.shape[:2]
    for row in (found if found is not None else []):
        x, y, bw, bh = (float(v) * up for v in row[:4])
        box = (max(int(y), 0), min(int(x + bw), w), min(int(y + bh), h), max(int(x), 0))
        eyes = ((float(row[4]) * up, float(row[5]) * up), (float(row[6]) * up, float(row[7]) * up))
        out.append((box, eyes))
    return out


CHIP_SIZE, CHIP_EYE_GAP, CHIP_EYE_Y = 150, 56, 62


def covered_encoding(rgb, eyes):
    """128-number description of a covered face, made from the eye positions alone.

    The usual route (let dlib find landmarks inside a face box and align by them) was unstable here: the landmarks of a covered
    face are guesses, and re-saving the very same picture at another JPEG quality moved the encoding by 0.42. So the face is
    aligned by hand instead: rotate and scale it so the two eyes sit at fixed places in a 150 x 150 picture, then give that
    picture straight to dlib's recognition network. The same picture at another JPEG quality now moves it by about 0.06."""
    (x0, y0), (x1, y1) = sorted(eyes)
    gap = max(float(np.hypot(x1 - x0, y1 - y0)), 1.0)
    angle = float(np.degrees(np.arctan2(y1 - y0, x1 - x0)))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    matrix = cv2.getRotationMatrix2D((cx, cy), angle, CHIP_EYE_GAP / gap)
    matrix[0, 2] += CHIP_SIZE / 2 - cx
    matrix[1, 2] += CHIP_EYE_Y - cy
    chip = cv2.warpAffine(rgb, matrix, (CHIP_SIZE, CHIP_SIZE), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return np.asarray(face_recognition_api.face_encoder.compute_face_descriptor(np.ascontiguousarray(chip)), dtype=np.float64)


def eye_strip(rgb, eyes):
    """Grey crop of the band that holds both eyes (used to judge light and sharpness: the cloth around it is dark by design)."""
    (x0, y0), (x1, y1) = eyes
    gap = max(abs(x1 - x0), 1.0)
    h, w = rgb.shape[:2]
    left, right = int(max(min(x0, x1) - 0.6 * gap, 0)), int(min(max(x0, x1) + 0.6 * gap, w))
    top, bottom = int(max(min(y0, y1) - 0.45 * gap, 0)), int(min(max(y0, y1) + 0.35 * gap, h))
    if right - left < 20 or bottom - top < 8:
        raise R.ImageError("no_eyes", "The eyes could not be located. Move closer to the camera.")
    return cv2.cvtColor(rgb[top:bottom, left:right], cv2.COLOR_RGB2GRAY)


def strip_quality(rgb, eyes):
    gray = eye_strip(rgb, eyes)
    fixed = cv2.resize(gray, (200, 60), interpolation=cv2.INTER_AREA if gray.shape[1] > 200 else cv2.INTER_CUBIC)
    return float(gray.mean()), float(cv2.Laplacian(fixed, cv2.CV_64F).var())


def _check_strip(rgb, eyes, limits):
    brightness, sharpness = strip_quality(rgb, eyes)
    if brightness < limits.brightness_min:
        raise R.ImageError("too_dark", "The eyes are too dark. Turn on more light and face it.")
    if brightness > limits.brightness_max:
        raise R.ImageError("too_bright", "The eyes are too bright or washed out. Reduce the light behind or on the face.")
    if sharpness < limits.sharpness_min:
        raise R.ImageError("blurry", "The image is blurry. Hold still and let the camera focus.")
    return round(brightness), round(sharpness)


# ---------- enrollment ----------

def find_covered_face(rgb, limits=R.Limits()):
    """Exactly one covered face with usable eyes, or an ImageError that says what to change."""
    frame_mean = float(rgb.mean())
    if frame_mean < 20:
        raise R.ImageError("too_dark", "The image is too dark. Turn on more light and face it.")
    if frame_mean > 238:
        raise R.ImageError("too_bright", "The image is too bright or washed out. Reduce the light behind or on the face.")
    if face_recognition.face_locations(rgb, model="hog"):
        raise R.ImageError("not_covered", "The face is visible. Covered enrollment is only for a covered face; "
                                          "use the normal face enrollment for a visible face.")
    found = detect_covered(rgb, limits.work_side)
    if not found:
        raise R.ImageError("no_face", "No head or eyes detected. Face the camera straight on, in good light.")
    if len(found) > 1:
        raise R.ImageError("multiple_faces", "More than one face is visible. Only the student should be in the picture.")
    box, eyes = found[0]
    if box[1] - box[3] < limits.face_min_px:
        raise R.ImageError("face_too_small", "The face is too far away. Move closer to the camera.")
    brightness, sharpness = _check_strip(rgb, eyes, limits)
    return R.FaceFound(box, {"brightness": brightness, "sharpness": sharpness, "face_px": box[1] - box[3]})


def analyze_covered_image(data, limits=R.Limits()):
    """Everything covered enrollment needs from one image: checks plus the 128-number encoding."""
    rgb = R.decode_image(data, limits)
    found = find_covered_face(rgb, limits)
    eyes = detect_covered(rgb, limits.work_side)[0][1]
    return covered_encoding(rgb, eyes), found.metrics


def quick_check(data, limits=R.Limits()):
    return find_covered_face(R.decode_image(data, limits), limits).metrics


# ---------- blink on a covered face ----------

BAND_SIZE = (96, 32)


def eye_bands(frames, eyes_list):
    """The eye band of every frame cut at the SAME place (the median eye position), as small grey float images. The fixed
    place matters: cutting each frame at its own detected eyes would make detector jitter look like a change."""
    med = np.median(np.array(eyes_list, dtype=np.float64), axis=0)
    (x0, y0), (x1, y1) = med
    gap = max(float(np.hypot(x1 - x0, y1 - y0)), 1.0)
    out = []
    for rgb in frames:
        h, w = rgb.shape[:2]
        left, right = int(max(min(x0, x1) - 0.3 * gap, 0)), int(min(max(x0, x1) + 0.3 * gap, w))
        top, bottom = int(max(min(y0, y1) - 0.3 * gap, 0)), int(min(max(y0, y1) + 0.25 * gap, h))
        if right - left < 10 or bottom - top < 4:
            raise R.ImageError("no_eyes", "The eyes could not be located. Move closer to the camera.")
        grey = cv2.cvtColor(rgb[top:bottom, left:right], cv2.COLOR_RGB2GRAY)
        out.append(cv2.resize(grey, BAND_SIZE, interpolation=cv2.INTER_AREA).astype(np.float32))
    return out


def band_changes(bands):
    """How far each frame's band is from the median band (mean absolute grey-level difference)."""
    reference = np.median(np.array(bands), axis=0)
    return [float(np.abs(b - reference).mean()) for b in bands]


def change_threshold(changes, min_change):
    """A frame 'changed' above this. The floor is the configured minimum; it rises with the burst's own noise level."""
    return max(min_change, 3.0 * float(np.median(changes)))


def band_blink(changes, s, quiet_frames=2):
    """True when a short run of clearly changed frames has at least `quiet_frames` unchanged frames before and after it."""
    if len(changes) < s.min_frames:
        return False
    limit = change_threshold(changes, s.covered_blink_min_change)
    changed = [c > limit for c in changes]
    i, n = 0, len(changed)
    while i < n:
        if not changed[i]:
            i += 1
            continue
        j = i
        while j < n and changed[j]:
            j += 1
        if j - i <= s.blink_max_closed and sum(not c for c in changed[:i]) >= quiet_frames and sum(not c for c in changed[j:]) >= quiet_frames:
            return True
        i = j
    return False


def open_eyed_frames(frames, eyes, changes, s, count=3):
    """Indices (sharpest first) of up to `count` frames that look like the typical, open-eyed band."""
    limit = change_threshold(changes, s.covered_blink_min_change)
    scored = []
    for i, change in enumerate(changes):
        if change <= limit:
            scored.append((strip_quality(frames[i], eyes[i])[1], i))
    scored.sort(reverse=True)
    return [i for _, i in scored[:count]]


# ---------- the gate ----------

def _dlib_sees_a_face(frames, s, sample=8, share=0.5):
    """Downgrade guard: if a normal face is visible, the strong full-face check must be used, not this weaker one."""
    step = max(len(frames) // sample, 1)
    picked = frames[::step][:sample]
    seen = sum(1 for rgb in picked if len(face_recognition.face_locations(V._shrink(rgb, s.work_side)[0], number_of_times_to_upsample=0)) == 1)
    return seen / len(picked) >= share


def verify_covered(frame_bytes, student_id, templates, s=V.Settings(), claimed=None):
    """Is the covered person in front of the camera the student `student_id`? One burst of JPEG frames in, a Decision out.
    `templates` are that student's covered-face encodings. Granted entries are always flagged for warden review."""
    if not templates:
        return V.deny("covered_not_enrolled", claimed=claimed)
    with V.DLIB_LOCK:
        return _verify_covered(frame_bytes, student_id, templates, s, claimed)


def _verify_covered(frame_bytes, student_id, templates, s, claimed):
    frames = []
    for blob in frame_bytes:
        try:
            frames.append(R.decode_image(blob, s.limits))
        except R.ImageError:
            continue
    if len(frames) < s.min_frames:
        return V.deny("bad_frames", claimed=claimed)
    if _dlib_sees_a_face(frames, s):
        return V.deny("face_visible", claimed=claimed)

    detections = [detect_covered(rgb, s.work_side) for rgb in frames]
    total = len(frames)
    if sum(1 for d in detections if len(d) > 1) / total > s.multi_face_ratio:
        return V.deny("multiple_faces", claimed=claimed, checks={"face": "failed", "liveness": "skipped", "recognition": "skipped"})
    if sum(1 for d in detections if len(d) == 1) / total < s.face_frame_ratio:
        return V.deny("no_face", claimed=claimed, checks={"face": "failed", "liveness": "skipped", "recognition": "skipped"})

    eyes = [d[0][1] if len(d) == 1 else None for d in detections]
    usable = [i for i, e in enumerate(eyes) if e]
    try:
        bands = eye_bands([frames[i] for i in usable], [eyes[i] for i in usable])
    except R.ImageError as err:
        return V.deny(err.code, err.message, claimed=claimed, checks={"face": "failed", "liveness": "skipped", "recognition": "skipped"})
    changes = band_changes(bands)
    if not band_blink(changes, s):
        return V.deny("liveness_failed", claimed=claimed, liveness="failed",
                      checks={"face": "ok", "liveness": "failed", "recognition": "skipped"})

    picked = open_eyed_frames([frames[i] for i in usable], [eyes[i] for i in usable], changes, s)
    indices = [usable[i] for i in picked]
    if not indices:
        return V.deny("bad_frames", claimed=claimed, liveness="passed", checks={"face": "ok", "liveness": "ok", "recognition": "skipped"})
    try:
        _check_strip(frames[indices[0]], eyes[indices[0]], s.limits)
    except R.ImageError as err:
        return V.deny(err.code, err.message, claimed=claimed, liveness="passed",
                      checks={"face": "failed", "liveness": "ok", "recognition": "skipped"})

    candidates = [covered_encoding(frames[i], eyes[i]) for i in indices]
    distance = V._min_distance(templates, np.asarray(candidates, dtype=np.float64))
    checks = {"face": "ok", "liveness": "ok", "recognition": "failed"}
    if distance >= s.covered_threshold:
        return V.deny("covered_not_matched", claimed=claimed, distance=distance, liveness="passed", checks=checks)
    checks["recognition"] = "ok"
    return V.Decision(True, "granted", V.MESSAGES["granted"], method="Eye-Region", student_id=student_id, distance=distance,
                      liveness="passed", needs_review=True, checks=checks, claimed=claimed)
