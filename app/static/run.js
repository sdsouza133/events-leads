// While a run works, check its status every few seconds. Show the new detail as it changes (read out by screen
// readers through the aria-live region), and reload the page once the run moves to another stage.
(function () {
  "use strict";
  var el = document.getElementById("run");
  if (!el) {
    return;
  }
  var url = el.getAttribute("data-status-url");
  var status = el.getAttribute("data-status");
  var detail = document.getElementById("run-detail");
  var every = 5000;

  function check() {
    fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (body) {
        if (body && body.status !== status) {
          window.location.reload();
          return;
        }
        if (body && detail && detail.textContent !== body.detail) {
          detail.textContent = body.detail;
        }
        window.setTimeout(check, every);
      })
      .catch(function () {
        window.setTimeout(check, every); // offline for a moment: try again later
      });
  }

  window.setTimeout(check, every);
})();
