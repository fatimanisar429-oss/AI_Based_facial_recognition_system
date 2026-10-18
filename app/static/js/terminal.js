// Entry terminal. Plain JavaScript, no libraries.
//
// One attempt:
//   1. "Start verification" switches the camera on (the browser may ask for permission).
//   2. A short burst of still pictures is captured (about 30 frames in 3.3 s) while the person blinks.
//   3. The camera is switched OFF straight away. Nothing is recorded as video and nothing is kept in the browser.
//   4. The pictures are sent to the server. THE SERVER decides and writes the entry log; this page only shows
//      the answer, so a tampered page cannot grant access.
//   5. After a few seconds the screen resets for the next person.
//
// Covered face (niqab): if the server answers "face_covered", the page asks for the registration number and sends the SAME
// pictures again with that number (the server then compares the eyes with that one student only). The pictures stay in this
// page's memory only until the answer arrives or the screen resets; nothing is stored.
(function () {
  "use strict";

  var root = document.getElementById("terminal");
  if (!root) return;

  var cfg = {
    url: root.dataset.verifyUrl,
    csrf: root.dataset.csrf,
    frames: parseInt(root.dataset.frames, 10),
    interval: parseInt(root.dataset.interval, 10),
    disabled: root.dataset.disabled === "1"
  };
  var MAX_SIDE = 640, JPEG_QUALITY = 0.8, SETTLE_MS = 800, RESET_MS = 9000;

  var $ = function (id) { return document.getElementById(id); };
  var el = {
    video: $("cam-video"), box: $("cam-box"), placeholder: $("cam-placeholder"), overlay: $("cam-overlay"),
    overlayText: $("overlay-text"), bar: $("capture-bar"), start: $("btn-start"), card: $("result-card"),
    title: $("result-title"), name: $("result-name"), detail: $("result-detail"), time: $("result-time"),
    chipCamera: $("chip-camera"), chipFace: $("chip-face"), chipLive: $("chip-liveness"),
    chipRecog: $("chip-recognition"), chipMethod: $("chip-method"),
    idPanel: $("id-form"), idForm: $("id-form"), idInput: $("id-input"), idCancel: $("id-cancel"), idSubmit: $("id-submit")
  };

  var stream = null, busy = false, resetTimer = null, pendingFrames = null, lastFrames = null;

  // ---------- clock ----------
  function tick() {
    var now = new Date();
    $("clock-date").textContent = now.toLocaleDateString(undefined, { weekday: "short", day: "2-digit", month: "short", year: "numeric" });
    $("clock-time").textContent = now.toLocaleTimeString();
  }
  tick();
  setInterval(tick, 1000);

  // ---------- small helpers ----------
  function chip(node, text, kind) {
    node.textContent = text;
    node.className = "pill pill-" + kind;
  }

  function showResult(state, title, name, detail, time) {
    el.card.dataset.result = state;                 // idle | working | granted | denied
    el.title.textContent = title;
    el.name.textContent = name || "";
    el.detail.textContent = detail || "";
    el.time.textContent = time || "";
  }

  function hideIdPanel() {
    pendingFrames = null;                          // the pictures are forgotten as soon as they are not needed
    el.idPanel.hidden = true;
    el.idInput.value = "";
  }

  function resetScreen() {
    clearTimeout(resetTimer);
    hideIdPanel();
    showResult("idle", "Ready", "", "Waiting for the next person.", "");
    chip(el.chipCamera, "Off", "muted");
    chip(el.chipFace, "Waiting", "muted");
    chip(el.chipLive, "Waiting", "muted");
    chip(el.chipRecog, "Waiting", "muted");
    chip(el.chipMethod, "—", "muted");
    el.overlay.hidden = true;
    el.bar.style.width = "0%";
    el.start.disabled = cfg.disabled;
  }

  function sleep(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

  // ---------- camera ----------
  function cameraError(err) {
    switch (err && err.name) {
      case "NotAllowedError":
      case "SecurityError":
        return "Camera permission was denied. Allow camera access for this site in the browser, then try again.";
      case "NotFoundError":
      case "OverconstrainedError":
        return "No camera was found. Check that the camera is connected.";
      case "NotReadableError":
      case "AbortError":
        return "The camera is being used by another program. Close it and try again.";
      default:
        return "The camera could not be started.";
    }
  }

  function openCamera() {
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return Promise.reject({ name: "Insecure", message: "Open this page at http://localhost:5000 (or over HTTPS) on the gate computer." });
    }
    return navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false })
      .then(function (s) {
        stream = s;
        el.video.srcObject = s;
        return el.video.play();
      });
  }

  function closeCamera() {
    if (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
    }
    el.video.srcObject = null;
    el.box.classList.remove("is-live");
    el.placeholder.hidden = false;
    chip(el.chipCamera, "Off", "muted");
  }

  // ---------- capture ----------
  function grabFrame() {
    return new Promise(function (resolve, reject) {
      var w = el.video.videoWidth, h = el.video.videoHeight;
      if (!w || !h) { reject(new Error("camera not ready")); return; }
      var scale = Math.min(1, MAX_SIDE / Math.max(w, h));
      var canvas = document.createElement("canvas");
      canvas.width = Math.round(w * scale);
      canvas.height = Math.round(h * scale);
      canvas.getContext("2d").drawImage(el.video, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(function (blob) { blob ? resolve(blob) : reject(new Error("capture failed")); }, "image/jpeg", JPEG_QUALITY);
    });
  }

  function captureBurst() {
    var frames = [];
    var startedAt = performance.now();
    function next(i) {
      if (i >= cfg.frames) return Promise.resolve(frames);
      var due = startedAt + i * cfg.interval;
      return sleep(Math.max(0, due - performance.now()))
        .then(grabFrame)
        .then(function (blob) {
          frames.push(blob);
          el.bar.style.width = Math.round(((i + 1) / cfg.frames) * 100) + "%";
          return next(i + 1);
        });
    }
    return next(0);
  }

  // ---------- server answer ----------
  function checkWord(value, good, bad) {
    return value === "ok" ? [good, "ok"] : value === "failed" ? [bad, "bad"] : ["Not checked", "muted"];
  }

  function showAnswer(data) {
    var f = checkWord(data.checks && data.checks.face, "Face found", "No usable face");
    var l = checkWord(data.checks && data.checks.liveness, "Blink detected", "No blink");
    var r = checkWord(data.checks && data.checks.recognition, "Matched", "Not matched");
    chip(el.chipFace, f[0], f[1]);
    chip(el.chipLive, l[0], l[1]);
    chip(el.chipRecog, r[0], r[1]);
    var guest = data.kind === "guest";
    if (data.granted) {
      chip(el.chipMethod, data.method, "info");
      if (guest) {
        showResult("granted", "GUEST ACCESS GRANTED", data.name, "Door unlocked. Welcome" + (data.valid_until ? " (visit valid until " + data.valid_until + ")" : "") + ".", data.time);
        return;
      }
      var a = data.attendance;
      var note = !a ? "" : a.first_entry ? " Attendance: " + a.status + " (first entry today)." : " Entry recorded; attendance was already marked today (" + a.status + ").";
      showResult("granted", "ACCESS GRANTED", data.name, "Door unlocked. Welcome." + note, data.time);
    } else {
      chip(el.chipMethod, "\u2014", "muted");
      showResult("denied", guest ? "GUEST ACCESS DENIED" : "ACCESS DENIED", "", data.message, data.time);
    }
  }

  function showFailure(message) {
    chip(el.chipFace, "Not checked", "muted");
    chip(el.chipLive, "Not checked", "muted");
    chip(el.chipRecog, "Not checked", "muted");
    showResult("denied", "ACCESS DENIED", "", message, "");
  }

  function send(frames, registrationNo) {
    var form = new FormData();
    frames.forEach(function (blob, i) { form.append("frames", blob, "frame-" + i + ".jpg"); });
    if (registrationNo) form.append("registration_no", registrationNo);
    return fetch(cfg.url, { method: "POST", headers: { "X-CSRF-Token": cfg.csrf }, body: form, credentials: "same-origin" });
  }

  function readAnswer(res) {
    return res.json().then(function (data) { return { status: res.status, data: data }; },
      function () { return { status: res.status, data: null }; });
  }

  function handleOutcome(out, frames) {
    if (out.data && out.data.ok && out.data.code === "face_covered") { askForId(frames, out.data); return; }
    hideIdPanel();
    if (out.data && out.data.ok) { showAnswer(out.data); return; }
    if (out.data && out.data.message) { showFailure(out.data.message); return; }
    showFailure(out.status === 403
      ? "This terminal can only be used on the gate computer."
      : "The server gave an unexpected answer. Please try again.");
  }

  // ---------- covered face: ask for the registration number ----------
  function askForId(frames, data) {
    pendingFrames = frames;
    showAnswer(data);
    showResult("working", "FACE COVERED", "", data.message, data.time);
    el.idPanel.hidden = false;
    el.idInput.focus();
    clearTimeout(resetTimer);
    resetTimer = setTimeout(resetScreen, 60000);          // give the person a minute to type
  }

  function submitId(event) {
    event.preventDefault();
    var regNo = el.idInput.value.trim();
    if (!regNo || !pendingFrames || busy) return;
    var frames = pendingFrames;
    busy = true;
    el.idSubmit.disabled = true;
    clearTimeout(resetTimer);
    showResult("working", "Checking...", "", "Please wait.", "");
    chip(el.chipRecog, "Checking", "warn");
    send(frames, regNo)
      .then(readAnswer)
      .then(function (out) { el.idPanel.hidden = true; handleOutcome(out, frames); })
      .catch(function () { hideIdPanel(); showFailure("Verification could not be completed. Check the connection, then try again."); })
      .then(function () {
        busy = false;
        el.idSubmit.disabled = false;
        el.start.disabled = cfg.disabled;
        resetTimer = setTimeout(resetScreen, RESET_MS);
      });
  }

  // ---------- one verification ----------
  function run() {
    if (busy || cfg.disabled) return;
    busy = true;
    clearTimeout(resetTimer);
    resetScreen();
    el.start.disabled = true;
    showResult("working", "Starting camera...", "", "Allow camera access if the browser asks.", "");
    chip(el.chipCamera, "Starting", "warn");

    openCamera()
      .then(function () {
        el.box.classList.add("is-live");
        el.placeholder.hidden = true;
        chip(el.chipCamera, "On", "ok");
        el.overlay.hidden = false;
        el.overlayText.textContent = "Look at the camera and blink twice";
        showResult("working", "Look at the camera", "", "Blink twice while the pictures are taken.", "");
        return sleep(SETTLE_MS);
      })
      .then(captureBurst)
      .then(function (frames) {
        lastFrames = frames;
        closeCamera();                                   // the camera is no longer needed
        el.overlay.hidden = true;
        showResult("working", "Checking...", "", "Please wait.", "");
        chip(el.chipFace, "Checking", "warn");
        chip(el.chipLive, "Checking", "warn");
        chip(el.chipRecog, "Checking", "warn");
        return send(frames, "");
      })
      .then(readAnswer)
      .then(function (out) { handleOutcome(out, lastFrames); })
      .catch(function (err) {
        closeCamera();
        el.overlay.hidden = true;
        chip(el.chipCamera, "Problem", "bad");
        if (err && err.name === "Insecure") showFailure(err.message);
        else if (err && err.name && /Error$/.test(err.name) && err.name !== "TypeError") showFailure(cameraError(err));
        else showFailure("Verification could not be completed. Check the camera and the connection, then try again.");
      })
      .then(function () {
        busy = false;
        lastFrames = null;
        el.start.disabled = cfg.disabled;
        if (!pendingFrames) resetTimer = setTimeout(resetScreen, RESET_MS);
      });
  }

  el.start.addEventListener("click", run);
  el.idForm.addEventListener("submit", submitId);
  el.idCancel.addEventListener("click", resetScreen);
  window.addEventListener("pagehide", closeCamera);       // never leave the camera on
  resetScreen();
  if (cfg.disabled) showResult("denied", "Unavailable", "", "The terminal is not configured. Contact the administrator.", "");
})();
