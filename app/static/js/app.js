// Small helpers shared by every page (vanilla JavaScript, no libraries besides Bootstrap).

// Show/hide password: any button with data-toggle-password="<input id>"
document.querySelectorAll("[data-toggle-password]").forEach(function (button) {
  var input = document.getElementById(button.dataset.togglePassword);
  if (!input) return;
  button.addEventListener("click", function () {
    var show = input.type === "password";
    input.type = show ? "text" : "password";
    button.textContent = show ? "Hide" : "Show";
    button.setAttribute("aria-pressed", show ? "true" : "false");
  });
});

// Confirmation dialog: <form data-confirm="Are you sure?" data-confirm-button="Deactivate"> asks first.
// The server does not depend on this; it only protects people from clicking by mistake.
(function () {
  var modalEl = document.getElementById("confirmModal");
  if (!modalEl || !window.bootstrap) return;
  var modal = new bootstrap.Modal(modalEl);
  var body = document.getElementById("confirmBody");
  var okButton = document.getElementById("confirmOk");
  var pendingForm = null;

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (form.dataset.confirmed === "1") return;   // already confirmed: let it go through
      event.preventDefault();
      pendingForm = form;
      body.textContent = form.dataset.confirm;
      okButton.textContent = form.dataset.confirmButton || "Confirm";
      modal.show();
    });
  });

  okButton.addEventListener("click", function () {
    if (!pendingForm) return;
    pendingForm.dataset.confirmed = "1";
    okButton.disabled = true;
    pendingForm.submit();
  });
  modalEl.addEventListener("hidden.bs.modal", function () { pendingForm = null; });
})();

// Stop double submits: a form's submit button is disabled once it has been sent.
document.querySelectorAll("form[data-once]").forEach(function (form) {
  form.addEventListener("submit", function () {
    var button = form.querySelector("[type=submit]");
    if (button) {
      button.disabled = true;
      button.dataset.originalText = button.textContent;
      button.textContent = button.dataset.loadingText || "Please wait...";
    }
  });
});
