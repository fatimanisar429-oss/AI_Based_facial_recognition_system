"""Test pictures. No camera is needed anywhere in the automated tests.

The one real face comes from scikit-image's bundled sample photo (a public-domain NASA portrait), cropped to a
single head. Everything else is synthetic. Tests that need to control recognition results use mocks instead.
"""
import io

import cv2
import numpy as np
from PIL import Image
from skimage import data


def jpeg(rgb, quality=92):
    ok, buf = cv2.imencode(".jpg", np.ascontiguousarray(rgb[:, :, ::-1]), [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return buf.tobytes()


def png(rgb):
    out = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb)).save(out, "PNG")
    return out.getvalue()


def face_rgb():
    """One clear face, 560x620 pixels."""
    crop = data.astronaut()[20:330, 80:360]
    return np.ascontiguousarray(cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))


def paint_eyes_closed(rgb):
    """Fake a blink: cover each eye with skin colour (slightly larger than the eye, like a closed eyelid) and draw a
    lash line. This brings the eye aspect ratio down to about 0.15, which is what a real closed eye measures. It is a
    stand-in so the blink detector can be exercised without a camera; it is NOT a real person blinking."""
    import face_recognition
    out = rgb.copy()
    marks = face_recognition.face_landmarks(rgb)[0]
    for side in ("left_eye", "right_eye"):
        pts = np.array(marks[side], np.float32)
        centre = pts.mean(axis=0)
        lid = ((pts - centre) * 1.3 + centre).astype(np.int32)
        x, y, w, h = cv2.boundingRect(pts.astype(np.int32))
        above = max(y - int(0.9 * h), 0)
        skin = out[above:above + 4, x:x + w].reshape(-1, 3).mean(axis=0)       # skin just above the eye
        cv2.fillPoly(out, [cv2.convexHull(lid)], tuple(int(v) for v in skin))
        cv2.ellipse(out, (x + w // 2, y + h // 2 + int(0.1 * h)), (int(w * 0.55), max(h // 8, 1)), 0, 0, 360,
                    tuple(int(v * 0.45) for v in skin), -1)
    return np.ascontiguousarray(out)


def blink_burst(open_rgb, closed_rgb, pattern="OOOOOCCOOOOOOOOO", quality=85):
    """A list of JPEG frames: O = eyes open, C = eyes closed."""
    opened, closed = jpeg(open_rgb, quality), jpeg(closed_rgb, quality)
    return [closed if ch == "C" else opened for ch in pattern]


def face_variants():
    """Five slightly different pictures of the same face (light, size, compression, mirror)."""
    base = face_rgb()
    return [
        base,
        np.clip(base.astype(np.int16) + 18, 0, 255).astype(np.uint8),
        cv2.resize(base, None, fx=0.85, fy=0.85, interpolation=cv2.INTER_AREA),
        np.ascontiguousarray(base[:, ::-1]),
        np.clip(base.astype(np.int16) - 15, 0, 255).astype(np.uint8),
    ]
