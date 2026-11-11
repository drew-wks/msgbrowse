// Desktop conversation-list width. The sidebar lives in the persistent shell,
// so boosted page changes preserve its width; localStorage preserves reloads.
(function () {
  "use strict";

  var key = "msgbrowse:sidebar-width";
  var preferred = 320;
  var boundHandle = null;
  try {
    var saved = Number(localStorage.getItem(key));
    if (Number.isFinite(saved) && saved >= 240) preferred = saved;
  } catch (e) {
    // Private browsing can disable storage; resizing still works this session.
  }

  function bounds() {
    return { min: 240, max: Math.max(240, Math.min(640, window.innerWidth - 320)) };
  }

  function applyWidth() {
    var shell = document.querySelector(".app-shell");
    var handle = document.getElementById("sidebar-resize");
    if (!shell || !handle) return;
    var range = bounds();
    var width = Math.max(range.min, Math.min(range.max, preferred));
    shell.style.setProperty("--msgbrowse-sidebar-width", width + "px");
    handle.setAttribute("aria-valuemax", String(range.max));
    handle.setAttribute("aria-valuenow", String(width));
  }

  function saveWidth() {
    try {
      localStorage.setItem(key, String(preferred));
    } catch (e) {
      // Width remains active until the page closes.
    }
  }

  function init() {
    var handle = document.getElementById("sidebar-resize");
    if (!handle) return;
    applyWidth();
    if (handle === boundHandle) return;
    boundHandle = handle;
    var dragging = false;

    handle.addEventListener("pointerdown", function (event) {
      if (!window.matchMedia("(min-width: 48rem)").matches) return;
      dragging = true;
      handle.classList.add("dragging");
      handle.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    handle.addEventListener("pointermove", function (event) {
      if (!dragging) return;
      var shell = document.querySelector(".app-shell");
      if (!shell) return;
      var range = bounds();
      preferred = Math.max(range.min, Math.min(range.max,
        event.clientX - shell.getBoundingClientRect().left));
      applyWidth();
    });
    function finish() {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove("dragging");
      saveWidth();
    }
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", finish);
    handle.addEventListener("lostpointercapture", finish);
    handle.addEventListener("keydown", function (event) {
      var range = bounds();
      if (event.key === "ArrowLeft") preferred = Math.max(range.min, preferred - 16);
      else if (event.key === "ArrowRight") preferred = Math.min(range.max, preferred + 16);
      else if (event.key === "Home") preferred = range.min;
      else if (event.key === "End") preferred = range.max;
      else return;
      event.preventDefault();
      applyWidth();
      saveWidth();
    });
  }

  window.addEventListener("resize", applyWidth);
  document.addEventListener("htmx:historyRestore", init);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
