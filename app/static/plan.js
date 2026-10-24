// The plan page, livelier: at most a few events picked (picking one more says to unpick one first instead), and the
// counts follow each pick. The form works without this script too: the server checks the same limit.
(function () {
  "use strict";
  var form = document.getElementById("plan");
  if (!form) {
    return;
  }
  var max = Number(form.getAttribute("data-max-events")) || 3;
  var boxes = Array.prototype.slice.call(form.querySelectorAll("input.pick"));
  var count = document.getElementById("picked-count");
  var summary = document.getElementById("go-summary");
  var note = document.getElementById("pick-note");

  function picked() {
    return boxes.filter(function (box) {
      return box.checked;
    }).length;
  }

  function show() {
    var n = picked();
    count.textContent = n + " of " + max + " picked";
    summary.textContent = n ? n + (n === 1 ? " event" : " events") + " · about 10 minutes" : "No events picked yet";
  }

  boxes.forEach(function (box) {
    box.addEventListener("change", function () {
      if (box.checked && picked() > max) {
        box.checked = false;
        note.textContent = "You can pick up to " + max + ". Unpick one first.";
        return;
      }
      note.textContent = "";
      show();
    });
  });
})();
