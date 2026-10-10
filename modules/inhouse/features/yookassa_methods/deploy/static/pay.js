(function () {
  "use strict";
  var LATE_MS = 4000; // show the manual button if the payment page has not opened by then
  var form = document.getElementById("pay");
  var field = document.getElementById("t");
  var late = document.getElementById("late");
  var status = document.getElementById("status");
  if (!form || !field) { return; }
  var prefix = "/p/";
  if (field.value.indexOf(prefix) !== 0) {
    // Caddy templates did not fill the field: take the label from the address.
    field.value = window.location.pathname;
  }
  function showButton(text) {
    if (status && text) { status.textContent = text; }
    if (late) { late.hidden = false; }
    document.body.classList.remove("loading-active");
  }
  if (field.value.indexOf(prefix) === 0 && field.value.length > prefix.length) {
    document.body.classList.add("loading-active");
    form.submit();
    var timer = window.setTimeout(function () {
      showButton("Оплата открывается дольше обычного. Если не открылась — нажмите кнопку.");
    }, LATE_MS);
    // Back from the payment page (page restored from cache): let the person open it again.
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) {
        window.clearTimeout(timer);
        showButton("Нажмите, чтобы снова открыть оплату.");
      }
    });
  } else {
    showButton("Ссылка неполная. Вернитесь в бот и откройте покупку заново.");
  }
})();
