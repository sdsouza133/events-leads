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

  function show(body) {
    if (body.status !== status) {
      window.location.reload(); // the run moved on: the page shows its new stage
      return;
    }
    if (detail && detail.textContent !== body.detail) {
      detail.textContent = body.detail;
    }
    window.setTimeout(check, every);
  }

  function check() {
    fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (response.status === 404) {
          window.location.reload(); // signed out meanwhile: the page says how to sign in again
          return undefined;
        }
        if (!response.ok) {
          window.setTimeout(check, every); // a hiccup on our side: try again later
          return undefined;
        }
        return response.json().then(show);
      })
      .catch(function () {
        window.setTimeout(check, every); // offline for a moment: try again later
      });
  }

  window.setTimeout(check, every);
})();
