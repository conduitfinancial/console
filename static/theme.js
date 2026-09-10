/* The theme, and nothing else.
 *
 * Its own file because of WHERE it has to run. `data-theme` decides which of
 * two palettes the page is painted in, so it must be on `<html>` before the
 * first box is drawn or a dark-mode operator opens every page on a white
 * flash — which means a BLOCKING `<script>` in `<head>`. CSP is
 * `script-src 'self'` with no `unsafe-inline` (app/main.py), so the one-line
 * inline bootstrap every other app uses is unavailable and always will be:
 * the choice is a file. This file is ~20 lines and costs ~1 KB. app.js is
 * ~40 KB and stays where it was, at the foot of the body, where blocking
 * nothing is worth more than sharing a file with this.
 *
 * The runtime handler for the control lives here too, not in app.js: one file
 * owns the theme end to end, and `apply()`/`stored()` are shared by both
 * halves. app.js does not mention the theme at all.
 *
 * Three states, and the third is the ABSENCE of the attribute: System removes
 * `data-theme` and lets the stylesheet's `@media (prefers-color-scheme: dark)`
 * branch decide, which is what makes "follow the machine" keep following it
 * rather than freezing whatever the machine said the day it was chosen.
 */
(function () {
  "use strict";

  var KEY = "conduit-console-theme";

  // localStorage throws rather than returning null in a browser that has
  // disabled site data, so both ends are guarded: a console that will not paint
  // because it could not read a preference is worse than one in the wrong
  // palette.
  function stored() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function apply(choice) {
    if (choice === "light" || choice === "dark") {
      document.documentElement.setAttribute("data-theme", choice);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  // Before first paint. Everything below waits for a DOM.
  apply(stored());

  // The control in the ribbon's env strip. Server-rendered with System checked,
  // because the server cannot know this browser's choice; this corrects it once
  // the DOM exists. Delegated to the group, so the three radios need no
  // per-input wiring. The ribbon is never htmx-swapped, so once is enough.
  document.addEventListener("DOMContentLoaded", function () {
    var group = document.getElementById("theme-choice");
    if (!group) return;
    var chosen = group.querySelector('input[value="' + (stored() || "system") + '"]');
    if (chosen) chosen.checked = true;
    group.addEventListener("change", function (event) {
      var choice = event.target.value;
      try {
        if (choice === "system") localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, choice);
      } catch (e) { /* see stored(): the choice still applies for this page */ }
      apply(choice);
    });
  });
})();
