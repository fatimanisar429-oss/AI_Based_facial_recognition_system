# Recognition: what was measured, and what the system can and cannot do

This page records the evidence behind the recognition settings and the limits we state in the report. All numbers
come from runs on the development laptop in October 2026. The samples are **small**, so treat them as indications,
not as an accuracy claim. The final accuracy test with real students and the real webcam is still to be done
(SRS NFR-02).

## Terms (they are different things)

| Term | Meaning here |
|---|---|
| Face detection | Finding that a face is in the picture (dlib HOG detector). Says nothing about who it is. |
| Face recognition | Comparing the 128-number encoding of the live face with stored encodings (dlib / `face_recognition`). |
| Eye-region (periocular) recognition | Comparing a texture description (LBP histograms) of the skin around the eyes. |
| Iris recognition | Reading the iris pattern. **Not implemented**: it needs near-infrared iris imaging that a laptop camera cannot provide. |
| Liveness (blink) check | Checking that the eyes close and reopen across a burst of frames. |

## 1. Face recognition: the threshold

Encoding distances measured on 7 public photos of 4 people (one person has 4 different photos):

| Comparison | Distances |
|---|---|
| Same person, different photos | 0.05 to 0.37 |
| Different people (15 pairs) | 0.71 to 0.90 (median 0.81) |

The default threshold is **0.5** (`FACE_THRESHOLD`, from the SRS; the library default is 0.6). We tried 0.55 and went back: on
our own webcam two different enrolled students were only 0.57 apart, so 0.55 left almost no margin (and wrongly flagged a new
student as a duplicate). The enrollment duplicate check has its own, stricter `DUPLICATE_FACE_THRESHOLD` (0.4). Caveats: only 4 people; photos are not webcam frames; twins and look-alikes will be closer.

Safety rules on top of the threshold:

* If two *different* students are both under the threshold and within 0.04 of each other (`FACE_MARGIN`), the
  attempt is denied as inconclusive. We do not guess.
* The same face cannot be enrolled for two students (checked at enrollment).
* Only **active** students' templates are loaded for matching.

### 1b. Final tuning against false rejects (October 2026)

The gate log of the first real-webcam session showed that almost every rejection was **"Image blurry"** (the sharpness rule) or
**"no blink"**, not a wrong face. So the changes were made there, not by loosening who is accepted:

| Change | Why it cannot let a stranger in |
|---|---|
| The gate's sharpness limit is 12 (`VERIFY_SHARPNESS_MIN`); enrollment stays at 30 | A soft picture only makes the face distance larger. The face distance still decides. |
| The 3 sharpest open-eyed frames are matched, and the smallest distance counts | Frames of one live person; a stranger is 0.71+ away in every frame. The two-students-equal "ambiguous" rule is unchanged. |
| Threshold stays 0.5 | Same person up to 0.37 and different people from 0.71 on public photos, but only 0.57 apart for two students on our webcam: keep the margin. |
| Blink: `BLINK_DROP_RATIO` 0.82, `BLINK_OPEN_RATIO` 0.88, `BLINK_CLOSED_MAX_EAR` 0.27 | Liveness is not identity; a gentler dip is accepted, a still photo (no dip) is still refused. |

Everything above is a setting in `.env` and can be tightened again. Re-measure on the real gate camera before claiming accuracy.

## 2. Liveness (blink)

* Method: eye aspect ratio (EAR) from dlib's 68 landmarks across about 30 frames (one every 110 ms). A blink is a
  run of frames where EAR is clearly below the person's own normal (`BLINK_DROP_RATIO` 0.82, was 0.78) **and** below an absolute
  limit (`BLINK_CLOSED_MAX_EAR` 0.27, was 0.25), with open-eyed frames before and after. Typical EAR is 0.22 to 0.41 for open
  eyes (it differs a lot between people, hence the relative rule).
* Tested with a **simulated** blink (eyes painted shut in a real face picture, EAR about 0.15) through the real
  pipeline and through a real browser with a fake camera: the "blinking" person is admitted, a still picture
  of the same person is denied (SRS test cases TC-01 and TC-03).
* **Not yet tested with a real person blinking.** A real blink lasts 150 to 300 ms; the capture rate was chosen for that.
* We found the blink measurement is sensitive to the processing size and to the exact face box: the same blink read
  as EAR 0.15 at 640 and 560 px but 0.25 at 480 px. The system therefore finds faces at the full 640 px and uses a
  median of three landmark readings. This costs about 2 seconds per attempt on the development laptop; the whole
  decision takes about 3 to 4 seconds (SRS NFR-01 asks for 3 seconds on recommended hardware).
* **Limit:** a blink check stops a still photograph. It does **not** stop a replayed video or a determined attacker.
  This is not production-grade biometric security.

## 3. Eye-region fallback: measured, and switched OFF by default

SRS FR-06 asks for an eye-region fallback when the face cannot be matched or is covered. We built and tested it, and
measured whether it is reliable enough to trust:

| Test | Result |
|---|---|
| Eye-region template (LBP) distance, same person vs different people | Same person: 0.140. Different people: min 0.117, median 0.486, max 1.123 (14 pairs). The same-person distance is **not smaller** than the closest different person, so the template cannot separate people reliably. |
| Lower half of the face covered (a white rectangle over nose and mouth) | dlib found a face in 0 of 5 pictures; OpenCV's Haar face detector found one in 1 of 5 and no eyes in any. With the specified libraries the eyes of a covered face could not even be located. |

### 3b. Covered face (niqab): measured on 91 public photos (October 2026)

A niqab leaves only the eye slit. We simulated it on 91 public LFW photos (everything dark except the eyebrows-to-under-eyes
strip) and compared dlib's face encoding on the covered pictures (genuine = the same photo with small changes in light, angle and
sharpness, which is **easier** than real life):

| | Full face | Covered (eyes only) |
|---|---|---|
| dlib finds the face | 91 of 91 | **0 of 91** (YuNet finds 71 of 91) |
| Same person, mean distance | 0.06 | 0.29 (worst 0.65) |
| Different people, median / closest | 0.82 / 0.31 | 0.61 / 0.10 |
| At threshold 0.55 (not used): real people refused / strangers accepted (per comparison) | 0 % / 0.3 % | 3.9 % / **30 %** |
| At threshold 0.45 | 0 % / 0.03 % | 14.5 % / **9.7 %** |

With a gallery of N students the chance that a stranger matches somebody grows with N. So **a covered face cannot be identified
safely with this stack**, and no threshold fixes it: the two groups of distances overlap.

Decision (October 2026, revised): a covered face is **never searched against the whole gallery**. The terminal (YuNet notices the
covered head) asks for the **registration number** and then compares the eyes with **that one student's covered-face templates only**
(1:1, `app/services/covered.py`). That removes the "stranger matches somebody in a gallery of N" effect; what is left is the chance
that someone who knows or guesses the number has eyes close enough to that student's.

| Step | How |
|---|---|
| Enrollment (admin, after the normal enrollment) | 5 samples with the covering ON; stored as encrypted encodings in `covered_profile` |
| Gate | Covered head seen, so the page asks for the registration number and sends the **same** pictures again with it (no second blink needed) |
| Downgrade guard | If dlib finds a normal face in the pictures, the covered path refuses (`face_visible`): the strong full-face check must be used |
| Liveness | Short change in the eye band that returns to normal (see below) |
| Match | Smallest distance to that student's covered templates under `COVERED_THRESHOLD` (0.40, stricter than 0.5) |
| Result | Granted entries are **always flagged for warden review** (method "Eye-Region"). A denial is logged with the typed number but never linked to a student, and counts towards security alerts |
| Unknown number / inactive / not enrolled | All look the same at the terminal ("ask the warden") |

What we measured while building it (simulated niqab, one public portrait; **not** an accuracy figure):

* dlib's landmarks on a covered face are unusable for the blink: a painted-shut eye still read as open (EAR 0.41 against 0.43), and
  the encoding of the same picture moved by **0.42** when it was merely re-saved at another JPEG quality. So the face is aligned by
  hand from YuNet's eye positions and given straight to the recognition network (`covered_encoding`): the same JPEG change now
  moves it by about 0.06, a lighting change by 0.07 to 0.17, a 15 % smaller picture by 0.34.
* The blink on a covered face is found by comparing the eye band with the burst's median band (a blink is a short run of clearly
  different frames with unchanged frames before and after). A still picture never changes; a head that moves and does not come back
  is too long to count.

**Not measured, and must be before this is trusted:** how often a different real person with a similar eye area passes (the 91-photo
test above gave about 10 % per 1:1 comparison at a threshold of 0.45 with 14 % of real people refused), how real niqab cloth and
real blinks behave, and whether `COVERED_BLINK_MIN_CHANGE` (7 grey levels) and `COVERED_THRESHOLD` (0.40) suit the gate camera. Until
then this is a convenience with warden review, not an identity guarantee. Real iris recognition needs a near-infrared iris camera;
a webcam cannot do it.

Conclusion: eye-region comparison with the specified stack is **not reliable enough to identify a person**, and the
"covered face" case could not be made to work. Therefore:

* `EYE_FALLBACK_ENABLED` is **0 (off)** by default. A face that does not match is simply denied.
* If switched on, it can only **confirm a borderline face match for the same student** (face distance between 0.5 and
  0.6, eye templates agree with a strict threshold and clear margin). It never identifies anyone by itself, it never
  runs without the blink check passing, and every such entry is flagged for warden review.
* The SRS targets (75 percent eye-region accuracy with false acceptance under 5 percent) were not reached and are not
  claimed. This should be stated in the report as a finding, with the limits above.

## 4. What happens to each outcome

| Outcome | Result | Logged as |
|---|---|---|
| Blink seen, face matches one student clearly | Access granted | Granted, student, method Face |
| No blink | Denied | Denied, no student, `liveness_failed` |
| Face not in the enrolled set | Denied | Denied, no student, `not_recognized` |
| Two students match equally | Denied | Denied, no student, `ambiguous` |
| Face covered (first pass) | Terminal asks for the registration number | Denied, `face_covered` (not flagged, not counted) |
| Covered, number typed, eyes match that student, blink seen | Access granted, **flagged for warden review** | Granted, student, method Eye-Region |
| Covered, eyes do not match / unknown number / not enrolled covered | Denied, "ask the warden" | Denied, no student, the typed number in the reason |
| No face / several faces / too dark / blurry / too far | Denied | Denied, no student, matching code |
| Camera missing, blocked or busy | Message on the terminal | Not logged (no attempt reached the server) |

A denied attempt is never attributed to a student, even when the face looked like one: a photograph of student X must
not be recorded as "X failed".

## 5. What still needs a real person and a real webcam

1. Does a normal blink in front of the laptop camera reliably pass (with glasses, with different people)?
2. Are the brightness and sharpness limits right for this camera in the hostel's lighting?
3. Accuracy over many students: false accepts and false rejects (SRS NFR-02).
4. Time from the end of capture to the decision on the gate computer.

## 6. Guests

* A guest is matched with the **same threshold and blink check** as a student, but **never with the eye-region fallback**.
* A face that matches both a student and a guest (or two guests about equally) is denied as inconclusive.
* Enrollment refuses to register a guest whose face matches a student, so a guest cannot inherit student access.
* A recognised guest whose visit rules fail (not approved, expired, already used, host inactive) is refused and the attempt is
  logged against that guest, because recognition and the blink check both succeeded. A failed blink or an unknown face is never
  attributed to anyone.
* Not yet tested with real people: how well two different real guests are separated, and how a guest's face holds up against
  lighting differences between registration and arrival.
