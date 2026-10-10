(function () {
  "use strict";
  var form = document.getElementById("pay");
  var field = document.getElementById("t");
  if (!form || !field) { return; }
  var prefix = "/p/";
  if (field.value.indexOf(prefix) !== 0) {
    // Caddy templates did not fill the field: take the label from the address.
    field.value = window.location.pathname;
  }
  if (field.value.indexOf(prefix) === 0 && field.value.length > prefix.length) {
    var button = form.querySelector("button");
    if (button) { button.disabled = true; }
    form.submit();
    // If the browser comes back to this page (Back button), let the person press again.
    window.addEventListener("pageshow", function () { if (button) { button.disabled = false; } });
  }
})();
