// Live "Filter conversations" box for the msgbrowse sidebar. Self-hosted (served
// from /static under script-src 'self') so it runs under the strict CSP — no
// inline handlers. As the user types, it shows only the conversation rows whose
// (humanized) name contains the query, case-insensitively, and whose latest
// message date is within the optional From/To range, across BOTH the
// PINNED and CONVERSATIONS sections, reveals a small empty-state line when
// nothing matches, and hides the PINNED group header when none of its rows match
// (SPEC-0006 REQ-0006-003 / REQ-0006-010).
(function () {
  "use strict";

  // The live filter bindings persist across OOB row swaps
  // (only the lists are replaced), so every re-init targets the SAME input —
  // without detaching the previous handler first, each pin/import would stack
  // another listener closed over a stale row list. Held at module scope so
  // initFilter can unbind before rebinding; on htmx:historyRestore the input
  // element itself is replaced and the removeEventListener is a harmless no-op.
  var filterInputs = [];
  var filterHandler = null;

  function initFilter() {
    var input = document.getElementById("sidebar-filter");
    var fromInput = document.getElementById("sidebar-date-from");
    var toInput = document.getElementById("sidebar-date-to");
    if (!input) return;

    // Default the upper bound to today's local date once per DOM input. Keep
    // a user-cleared or edited value through subsequent sidebar row swaps.
    if (toInput && !toInput.dataset.defaultApplied) {
      if (!toInput.value) {
        var today = new Date();
        toInput.value = String(today.getFullYear()) + "-" +
          String(today.getMonth() + 1).padStart(2, "0") + "-" +
          String(today.getDate()).padStart(2, "0");
      }
      toInput.dataset.defaultApplied = "true";
    }

    // Idempotent re-init: drop the previous binding (and its captured rows)
    // before the early returns, so a re-init over an emptied list still
    // detaches the stale handler.
    if (filterHandler) {
      for (var bound = 0; bound < filterInputs.length; bound++) {
        filterInputs[bound].removeEventListener("input", filterHandler);
        filterInputs[bound].removeEventListener("change", filterHandler);
      }
      filterInputs = [];
      filterHandler = null;
    }

    // Every conversation row in either section carries .conv-item + data-name.
    var items = Array.prototype.slice.call(document.querySelectorAll(".conv-item"));
    if (!items.length) return;

    // Precompute the lowercased names once (SPEC-0008 REQ-0008-012): the
    // reference archive has 2,271 rows, so re-reading + re-lowercasing
    // data-name on every keystroke was pure waste. The parallel array is
    // rebuilt whenever initFilter re-runs (htmx:historyRestore re-init below
    // keeps it in sync with a restored DOM).
    var names = new Array(items.length);
    var dates = new Array(items.length);
    for (var i = 0; i < items.length; i++) {
      names[i] = (items[i].getAttribute("data-name") || "").toLowerCase();
      var time = items[i].querySelector(".conv-last-date");
      dates[i] = time ? time.getAttribute("datetime") || "" : "";
    }

    var empty = document.querySelector(".sidebar-empty");
    var pinnedUl = document.getElementById("sidebar-pinned");
    var pinnedHead = pinnedUl ? pinnedUl.previousElementSibling : null;

    function anyVisible(ul) {
      if (!ul) return false;
      var rows = ul.querySelectorAll(".conv-item");
      for (var i = 0; i < rows.length; i++) {
        if (!rows[i].hidden) return true;
      }
      return false;
    }

    // setHidden writes el.hidden only when the value actually changes
    // (REQ-0008-012): unconditional writes dirtied style/layout for every row
    // on every keystroke even when nothing moved.
    function setHidden(el, hidden) {
      if (el.hidden !== hidden) el.hidden = hidden;
    }

    function apply() {
      var q = input.value.trim().toLowerCase();
      var from = fromInput ? fromInput.value : "";
      var to = toInput ? toInput.value : "";
      var shown = 0;
      for (var i = 0; i < items.length; i++) {
        var match = (q === "" || names[i].indexOf(q) !== -1) &&
          (!from || (dates[i] && dates[i] >= from)) &&
          (!to || (dates[i] && dates[i] <= to));
        setHidden(items[i], !match);
        if (match) shown++;
      }
      if (empty) setHidden(empty, shown !== 0);
      // Drop the PINNED header + list entirely when nothing in it matches, so a
      // filtered-out section doesn't leave a dangling "Pinned" label.
      if (pinnedUl && pinnedHead) {
        var visible = anyVisible(pinnedUl);
        setHidden(pinnedUl, !visible);
        setHidden(pinnedHead, !visible);
      }
    }

    // Coalesce input events with requestAnimationFrame (REQ-0008-012): a fast
    // typist fires several input events per frame; only one trailing visibility
    // pass runs per frame, and apply() reads input.value at run time so the
    // pass always sees the latest text.
    var rafId = 0;
    function schedule() {
      if (rafId) return;
      rafId = requestAnimationFrame(function () {
        rafId = 0;
        apply();
      });
    }

    filterInputs = [input];
    if (fromInput) filterInputs.push(fromInput);
    if (toInput) filterInputs.push(toInput);
    for (var bound = 0; bound < filterInputs.length; bound++) {
      filterInputs[bound].addEventListener("input", schedule);
      filterInputs[bound].addEventListener("change", schedule);
    }
    filterHandler = schedule;
    apply();
  }

  // Keep the active-conversation highlight correct. A boosted navigation swaps
  // only #main-content, leaving the sidebar DOM (including the server-rendered
  // .conv-row-selected) untouched — so move the highlight to the row whose href
  // matches the current path. Prefix-matching keeps the highlight on
  // jump-to-context pages (/c/{id}/at/{mid}) whose path extends the row's href.
  // Runs on initial load, after every HTMX settle (boosted nav), and on
  // browser back/forward.
  function syncActive() {
    var path = window.location.pathname;
    var rows = document.querySelectorAll("a.conv-row");
    for (var i = 0; i < rows.length; i++) {
      var href = rows[i].getAttribute("href");
      rows[i].classList.toggle(
        "conv-row-selected",
        href === path || (href !== "/" && path.indexOf(href + "/") === 0)
      );
    }
  }

  function init() {
    initFilter();
    syncActive();
  }

  // After a boosted swap settles: re-sync the active row and close the mobile
  // drawer — the #nav-drawer checkbox lives outside #main-content, so unlike a
  // full page load nothing else resets it and the overlay would stay open.
  document.addEventListener("htmx:afterSettle", function () {
    syncActive();
    var drawer = document.getElementById("nav-drawer");
    if (drawer) drawer.checked = false;
  });
  window.addEventListener("popstate", syncActive);

  // htmx restores history snapshots by replacing body.innerHTML, which drops
  // the element-bound filter listener and stales the captured row list — fully
  // re-init against the restored DOM (also resets any snapshotted hidden rows,
  // since the input's typed value is not serialized into the snapshot).
  document.addEventListener("htmx:historyRestore", init);

  // A successful Setup Enable→import emits HX-Trigger: msgbrowse:imported and
  // out-of-band-swaps fresh conversation rows into the sidebar lists (#142). The
  // OOB swap replaces the row DOM, staling the captured filter row list and its
  // element-bound listener — re-init so the newly-imported conversations are
  // filterable and the active-row highlight is correct, making the payoff appear
  // without a manual navigation.
  document.body.addEventListener("msgbrowse:imported", init);

  // A pin/unpin toggle answers its boosted POST with the same out-of-band
  // sidebar-list swap (#176) — same row-list staleness as the Setup import
  // above, same fix.
  document.body.addEventListener("msgbrowse:pinned", init);

  // A failed boosted request swaps nothing (htmx's default for 4xx/5xx), which
  // would turn an error page into a dead click — fall back to full navigation
  // so the error stays visible.
  document.addEventListener("htmx:responseError", function (e) {
    var path = e.detail && e.detail.requestConfig && e.detail.requestConfig.path;
    if (path) window.location.href = path;
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
