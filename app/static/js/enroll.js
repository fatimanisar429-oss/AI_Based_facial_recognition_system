// Face enrollment page. Plain JavaScript, no libraries.
//
// What happens here
//   1. "Start camera" asks the browser for the built-in camera (only then, never automatically).
//   2. "Capture sample" grabs ONE still picture from the live video and sends it to the server for a quality
//      check. If it passes, the picture is kept in this page's memory only (not uploaded for storage).
//   3. After all samples are good, "Save enrollment" sends them together; the server turns them into numeric
//      templates and discards the pictures.
//   4. The camera is switched off after saving, on "Stop camera", and whenever the page is left.
(function () {
  "use strict";

  var root = document.getElementById("enroll");
  if (!root) return;

  var cfg = {
    checkUrl: root.dataset.checkUrl,
    saveUrl: root.dataset.saveUrl,
    csrf: root.dataset.csrf,
    samples: parseInt(root.dataset.samples, 10),
    replace: root.dataset.replace === "1",
    disabled: root.dataset.disabled === "1"
  };
  var MAX_SIDE = 960;       // pictures are shrunk to this before upload
  var JPEG_QUALITY = 0.9;

  var el = {
    video: document.getElementById("cam-video"),
    box: document.getElementById("cam-box"),
    placeholder: document.getElementById("cam-placeholder"),
    state: document.getElementById("cam-state"),
    consent: document.getElementById("consent"),
    start: document.getElementById("btn-start"),
    capture: document.getElementById("btn-capture"),
    stop: document.getElementById("btn-stop"),
    save: document.getElementById("btn-save"),
    camMsg: document.getElementById("cam-message"),
    saveMsg: document.getElementById("save-message"),
    count: document.getElementById("sample-count"),
    slots: Array.prototype.slice.call(document.querySelectorAll(".sample-slot"))
  };

  var stream = null;
  var busy = false;
  var target = 0;                              // slot that the next capture will fill
  var blobs = new Array(cfg.samples).fill(null);
  var previews = new Array(cfg.samples).fill(null);

  // ---------- small helpers ----------

  function say(node, text, kind) {
    node.textContent = text;
    node.className = "msg-line" + (kind ? " msg-" + kind : "");
  }

  function setState(text, kind) {
    el.state.textContent = text;
    el.state.className = "pill pill-" + kind;
    root.dataset.camera = kind === "ok" ? "on" : "off";
  }

  function filledCount() {
    return blobs.filter(Boolean).length;
  }

  function firstEmpty() {
    var i = blobs.indexOf(null);
    return i === -1 ? Math.min(target, cfg.samples - 1) : i;
  }

  function refresh() {
    var on = !!stream;
    var consented = el.consent.checked;
    // The camera preview starts by itself. Consent is only needed to CAPTURE and SAVE pictures.
    el.start.disabled = cfg.disabled || busy || on;
    el.capture.disabled = cfg.disabled || busy || !on || !consented;
    el.stop.disabled = !on || busy;
    el.save.disabled = cfg.disabled || busy || !consented || filledCount() !== cfg.samples;
    el.count.textContent = filledCount();
    el.box.classList.toggle("is-live", on);
    el.placeholder.hidden = on;
    el.slots.forEach(function (slot, i) {
      slot.classList.toggle("filled", !!blobs[i]);
      slot.classList.toggle("current", i === target);
      var img = slot.querySelector(".slot-img");
      var num = slot.querySelector(".slot-empty");
      img.hidden = !previews[i];
      num.hidden = !!previews[i];
      if (previews[i]) img.src = previews[i];
    });
  }

  function post(url, formData) {
    return fetch(url, {
      method: "POST",
      headers: { "X-CSRF-Token": cfg.csrf },
      body: formData,
      credentials: "same-origin"
    }).then(function (res) {
      return res.json().then(function (data) { return { status: res.status, data: data }; },
        function () { return { status: res.status, data: null }; });
    });
  }

  function unreadable(result) {
    if (result.status === 413) return "The picture was too large to upload. Try again.";
    if (result.status === 400 || result.status === 403) return "The request was rejected. Reload the page and try again.";
    return "Unexpected answer from the server. Your session may have expired; reload the page and log in again.";
  }

  // ---------- camera ----------

  var EMBEDDED_HINT = " If this page is open inside VS Code or another preview window, open http://127.0.0.1:5000 in Edge or Chrome instead.";

  function cameraErrorMessage(err) {
    switch (err && err.name) {
      case "NotAllowedError":
      case "SecurityError":
        return "Camera permission was denied. Click the camera icon in the address bar, allow access, then press Start camera again." + EMBEDDED_HINT;
      case "NotFoundError":
      case "OverconstrainedError":
        return "No camera was found on this device. Check that it is connected and not disabled, then try again.";
      case "NotReadableError":
      case "AbortError":
        return "The camera could not be opened. Another program may be using it; close it and try again.";
      default:
        return "The camera could not be started (" + ((err && err.name) || "unknown error") + ")." + EMBEDDED_HINT;
    }
  }

  function startCamera() {
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setState("Unavailable", "bad");
      say(el.camMsg, "This page cannot use the camera. Open the site at http://localhost:5000 (or over HTTPS) in Chrome, Edge or Firefox." + EMBEDDED_HINT, "error");
      return;
    }
    busy = true;
    refresh();
    setState("Starting...", "warn");
    say(el.camMsg, "Waiting for camera permission...");
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false })
      .then(function (s) {
        stream = s;
        el.video.srcObject = s;
        s.getVideoTracks().forEach(function (track) {
          track.addEventListener("ended", function () {   // camera unplugged or taken away
            stopCamera();
            setState("Camera lost", "bad");
            say(el.camMsg, "The camera stopped working. Reconnect it and press Start camera.", "error");
          });
        });
        return el.video.play();
      })
      .then(function () {
        setState("Camera on", "ok");
        say(el.camMsg, el.consent.checked
          ? "Camera ready. Ask the student to look at the camera, then press Capture sample."
          : "Camera is on. Tick the consent box below to enable Capture sample.", "ok");
      })
      .catch(function (err) {
        stopCamera();
        setState(err && (err.name === "NotAllowedError" || err.name === "SecurityError") ? "Permission denied" : "Camera error", "bad");
        say(el.camMsg, cameraErrorMessage(err), "error");
      })
      .then(function () { busy = false; refresh(); });
  }

  function stopCamera() {
    if (stream) {
      stream.getTracks().forEach(function (track) { track.stop(); });
      stream = null;
    }
    el.video.srcObject = null;
    setState("Camera off", "muted");
    refresh();
  }

  // ---------- capturing ----------

  function grabFrame() {
    return new Promise(function (resolve, reject) {
      var w = el.video.videoWidth, h = el.video.videoHeight;
      if (!w || !h) { reject(new Error("The camera is not ready yet. Wait a second and try again.")); return; }
      var scale = Math.min(1, MAX_SIDE / Math.max(w, h));
      var canvas = document.createElement("canvas");
      canvas.width = Math.round(w * scale);
      canvas.height = Math.round(h * scale);
      canvas.getContext("2d").drawImage(el.video, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(function (blob) {
        blob ? resolve(blob) : reject(new Error("The picture could not be captured."));
      }, "image/jpeg", JPEG_QUALITY);
    });
  }

  function capture() {
    if (busy || !stream) return;
    var index = target;
    busy = true;
    refresh();
    say(el.camMsg, "Checking the picture...");
    grabFrame()
      .then(function (blob) {
        var form = new FormData();
        form.append("image", blob, "sample.jpg");
        return post(cfg.checkUrl, form).then(function (result) { return { blob: blob, result: result }; });
      })
      .then(function (out) {
        var data = out.result.data;
        if (!data) { say(el.camMsg, unreadable(out.result), "error"); return; }
        if (!data.ok) { say(el.camMsg, data.message, "error"); return; }
        if (previews[index]) URL.revokeObjectURL(previews[index]);
        blobs[index] = out.blob;
        previews[index] = URL.createObjectURL(out.blob);
        target = firstEmpty();
        var m = data.metrics || {};
        say(el.camMsg, "Sample " + (index + 1) + " accepted (brightness " + m.brightness + ", sharpness " + m.sharpness + ")." +
          (filledCount() === cfg.samples ? " All samples captured: press Save enrollment." : " Next: " + slotPrompt(target) + "."), "ok");
      })
      .catch(function (err) { say(el.camMsg, err.message || "Capture failed. Please try again.", "error"); })
      .then(function () { busy = false; refresh(); });
  }

  function slotPrompt(index) {
    var p = el.slots[index] && el.slots[index].querySelector(".slot-prompt");
    return p ? p.textContent.toLowerCase() : "";
  }

  // ---------- saving ----------

  function save() {
    if (busy || filledCount() !== cfg.samples) return;
    busy = true;
    refresh();
    say(el.saveMsg, "Processing " + cfg.samples + " samples. This can take up to a minute, please wait...");
    var form = new FormData();
    blobs.forEach(function (blob, i) { form.append("images", blob, "sample-" + (i + 1) + ".jpg"); });
    form.append("consent", el.consent.checked ? "1" : "0");
    if (cfg.replace) form.append("replace", "1");

    post(cfg.saveUrl, form)
      .then(function (result) {
        var data = result.data;
        if (!data) { say(el.saveMsg, unreadable(result), "error"); return; }
        if (data.ok) {
          say(el.saveMsg, data.message, "ok");
          stopCamera();
          clearSamples();
          window.location.href = data.redirect;
          return;
        }
        say(el.saveMsg, data.message, "error");
        if (data.sample) {                    // one specific sample was bad: let the admin retake just that one
          var i = data.sample - 1;
          if (previews[i]) URL.revokeObjectURL(previews[i]);
          blobs[i] = null;
          previews[i] = null;
          target = i;
        }
      })
      .catch(function () { say(el.saveMsg, "The server could not be reached. Check the connection and try again.", "error"); })
      .then(function () { busy = false; refresh(); });
  }

  function clearSamples() {
    previews.forEach(function (url) { if (url) URL.revokeObjectURL(url); });
    blobs = new Array(cfg.samples).fill(null);
    previews = new Array(cfg.samples).fill(null);
  }

  // ---------- wiring ----------

  el.consent.addEventListener("change", function () {
    if (stream && !busy) {
      say(el.camMsg, el.consent.checked ? "Camera ready. Ask the student to look at the camera, then press Capture sample."
        : "Consent removed: capturing is switched off.", el.consent.checked ? "ok" : "");
    }
    refresh();
  });
  el.start.addEventListener("click", startCamera);
  el.stop.addEventListener("click", function () { stopCamera(); say(el.camMsg, "Camera stopped."); });
  el.capture.addEventListener("click", capture);
  el.save.addEventListener("click", save);
  el.slots.forEach(function (slot, i) {
    slot.querySelector(".slot-button").addEventListener("click", function () {
      if (busy) return;
      target = i;
      if (blobs[i]) say(el.camMsg, "Retaking sample " + (i + 1) + ": capture a new picture to replace it.");
      refresh();
    });
  });

  // Never leave the camera running behind the user's back.
  window.addEventListener("pagehide", function () { stopCamera(); clearSamples(); });

  if (cfg.disabled) say(el.camMsg, "Enrollment is switched off until the biometric key is configured.", "error");
  setState("Camera off", "muted");
  refresh();
  // Opening this page IS the request to use the camera, so start the preview straight away
  // (the browser asks for permission once). Nothing is captured until Capture sample is pressed.
  if (!cfg.disabled) startCamera();
})();
