"""Image validation and feature extraction, run on real pictures (no camera, no mocks)."""
import cv2
import numpy as np
import pytest

from app.services import recognition as R
from imaging import face_rgb, jpeg, png


@pytest.fixture(scope="module")
def face():
    return face_rgb()


@pytest.fixture(scope="module")
def analysis(face):
    """One real analysis, shared by several tests (dlib is slow)."""
    return R.analyze_image(jpeg(face))


def code_of(data, fn=R.quick_check, **kw):
    with pytest.raises(R.ImageError) as err:
        fn(data, **kw)
    return err.value.code


# ---------- decoding: things that are not usable images ----------

@pytest.mark.parametrize("blob,code", [
    (b"", "no_image"),
    (b"hello, I am not a picture at all", "not_image"),
    (b"GIF89a" + b"\x00" * 100, "not_image"),
    (b"%PDF-1.4 fake", "not_image"),
])
def test_rejects_non_images(blob, code):
    assert code_of(blob) == code


def test_rejects_corrupt_jpeg(face):
    assert code_of(jpeg(face)[:2500]) == "corrupt"


def test_rejects_jpeg_header_with_garbage_body():
    assert code_of(b"\xff\xd8\xff\xe0" + b"garbage" * 50) == "corrupt"


def test_rejects_oversize_file(face):
    assert code_of(jpeg(face), limits=R.Limits(max_bytes=1000)) == "too_large"


def test_rejects_tiny_picture(face):
    assert code_of(jpeg(face[:100, :100])) == "too_small"


def test_rejects_decompression_bomb():
    """A small PNG file that claims to be 30000 x 30000 pixels must be refused before it is decoded."""
    from PIL import Image
    import io
    out = io.BytesIO()
    Image.new("L", (30000, 30000)).save(out, "PNG")
    assert len(out.getvalue()) < 2_000_000
    assert code_of(out.getvalue(), fn=R.decode_image, limits=R.Limits(max_bytes=10**7)) == "too_large"


def test_png_and_jpeg_are_both_accepted(face):
    for blob in (jpeg(face), png(face)):
        assert R.decode_image(blob).dtype == np.uint8


def test_large_pictures_are_shrunk_and_contiguous(face):
    big = cv2.resize(face, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)       # 1680 x 1860
    out = R.decode_image(jpeg(big, 80), R.Limits(max_bytes=10**7))
    assert max(out.shape[:2]) == 960 and out.flags["C_CONTIGUOUS"] and out.shape[2] == 3


# ---------- quality and face finding ----------

def test_dark_picture_rejected(face):
    assert code_of(jpeg((face * 0.15).astype(np.uint8))) == "too_dark"


def test_pitch_black_picture_rejected():
    assert code_of(jpeg(np.zeros((480, 640, 3), np.uint8))) == "too_dark"


def test_washed_out_picture_rejected(face):
    assert code_of(jpeg(np.clip(face.astype(np.int16) + 140, 0, 255).astype(np.uint8))) == "too_bright"


def test_blurry_picture_rejected(face):
    assert code_of(jpeg(cv2.GaussianBlur(face, (0, 0), 6))) == "blurry"


def test_picture_without_a_face_rejected():
    noise = np.random.default_rng(1).integers(0, 255, (480, 640, 3), dtype=np.uint8)
    assert code_of(jpeg(noise)) == "no_face"
    assert code_of(jpeg(np.full((480, 640, 3), 128, np.uint8))) == "no_face"


def test_two_faces_rejected(face):
    assert code_of(jpeg(np.hstack([face, face]))) == "multiple_faces"


def test_distant_face_rejected(face):
    small = cv2.resize(face, None, fx=0.25, fy=0.25)
    framed = cv2.copyMakeBorder(small, 200, 200, 300, 300, cv2.BORDER_CONSTANT, value=(120, 120, 120))
    assert code_of(jpeg(framed)) == "face_too_small"


def test_good_picture_reports_its_quality_numbers(face):
    metrics = R.quick_check(jpeg(face))
    assert set(metrics) == {"brightness", "sharpness", "face_px"}
    assert 45 <= metrics["brightness"] <= 215 and metrics["sharpness"] >= 30 and metrics["face_px"] >= 90


def test_thresholds_are_configurable(face):
    strict = R.Limits(sharpness_min=10_000)
    assert code_of(jpeg(face), limits=strict) == "blurry"
    assert R.quick_check(jpeg(face), R.Limits(sharpness_min=1))


def test_every_error_has_a_human_message(face):
    for blob in (b"", b"xx", jpeg(face)[:2500]):
        with pytest.raises(R.ImageError) as err:
            R.quick_check(blob)
        assert err.value.message and err.value.message[0].isupper()


# ---------- features ----------

def test_analysis_shapes(analysis):
    assert analysis.encoding.shape == (128,)
    assert analysis.eye_template.shape == (R.EYE_GRID[0] * R.EYE_GRID[1] * R.LBP_BINS,)
    assert analysis.eye_template.dtype == np.float32


def test_each_eye_grid_cell_is_a_probability_distribution(analysis):
    cells = analysis.eye_template.reshape(-1, R.LBP_BINS)
    assert np.allclose(cells.sum(axis=1), 1.0, atol=1e-5) and (cells >= 0).all()


def test_same_picture_gives_identical_features(face, analysis):
    again = R.analyze_image(jpeg(face))
    assert float(R.face_distance(analysis.encoding, again.encoding)[0]) == 0.0
    assert R.chi_square_distance(analysis.eye_template, again.eye_template) == 0.0


def test_features_are_stable_when_lighting_and_size_change(face, analysis):
    brighter = np.clip(face.astype(np.int16) + 20, 0, 255).astype(np.uint8)
    smaller = cv2.resize(face, None, fx=0.7, fy=0.7, interpolation=cv2.INTER_AREA)
    for variant in (brighter, smaller, np.ascontiguousarray(face[:, ::-1])):    # the mirror needs contiguity
        other = R.analyze_image(jpeg(variant))
        assert float(R.face_distance(analysis.encoding, other.encoding)[0]) < 0.35
        assert R.chi_square_distance(analysis.eye_template, other.eye_template) < 0.5


def test_eye_template_changes_when_the_eye_area_changes(face, analysis):
    """Sanity check that the template really describes the eye region (not a constant)."""
    altered = face.copy()
    marks = __import__("face_recognition").face_landmarks(face)[0]
    xs, ys = zip(*(marks["left_eye"] + marks["right_eye"]))
    altered[min(ys) - 6:max(ys) + 6, min(xs) - 6:max(xs) + 6] = 128            # grey patch over both eyes
    other = R.analyze_image(jpeg(altered), R.Limits(sharpness_min=1))
    assert R.chi_square_distance(analysis.eye_template, other.eye_template) > 0.5


# ---------- comparing ----------

def test_face_distance_math():
    a = np.zeros(128)
    b = np.ones(128) * 0.05
    assert R.face_distance(a, a)[0] == 0
    assert R.face_distance(a, b)[0] == pytest.approx(np.sqrt(128) * 0.05)
    assert R.face_distance(np.vstack([a, b]), a).tolist() == [0.0, pytest.approx(np.sqrt(128) * 0.05)]


def test_chi_square_is_zero_for_equal_and_grows_with_difference():
    a = np.array([0.5, 0.5, 0.0])
    assert R.chi_square_distance(a, a) == 0
    assert R.chi_square_distance(a, np.array([0.0, 0.5, 0.5])) > R.chi_square_distance(a, np.array([0.4, 0.6, 0.0]))


def test_max_pairwise_distance():
    e = [np.zeros(128), np.zeros(128), np.ones(128) * 0.1]
    assert R.max_pairwise_distance(e) == pytest.approx(np.sqrt(128) * 0.1)
    assert R.max_pairwise_distance([np.zeros(128)]) == 0.0
