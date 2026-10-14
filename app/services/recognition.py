"""Image checks and biometric feature extraction. No Flask code here, so it can be tested on its own.

What this module does
    * decode_image    - accepts only real JPEG/PNG data of a sane size and returns an RGB array
    * find_face       - exactly one usable face, bright enough and sharp enough, or a clear ImageError
    * analyze_image   - face encoding (128 numbers) plus an eye-region texture template

Terms (see the README): "face detection" only finds a face; "face recognition" compares the 128-number
encoding of a face with stored ones; the "eye-region (periocular) template" describes the skin around the
eyes. None of this is true iris recognition, which needs near-infrared iris imaging.
"""
import io
from dataclasses import dataclass

import cv2
import face_recognition
import numpy as np
from PIL import Image, UnidentifiedImageError
from skimage.feature import local_binary_pattern

TEMPLATE_VERSION = 1          # bump if the way templates are computed ever changes
EYE_CROP_SIZE = (128, 48)     # width, height of the normalised eye-region crop
LBP_POINTS, LBP_RADIUS = 8, 1
LBP_BINS = LBP_POINTS + 2     # "uniform" LBP gives 10 distinct codes
EYE_GRID = (4, 2)             # columns, rows of histogram cells


class ImageError(Exception):
    """A problem with the submitted image that the user can fix. `code` is for programs, `message` for people."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 2 * 1024 * 1024
    min_side: int = 240
    max_side: int = 4096
    work_side: int = 960
    face_min_px: int = 90
    brightness_min: float = 45
    brightness_max: float = 215
    sharpness_min: float = 30

    @classmethod
    def from_config(cls, cfg):
        return cls(cfg["MAX_IMAGE_BYTES"], cfg["MIN_IMAGE_SIDE"], cfg["MAX_IMAGE_SIDE"], cfg["WORK_IMAGE_SIDE"],
                   cfg["FACE_MIN_PX"], cfg["BRIGHTNESS_MIN"], cfg["BRIGHTNESS_MAX"], cfg["SHARPNESS_MIN"])


@dataclass
class FaceFound:
    location: tuple      # (top, right, bottom, left) in the working image
    metrics: dict        # numbers shown to the admin: brightness, sharpness, face_px


@dataclass
class Analysis:
    encoding: np.ndarray        # 128 floats
    eye_template: np.ndarray    # EYE_GRID cells x LBP_BINS floats
    metrics: dict


# ---------- decoding ----------

def decode_image(data, limits=Limits()):
    """Bytes from the browser -> RGB uint8 array (C-contiguous, shrunk to limits.work_side)."""
    if not data:
        raise ImageError("no_image", "No image was received.")
    if len(data) > limits.max_bytes:
        raise ImageError("too_large", "The image is too large. Please try again.")
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        raise ImageError("not_image", "That file is not a JPEG or PNG image.")

    try:  # read only the header first: a tiny file can claim to be a gigantic picture
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
    except Image.DecompressionBombError:
        raise ImageError("too_large", "The image dimensions are too large.") from None
    except (UnidentifiedImageError, OSError):
        raise ImageError("corrupt", "The image could not be read. Please capture it again.") from None
    if max(width, height) > limits.max_side:
        raise ImageError("too_large", "The image dimensions are too large.")
    if min(width, height) < limits.min_side:
        raise ImageError("too_small", "The image is too small. Use a higher camera resolution.")

    bgr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ImageError("corrupt", "The image could not be read. Please capture it again.")
    longest = max(bgr.shape[:2])
    if longest > limits.work_side:
        scale = limits.work_side / longest
        bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    # dlib refuses arrays that are not C-contiguous, so make the layout explicit.
    return np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))


# ---------- quality and face finding ----------

def _face_crop_gray(rgb, location):
    top, right, bottom, left = location
    h, w = rgb.shape[:2]
    crop = rgb[max(top, 0):min(bottom, h), max(left, 0):min(right, w)]
    return cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)


def measure_face(rgb, location):
    """Brightness (0-255) and sharpness of the face area. Sharpness is measured on a fixed 200px crop so
    it does not depend on how big the picture is."""
    gray = _face_crop_gray(rgb, location)
    if gray.size == 0:
        raise ImageError("no_face", "No face detected.")
    brightness = float(gray.mean())
    fixed = cv2.resize(gray, (200, 200), interpolation=cv2.INTER_AREA if gray.shape[0] > 200 else cv2.INTER_CUBIC)
    sharpness = float(cv2.Laplacian(fixed, cv2.CV_64F).var())
    return brightness, sharpness


def find_face(rgb, limits=Limits()):
    """Exactly one face of usable size and quality, or an ImageError that says what to change."""
    # A whole-frame check first: in a nearly black or washed-out frame the detector just says "no face",
    # which would send the person looking for the wrong fix.
    frame_mean = float(rgb.mean())
    if frame_mean < 20:
        raise ImageError("too_dark", "The image is too dark. Turn on more light and face it.")
    if frame_mean > 238:
        raise ImageError("too_bright", "The image is too bright or washed out. Reduce the light behind or on the face.")

    locations = face_recognition.face_locations(rgb, model="hog")
    if not locations:
        # Lighting is the most common reason a real face is not found, so say so when the frame is extreme.
        if frame_mean > 200:
            raise ImageError("too_bright", "No face found and the image is very bright. Reduce the light on or behind the face.")
        if frame_mean < 55:
            raise ImageError("too_dark", "No face found and the image is very dark. Turn on more light.")
        raise ImageError("no_face", "No face detected. Face the camera straight on, in good light.")
    if len(locations) > 1:
        raise ImageError("multiple_faces", "More than one face is visible. Only the student should be in the picture.")

    location = locations[0]
    top, right, bottom, left = location
    face_px = right - left
    if face_px < limits.face_min_px:
        raise ImageError("face_too_small", "The face is too far away. Move closer to the camera.")

    brightness, sharpness = measure_face(rgb, location)
    if brightness < limits.brightness_min:
        raise ImageError("too_dark", "The image is too dark. Turn on more light and face it.")
    if brightness > limits.brightness_max:
        raise ImageError("too_bright", "The image is too bright or washed out. Reduce the light behind or on the face.")
    if sharpness < limits.sharpness_min:
        raise ImageError("blurry", "The image is blurry. Hold still and let the camera focus.")

    return FaceFound(location, {"brightness": round(brightness), "sharpness": round(sharpness),
                                "face_px": face_px})


def quick_check(data, limits=Limits()):
    """Fast check used while capturing (no encoding). Returns the quality numbers or raises ImageError."""
    return find_face(decode_image(data, limits), limits).metrics


# ---------- features ----------

def eye_region_template(rgb, location):
    """Texture description of the area around both eyes (eyes and eyebrows).

    Steps: crop the box around the eye and eyebrow landmarks, grey-scale it, equalise the contrast so lighting
    matters less, resize to a fixed size, compute Local Binary Patterns and describe them with one histogram
    per grid cell. Result: float32 array of EYE_GRID[0] * EYE_GRID[1] * LBP_BINS numbers.
    """
    marks = face_recognition.face_landmarks(rgb, [location])
    if not marks:
        raise ImageError("no_eyes", "The eyes could not be located. Face the camera straight on.")
    points = np.array(marks[0]["left_eye"] + marks[0]["right_eye"]
                      + marks[0]["left_eyebrow"] + marks[0]["right_eyebrow"], dtype=np.float32)
    x0, y0 = points.min(axis=0)
    x1, y1 = points.max(axis=0)
    pad_x, pad_y = 0.15 * (x1 - x0), 0.25 * (y1 - y0)
    h, w = rgb.shape[:2]
    box = (int(max(x0 - pad_x, 0)), int(max(y0 - pad_y, 0)), int(min(x1 + pad_x, w)), int(min(y1 + pad_y, h)))
    if box[2] - box[0] < 20 or box[3] - box[1] < 8:
        raise ImageError("no_eyes", "The eyes could not be located. Move closer to the camera.")

    gray = cv2.cvtColor(rgb[box[1]:box[3], box[0]:box[2]], cv2.COLOR_RGB2GRAY)
    gray = cv2.equalizeHist(cv2.resize(gray, EYE_CROP_SIZE, interpolation=cv2.INTER_AREA))
    codes = local_binary_pattern(gray, LBP_POINTS, LBP_RADIUS, method="uniform")

    cols, rows = EYE_GRID
    cell_h, cell_w = codes.shape[0] // rows, codes.shape[1] // cols
    parts = []
    for r in range(rows):
        for c in range(cols):
            cell = codes[r * cell_h:(r + 1) * cell_h, c * cell_w:(c + 1) * cell_w]
            hist, _ = np.histogram(cell, bins=LBP_BINS, range=(0, LBP_BINS))
            parts.append(hist / max(hist.sum(), 1))
    return np.concatenate(parts).astype(np.float32)


def analyze_image(data, limits=Limits(), jitters=1):
    """Everything enrollment needs from one image: checks, face encoding, eye-region template."""
    rgb = decode_image(data, limits)
    found = find_face(rgb, limits)
    encodings = face_recognition.face_encodings(rgb, [found.location], num_jitters=jitters)
    if not encodings:
        raise ImageError("encoding_failed", "The face could not be analysed. Please capture it again.")
    return Analysis(np.asarray(encodings[0], dtype=np.float64), eye_region_template(rgb, found.location), found.metrics)


# ---------- comparing ----------

def face_distance(known, candidate):
    """Euclidean distance between a candidate encoding and one or many stored ones. Smaller = more alike."""
    known = np.atleast_2d(np.asarray(known, dtype=np.float64))
    return np.linalg.norm(known - np.asarray(candidate, dtype=np.float64), axis=1)


def chi_square_distance(a, b):
    """Distance between two eye-region histograms (used by the fallback in Phase 7)."""
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    return float(0.5 * np.sum((a - b) ** 2 / (a + b + 1e-10)))


def max_pairwise_distance(encodings):
    """Largest distance between any two of the encodings (0 if there is only one)."""
    worst = 0.0
    for i in range(len(encodings)):
        for j in range(i + 1, len(encodings)):
            worst = max(worst, float(face_distance(encodings[i], encodings[j])[0]))
    return worst
