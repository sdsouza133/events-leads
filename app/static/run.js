// While a run works, check its status every few seconds. Tick the steps off as they finish (each new one is read
// out to screen readers), keep the search's counters and progress bar current, and reload the page once the run
// moves on: to its plan, its list, or a stop.
(function () {
  "use strict";
  var el = document.getElementById("run");
  if (!el) {
    return;
  }
  var url = el.getAttribute("data-status-url");
  var phase = el.getAttribute("data-phase");
  var note = document.getElementById("run-note");
  var estimate = document.getElementById("run-estimate");
  var live = document.getElementById("run-step");
  var states = { done: "Done", active: "In progress", pending: "Up next" };
  var active = el.querySelector(".step.is-active");
  var current = active ? Number(active.getAttribute("data-step")) : -1;
  var every = 5000;

  function tick(step) {
    var items = el.querySelectorAll(".step");
    for (var i = 0; i < items.length; i++) {
      var state = i < step ? "done" : i === step ? "active" : "pending";
      items[i].className = "step is-" + state;
      items[i].querySelector(".step-state").textContent = states[state];
    }
    if (step !== current && items[step]) {
      live.textContent = "Now: " + items[step].querySelector(".step-label").textContent;
    }
    current = step;
  }

  function count(id, value) {
    var counter = document.getElementById(id);
    if (counter) {
      counter.textContent = value.toLocaleString("en-US");
    }
  }

  function show(body) {
    if (!body.progress || body.progress.phase !== phase) {
      window.location.reload(); // the run moved on: the page shows its new stage
      return;
    }
    tick(body.progress.step);
    if (note && note.textContent !== body.progress.note) {
      note.textContent = body.progress.note;
    }
    if (estimate) {
      estimate.hidden = Boolean(body.progress.note); // waiting for capacity: no promise of minutes
    }
    if (body.found) {
      count("found-posts", body.found.posts);
      count("found-people", body.found.people);
      count("found-emails", body.found.emails);
      var bar = document.getElementById("found-percent");
      if (bar) {
        bar.value = body.found.percent;
        bar.textContent = body.found.percent + "%";
      }
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
