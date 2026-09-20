/* Panel chrome: the drawer on narrow screens, the toasts, and the count-up on
   stat tiles. Every page is complete without this file; it only adds motion and
   the small-screen navigation. No dependencies. */
(function () {
  "use strict";

  var reduced = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // --- side navigation as a drawer under 960px --------------------------------

  var toggle = document.getElementById("nav-toggle");
  var scrim = document.getElementById("nav-scrim");
  var sidenav = document.getElementById("sidenav");

  function setNav(open) {
    document.body.classList.toggle("nav-open", open);
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (scrim) scrim.hidden = !open;
  }

  if (toggle && sidenav) {
    toggle.addEventListener("click", function () {
      setNav(!document.body.classList.contains("nav-open"));
    });
    if (scrim) scrim.addEventListener("click", function () { setNav(false); });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") setNav(false);
    });
    // Following a link inside the drawer closes it with the navigation.
    sidenav.addEventListener("click", function (ev) {
      if (ev.target.closest("a")) setNav(false);
    });
  }

  // --- toasts: errors wait to be dismissed, confirmations leave on their own ---

  var box = document.getElementById("toasts");
  if (box) {
    box.addEventListener("click", function (ev) {
      var x = ev.target.closest(".toast-x");
      if (x) dismiss(x.parentElement);
    });
    box.querySelectorAll(".toast-ok").forEach(function (t) {
      setTimeout(function () { dismiss(t); }, 8000);
    });
  }

  function dismiss(toast) {
    if (!toast || toast.classList.contains("is-going")) return;
    if (reduced) { toast.remove(); return; }
    toast.classList.add("is-going");
    toast.addEventListener("animationend", function () { toast.remove(); });
  }

  // --- stat tiles count up to their value -------------------------------------
  // Only whole counters carry data-count; measurements are printed, never animated.

  if (!reduced && !document.hidden) {
    document.querySelectorAll("[data-count]").forEach(function (el) {
      var target = Number(el.getAttribute("data-count"));
      if (!isFinite(target) || target <= 0) return;
      var started = null;
      var span = Math.min(700, 220 + target * 28);
      function step(now) {
        if (started === null) started = now;
        var k = Math.min(1, (now - started) / span);
        el.textContent = String(Math.round(target * (1 - Math.pow(1 - k, 3))));
        if (k < 1) requestAnimationFrame(step);
      }
      el.textContent = "0";
      requestAnimationFrame(step);
      // A frame callback that never fires (a hidden or throttled tab) must not
      // leave a zero on screen where the real count belongs.
      setTimeout(function () { el.textContent = String(target); }, span + 500);
    });
  }
})();
