"""Deciding whether a person at the entry terminal may enter. No Flask code here.

Pipeline for one attempt (a short burst of camera frames):
    1. Frames      - decode them; the picture must show exactly ONE face in most frames
    2. Liveness    - look for a natural blink (the eye aspect ratio dips and recovers)
    3. Quality     - pick the sharpest open-eyed frame and check it like an enrollment picture
    4. Recognition - compare its face encoding with every enrolled, active student
    5. Fallback    - only if switched on: an eye-region comparison may CONFIRM a borderline face match
    6. Decision    - granted only if every required step passed

Every step that fails produces a Decision with a stable `code` (for the entry log) and a short `message` (for the
person at the gate). A denied attempt is never attributed to a student: we do not know who is in front of the camera.

Honest limits: a blink check stops a still photograph but NOT a replayed video or a determined attacker, and
face recognition is not production-grade biometric security.
"""
import threading
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import face_recognition
import numpy as np

from . import recognition as R

# dlib's detectors are shared objects; running them from several request threads at once is not guaranteed safe.
DLIB_LOCK = threading.Lock()

MESSAGES = {
    "granted": "Access granted.",
    "liveness_failed": "Liveness check failed. Look at the camera and blink twice, then try again.",
    "not_recognized": "Face not recognised.",
    "ambiguous": "Verification was inconclusive. Please contact the warden.",
    "no_face": "No face detected. Face the camera straight on, in good light.",
    "face_covered": "Your face is covered, so it cannot be verified automatically. Please ask the warden to verify you in person.",
    "multiple_faces": "More than one face is visible. Only one person may be in front of the camera.",
    "bad_frames": "The camera pictures could not be used. Please try again.",
    "no_enrolled": "No students are enrolled yet.",
    "processing_error": "Verification could not be completed. Please try again.",
    "guest_candidate": "Guest recognised.",
}


def _verify_limits(cfg):
    """Quality limits for the gate. The sharpness limit is lower than at enrollment: a person who is walking up and blinking
    is a little softer than one sitting still, and a soft picture only makes the match WORSE (never a false accept), so the
    face distance, not a hard blur rule, decides."""
    from dataclasses import replace
    base = R.Limits.from_config(cfg)
    return replace(base, sharpness_min=min(base.sharpness_min, cfg.get("VERIFY_SHARPNESS_MIN", 12)))


@dataclass(frozen=True)
class Settings:
    face_threshold: float = 0.55
    face_margin: float = 0.04
    eye_fallback: bool = False
    eye_threshold: float = 0.06
    eye_margin: float = 0.05
    face_fallback_max: float = 0.6
    min_frames: int = 10
    max_frames: int = 40
    work_side: int = 640
    face_frame_ratio: float = 0.6
    multi_face_ratio: float = 0.2
    blink_drop: float = 0.82
    blink_open: float = 0.88
    blink_closed_max_ear: float = 0.27
    blink_max_closed: int = 8
    ear_min_baseline: float = 0.18
    limits: R.Limits = field(default_factory=R.Limits)

    @classmethod
    def from_config(cls, cfg):
        return cls(
            face_threshold=cfg["FACE_THRESHOLD"], face_margin=cfg["FACE_MARGIN"], eye_fallback=cfg["EYE_FALLBACK_ENABLED"],
            eye_threshold=cfg["EYE_THRESHOLD"], eye_margin=cfg["EYE_MARGIN"], face_fallback_max=cfg["FACE_FALLBACK_MAX"],
            min_frames=cfg["LIVENESS_MIN_FRAMES"], max_frames=cfg["LIVENESS_MAX_FRAMES"], work_side=cfg["LIVENESS_WORK_SIDE"],
            face_frame_ratio=cfg["FACE_FRAME_RATIO"], multi_face_ratio=cfg["MULTI_FACE_RATIO"],
            blink_drop=cfg["BLINK_DROP_RATIO"], blink_open=cfg["BLINK_OPEN_RATIO"],
            blink_closed_max_ear=cfg["BLINK_CLOSED_MAX_EAR"], blink_max_closed=cfg["BLINK_MAX_CLOSED_FRAMES"],
            ear_min_baseline=cfg["EAR_MIN_BASELINE"], limits=_verify_limits(cfg))


@dataclass
class Decision:
    granted: bool
    code: str                                  # "granted" or the failure code
    message: str                               # safe to show at the terminal
    method: str = "None"                       # Face / Eye-Region / None
    student_id: int = None                     # only ever set when granted
    distance: float = None                     # best face distance (stored in the log; never shown at the terminal)
    liveness: str = "not_checked"              # passed / failed / not_checked
    needs_review: bool = False
    checks: dict = field(default_factory=lambda: {"face": "skipped", "liveness": "skipped", "recognition": "skipped"})
    # Guests: verification only says "this face is guest X" (code guest_candidate, NOT granted). The server then checks the
    # visit rules and turns it into a real grant or a refusal. Unresolved, it stays a denial.
    subject_type: str = None                   # "guest" or None
    guest_id: int = None
    visit_request_id: int = None


def deny(code, message=None, **kw):
    return Decision(False, code, message or MESSAGES.get(code, "Access denied."), **kw)


# ---------- blink detection ----------

def eye_aspect_ratio(points):
    """EAR from the 6 landmark points of one eye (dlib order): tall opening / wide opening. About 0.25-0.40 when
    the eye is open and falls towards 0.1 when it closes."""
    p = np.asarray(points, dtype=np.float64)
    vertical = np.linalg.norm(p[1] - p[5]) + np.linalg.norm(p[2] - p[4])
    return float(vertical / (2.0 * np.linalg.norm(p[0] - p[3]) + 1e-9))


def blink_detected(ears, s=Settings()):
    """ears: one EAR number per frame (None where no single face was found). Returns (blinked, reason).

    A blink is a short run of 'closed' frames (EAR clearly below this person's normal AND below an absolute limit)
    that has open-eyed frames both before and after it."""
    values = [e for e in ears if e is not None]
    if len(values) < s.min_frames:
        return False, "too_few_measurements"
    baseline = float(np.percentile(values, 75))             # eyes are open most of the time
    if baseline < s.ear_min_baseline:
        return False, "eyes_not_open"
    closed_below = min(baseline * s.blink_drop, s.blink_closed_max_ear)
    open_above = baseline * s.blink_open

    state = ["closed" if v < closed_below else "open" if v >= open_above else "between" for v in values]
    i, n = 0, len(state)
    while i < n:
        if state[i] != "closed":
            i += 1
            continue
        j = i
        while j < n and state[j] == "closed":
            j += 1
        run = j - i
        opened_before = any(x == "open" for x in state[:i])
        opened_after = any(x == "open" for x in state[j:])
        if run <= s.blink_max_closed and opened_before and opened_after:
            return True, "blink"
        i = j
    return False, "no_blink"


# ---------- frames ----------

def _shrink(rgb, side):
    longest = max(rgb.shape[:2])
    if longest <= side:
        return rgb, 1.0
    scale = side / longest
    return np.ascontiguousarray(cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)), 1 / scale


def _steady_box(box, median, tolerance):
    """Use the shared median box while the face has not moved much, otherwise the frame's own box."""
    face_w = max(median[1] - median[3], 1)
    shift = max(abs(box[0] - median[0]), abs(box[1] - median[1]), abs(box[2] - median[2]), abs(box[3] - median[3]))
    return median if shift <= tolerance * face_w else box


_YUNET_FILE = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
_yunet = None


def _yunet_detector():
    global _yunet
    if _yunet is None:
        _yunet = cv2.FaceDetectorYN.create(str(_YUNET_FILE), "", (320, 320), 0.6, 0.3, 5000)
    return _yunet


def looks_covered(frames, s, min_share=0.4):
    """True when the frames show a face-like head that dlib could not use (a niqab, a scarf, a mask: the eyes are visible).

    dlib's detector found a face in 0 of 91 covered test pictures; YuNet found one in 71 of 91. It only decides which MESSAGE
    the person sees. Nothing is ever recognised or granted from a covered face: matching just the eye area let about 17 in 100
    strangers through at a point where 8 in 100 real people were refused (docs/recognition-limits.md, section 3b)."""
    try:
        detector = _yunet_detector()
    except cv2.error:
        return False                                # model file missing: keep the plain "no face" message
    seen = 0
    for rgb in frames:
        small, _ = _shrink(rgb, s.work_side)
        height, width = small.shape[:2]
        detector.setInputSize((width, height))
        _, found = detector.detect(cv2.cvtColor(small, cv2.COLOR_RGB2BGR))
        if found is not None and len(found) == 1:
            seen += 1
    return bool(frames) and seen / len(frames) >= min_share


def measure_frames(frames, s):
    """For every frame: how many faces, where the single face is, and its EAR.

    Two things keep the blink measurement steady (we measured both effects):
      * faces are FOUND on a shrunken copy (fast) but the eye landmarks are read on the full-size picture;
      * dlib's landmarks react strongly to tiny shifts of the face box, which would look like blinks. While the
        person is roughly still, every frame therefore uses the same (median) box, so a change of EAR means the eye
        changed, not the box."""
    counts, found_boxes = [], []
    for rgb in frames:
        small, up = _shrink(rgb, s.work_side)
        found = face_recognition.face_locations(small, number_of_times_to_upsample=0)
        counts.append(len(found))
        if len(found) != 1:
            found_boxes.append(None)
            continue
        h, w = rgb.shape[:2]
        top, right, bottom, left = found[0]
        found_boxes.append((max(int(top * up), 0), min(int(right * up), w), min(int(bottom * up), h), max(int(left * up), 0)))

    present = [b for b in found_boxes if b]
    median = tuple(int(np.median([b[i] for b in present])) for i in range(4)) if present else None

    boxes, ears = [], []
    for rgb, box in zip(frames, found_boxes):
        if box is None:
            boxes.append(None)
            ears.append(None)
            continue
        ears.append(_robust_ear(rgb, _steady_box(box, median, tolerance=0.2)))
        boxes.append(box)                       # the frame's own box is kept for picking the best frame
    return counts, boxes, ears


def _robust_ear(rgb, box):
    """EAR of one frame: the median of three readings taken with the face box slightly smaller, as found, and
    slightly bigger. A single reading can swing with the exact box; the median of three does not."""
    top, right, bottom, left = box
    cy, cx = (top + bottom) / 2, (left + right) / 2
    h, w = rgb.shape[:2]
    readings = []
    for scale in (0.95, 1.0, 1.05):
        half_h, half_w = (bottom - top) / 2 * scale, (right - left) / 2 * scale
        variant = (max(int(cy - half_h), 0), min(int(cx + half_w), w), min(int(cy + half_h), h), max(int(cx - half_w), 0))
        marks = face_recognition.face_landmarks(rgb, [variant])
        if marks:
            readings.append((eye_aspect_ratio(marks[0]["left_eye"]) + eye_aspect_ratio(marks[0]["right_eye"])) / 2)
    return float(np.median(readings)) if readings else None


def pick_best_frames(frames, boxes, ears, s, count=3):
    """Indices (best first) of up to `count` sharpest frames with open eyes. Matching several frames of the same live
    person makes one unlucky frame (odd expression, motion blur) unable to reject a real student."""
    values = [e for e in ears if e is not None]
    baseline = float(np.percentile(values, 75))
    scored = []
    for i, (box, ear) in enumerate(zip(boxes, ears)):
        if box is None or ear is None or ear < baseline * s.blink_open:
            continue
        try:
            _, sharp = R.measure_face(frames[i], box)
        except R.ImageError:
            continue
        scored.append((sharp, i))
    scored.sort(reverse=True)
    return [i for _, i in scored[:count]]


# ---------- the decision (pure: no camera, no database) ----------

def _min_distance(known, encoding):
    """Smallest distance between any stored template and any of the candidate encodings (one array or a list of them)."""
    candidates = np.atleast_2d(np.asarray(encoding, dtype=np.float64))
    return float(min(R.face_distance(known, c).min() for c in candidates))


def decide(encoding, eye_template, gallery_faces, gallery_eyes, s):
    """Who is this? gallery_faces: {student_id: [encoding arrays]}, gallery_eyes: {student_id: [eye templates]}.
    Returns a Decision (liveness is filled in by the caller)."""
    if not gallery_faces:
        return deny("no_enrolled", checks={"face": "ok", "liveness": "ok", "recognition": "failed"})

    ranked = sorted(((_min_distance(t, encoding), sid) for sid, t in gallery_faces.items()))
    best_distance, best_id = ranked[0]
    checks = {"face": "ok", "liveness": "ok", "recognition": "failed"}

    if best_distance < s.face_threshold:
        # Two different students both close to this face: we cannot tell them apart, so do not guess.
        if len(ranked) > 1 and ranked[1][0] < s.face_threshold and ranked[1][0] - best_distance < s.face_margin:
            return deny("ambiguous", distance=best_distance, checks=checks)
        checks["recognition"] = "ok"
        return Decision(True, "granted", MESSAGES["granted"], method="Face", student_id=best_id,
                        distance=best_distance, checks=checks)

    # Not a clear face match. The eye-region check may only confirm a borderline match for the SAME student.
    if s.eye_fallback and eye_template is not None and best_distance < s.face_fallback_max and gallery_eyes.get(best_id):
        eye_ranked = sorted(
            (min(R.chi_square_distance(t, eye_template) for t in templates), sid)
            for sid, templates in gallery_eyes.items() if templates)
        eye_best, eye_id = eye_ranked[0]
        runner_up = eye_ranked[1][0] if len(eye_ranked) > 1 else float("inf")
        if eye_id == best_id and eye_best < s.eye_threshold and runner_up - eye_best >= s.eye_margin:
            checks["recognition"] = "ok"
            return Decision(True, "granted", MESSAGES["granted"], method="Eye-Region", student_id=best_id,
                            distance=best_distance, needs_review=True, checks=checks)

    return deny("not_recognized", distance=best_distance, checks=checks)


def consider_guests(decision, encoding, guest_faces, s):
    """Add guests to the picture. `decision` is the answer for students only.

    * Face matches no guest                      -> unchanged.
    * Face matches a student AND a guest         -> ambiguous, denied (enrollment forbids this, so it means a look-alike).
    * Face matches two guests about equally      -> ambiguous, denied.
    * Face matches exactly one guest only        -> a guest candidate (still NOT granted; the visit rules decide)."""
    if not guest_faces:
        return decision
    ranked = sorted((_min_distance(t, encoding), gid) for gid, t in guest_faces.items())
    best_distance, best_id = ranked[0]
    if best_distance >= s.face_threshold:
        return decision
    checks = {"face": "ok", "liveness": "ok", "recognition": "failed"}
    if decision.granted or decision.code == "ambiguous":
        return deny("ambiguous", distance=min(best_distance, decision.distance or best_distance), checks=checks)
    if len(ranked) > 1 and ranked[1][0] < s.face_threshold and ranked[1][0] - best_distance < s.face_margin:
        return deny("ambiguous", distance=best_distance, checks=checks)
    return Decision(False, "guest_candidate", MESSAGES["guest_candidate"], method="Face", distance=best_distance,
                    subject_type="guest", guest_id=best_id, checks={"face": "ok", "liveness": "ok", "recognition": "ok"})


# ---------- the whole attempt ----------

def verify(frame_bytes, gallery_faces, gallery_eyes, s=Settings(), guest_faces=None):
    """frame_bytes: list of JPEG/PNG byte strings from one burst of the camera."""
    with DLIB_LOCK:
        return _verify(frame_bytes, gallery_faces, gallery_eyes, s, guest_faces or {})


def _verify(frame_bytes, gallery_faces, gallery_eyes, s, guest_faces):
    frames = []
    for blob in frame_bytes:
        try:
            frames.append(R.decode_image(blob, s.limits))
        except R.ImageError:
            continue                                    # one broken frame does not ruin the attempt
    if len(frames) < s.min_frames:
        return deny("bad_frames")

    counts, boxes, ears = measure_frames(frames, s)
    total = len(frames)
    if sum(1 for c in counts if c > 1) / total > s.multi_face_ratio:
        return deny("multiple_faces", checks={"face": "failed", "liveness": "skipped", "recognition": "skipped"})
    single = sum(1 for c in counts if c == 1)
    if single / total < s.face_frame_ratio:
        if looks_covered(frames, s):
            # A covered face is never identified (too unreliable, see looks_covered). It is flagged so the warden sees it.
            return deny("face_covered", needs_review=True,
                        checks={"face": "failed", "liveness": "skipped", "recognition": "skipped"})
        return deny("no_face", checks={"face": "failed", "liveness": "skipped", "recognition": "skipped"})

    blinked, _ = blink_detected(ears, s)
    if not blinked:
        return deny("liveness_failed", liveness="failed",
                    checks={"face": "ok", "liveness": "failed", "recognition": "skipped"})

    indices = pick_best_frames(frames, boxes, ears, s)
    if not indices:
        return deny("bad_frames", liveness="passed", checks={"face": "ok", "liveness": "ok", "recognition": "skipped"})
    index = indices[0]
    try:
        found = R.find_face(frames[index], s.limits)               # same quality rules as enrollment
    except R.ImageError as err:
        return deny(err.code, err.message, liveness="passed",
                    checks={"face": "failed", "liveness": "ok", "recognition": "skipped"})

    encodings = face_recognition.face_encodings(frames[index], [found.location], num_jitters=1)
    if not encodings:
        return deny("bad_frames", liveness="passed", checks={"face": "failed", "liveness": "ok", "recognition": "skipped"})
    eye = None
    if s.eye_fallback:
        try:
            eye = R.eye_region_template(frames[index], found.location)
        except R.ImageError:
            eye = None

    encoding = np.asarray(encodings, dtype=np.float64)           # one row per frame used
    decision = decide(encoding, eye, gallery_faces, gallery_eyes, s)
    if decision.code == "not_recognized":
        # One unlucky frame (odd expression, motion blur) must not reject a real student: try the next-sharpest frames of the
        # same burst (their boxes are already known). A clear match stops after one frame, so it costs no extra time.
        for extra in indices[1:]:
            encodings += face_recognition.face_encodings(frames[extra], [boxes[extra]], num_jitters=1)
        encoding = np.asarray(encodings, dtype=np.float64)
        decision = decide(encoding, eye, gallery_faces, gallery_eyes, s)
    decision = consider_guests(decision, encoding, guest_faces, s)
    decision.liveness = "passed"
    return decision
