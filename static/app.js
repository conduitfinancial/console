/* Browser glue for the wizard (FORM_ENGINE_SPEC §4, §8).
 *
 * Three jobs, none of them decisions:
 *   1. collect the form's values and hand them to Conditions (conditions.js) so
 *      inactive fields are hidden and disabled — the server re-decides all of
 *      it in forms.active_fields, which is authoritative;
 *   2. upload a file and turn the answer into a chip;
 *   3. remove a chip, copy an IDV link.
 *
 * No build step, no framework. htmx does the rest.
 */
(function () {
  "use strict";

  // Mirrors forms.coerce (§3) closely enough for *display*: booleans from
  // radio/checkbox, numbers from number inputs, arrays from checkbox groups.
  // Anything subtler is the server's problem by design.
  function collect(scope, prefix) {
    var values = {};
    var inputs = scope.querySelectorAll("input[name], select[name], textarea[name]");
    for (var i = 0; i < inputs.length; i++) {
      var input = inputs[i];
      var name = input.name;
      if (name.indexOf(prefix) !== 0) continue;
      // A disabled control is not submitted, so the server will not see it —
      // and `forms.active_fields` will therefore judge its dependents against
      // an absent value. Reading it here anyway made a cascade one-way: turning
      // the gate back off hid the middle field but left the field *it* gates on
      // screen, still fed by a checkbox nothing would send.
      if (input.disabled) continue;
      var path = name.slice(prefix.length).split(".");
      if (input.type === "radio" || input.type === "checkbox") {
        if (!input.checked) continue;
      }
      var value = input.value;
      if (input.type === "checkbox" && input.value === "true") value = true;
      else if (input.type === "radio") value = value === "true" ? true : value === "false" ? false : value;
      else if (input.type === "number" && value !== "") value = Number(value);

      var node = values;
      for (var p = 0; p < path.length - 1; p++) {
        if (typeof node[path[p]] !== "object" || node[path[p]] === null) node[path[p]] = {};
        node = node[path[p]];
      }
      var key = path[path.length - 1];
      if (input.type === "checkbox" && input.value !== "true") {
        // enumArray: a checkbox group under one name.
        if (!Array.isArray(node[key])) node[key] = [];
        node[key].push(input.value);
      } else {
        node[key] = value;
      }
    }
    return values;
  }

  function pass(form) {
    var root = collect(form, "f.");
    // Scoped to this form, and falling back to the form itself: person cards
    // live outside `#root-fields`, so they keep being applied separately below
    // with their own person scope.
    window.Conditions.apply(form.querySelector("#root-fields") || form, root, null);
    var cards = form.querySelectorAll(".person-card");
    for (var i = 0; i < cards.length; i++) {
      var index = cards[i].getAttribute("data-person");
      window.Conditions.apply(cards[i], root, collect(cards[i], "p." + index + ".f."));
    }
  }

  // Everything one pass can change, as a string: hidden boxes plus the
  // disabled/required flags on their controls. Cheap enough to take twice per
  // round, and exact enough that "identical to last round" really does mean the
  // fixed point has been reached — an earlier version compared `disabled` only,
  // which would have stopped early on a pass that moved `required` alone.
  function shape(form) {
    var out = "";
    var boxes = form.querySelectorAll("[data-conditions], [data-required-when]");
    for (var i = 0; i < boxes.length; i++) out += boxes[i].hidden ? "h" : ".";
    var nodes = form.querySelectorAll("input, select, textarea");
    for (var j = 0; j < nodes.length; j++) {
      out += (nodes[j].disabled ? "d" : ".") + (nodes[j].required ? "r" : ".");
    }
    return out;
  }

  // Gates chain: `hasRegulatedActivities` gates `regulatedActivities`, which
  // gates `otherActivityDescription`. One pass decides every field from the
  // values as they are *now*, but disabling a control removes it from what the
  // form will submit — so the answer the middle gate provides changes underneath
  // the pass that read it, and the chain used to collapse only one link per
  // change event. Iterating to a fixed point matches `forms.active_fields`,
  // which sees the submitted body and therefore sees the whole chain at once.
  //
  // The bound is the number of conditional fields plus one, not a magic 5: one
  // round can only settle one link, so a chain N gates deep needs N rounds, and
  // a form cannot hold a chain longer than the fields it has. The +1 is the
  // round that confirms nothing moved. Bounded at all so that a snapshot whose
  // conditions are mutually circular cannot spin the page — and in practice the
  // early exit below ends it in two.
  //
  // Every form that renders engine fields, not just the wizard: the payout and
  // transfer screens render the same `m.field()` macro from the same discovery
  // metadata, and keying off `id="wizard"` left their conditions and their
  // `requiredWhen` gates dead on the page.
  function conditionalForms() {
    var found = [];
    var nodes = document.querySelectorAll("[data-conditions], [data-required-when]");
    for (var i = 0; i < nodes.length; i++) {
      var form = nodes[i].closest("form");
      if (form && found.indexOf(form) === -1) found.push(form);
    }
    return found;
  }

  function refresh() {
    if (window.Conditions) {
      var forms = conditionalForms();
      for (var f = 0; f < forms.length; f++) {
        var form = forms[f];
        var rounds = form.querySelectorAll("[data-conditions], [data-required-when]").length + 1;
        for (var round = 0; round < rounds; round++) {
          var before = shape(form);
          pass(form);
          if (shape(form) === before) break;
        }
      }
    }
    // After the fixed point, never inside it: the counts below read `hidden` and
    // `required` as conditions.js has finally settled them.
    progress();
  }

  // What one section still owes: required, still on screen, unanswered. Counted
  // per `.field` box rather than per control, so a yes/no radio pair and an
  // enumArray checkbox group are each one answer, not two or nine.
  //
  // Required means either the base flag the server rendered (`data-req`) or the
  // live `required` conditions.js has since set — a `requiredWhen` gate that was
  // just answered carries only the second. Neither is re-derived here: the
  // condition logic stays in conditions.js and, authoritatively, in forms.py.
  function remaining(section) {
    var left = 0;
    var boxes = section.querySelectorAll(".field");
    for (var i = 0; i < boxes.length; i++) {
      var box = boxes[i];
      // Named controls only. A control with no `name` submits nothing, so it
      // cannot be an answer — which is what lets the country filter below live
      // *inside* its field box without a typed search term reading as the group
      // having been answered.
      var inputs = box.querySelectorAll("input[name], select[name], textarea[name]");
      // Disabled ≡ hidden here: conditions.js only ever sets the two together,
      // and the server drops exactly what the browser will not send.
      if (box.hidden || !inputs.length || inputs[0].disabled) continue;
      var required = box.hasAttribute("data-req");
      var filled = false;
      for (var j = 0; j < inputs.length; j++) {
        var input = inputs[j];
        if (input.required) required = true;
        if (input.type === "radio" || input.type === "checkbox"
          ? input.checked
          : String(input.value).trim() !== "") filled = true;
      }
      if (required && !filled) left++;
    }
    // The documents floor is a requirement with no field behind it, so counting
    // boxes alone would call the section done with nothing uploaded.
    var floor = Number(section.getAttribute("data-min-docs") || 0);
    if (floor) left += Math.max(0, floor - section.querySelectorAll('input[name="documentIds"]').length);
    return left;
  }

  // Wizard-only, keyed off the nav element: the payout and transfer screens
  // render the same fields through the same macro and must stay untouched.
  function progress() {
    var nav = document.getElementById("wizard-nav");
    if (!nav) return;
    var total = 0;
    var badges = nav.querySelectorAll("[data-count-for]");
    for (var i = 0; i < badges.length; i++) {
      var section = document.getElementById(badges[i].getAttribute("data-count-for"));
      var left = section ? remaining(section) : 0;
      total += left;
      // A finished section is quiet, not celebrated — colour means money state.
      badges[i].textContent = left ? String(left) : "✓";
      badges[i].className = left ? "count" : "count done";
      badges[i].title = left ? left + " required field(s) left" : "nothing left here";
    }
    var out = document.getElementById("wizard-total");
    if (out) {
      out.textContent = total
        ? total + " required field" + (total === 1 ? "" : "s") + " left"
        : "Every required field is answered";
    }
  }

  // ---- Progressive disclosure (QA F-001) ------------------------------------
  //
  // One section on screen at a time, with the existing sticky nav as the
  // stepper. What does NOT change is everything the rest of the page depends on:
  // every section stays in the DOM, enabled and named, so the autosave still
  // posts `#wizard` whole, conditions.js still walks the whole form, and the
  // counts above still read global state. Hiding a step has to be invisible to
  // all three.
  //
  // The mechanism is a CLASS (`.step-off` → `display: none` in the stylesheet),
  // deliberately not the `hidden` attribute: `hidden` is conditions.js's own
  // per-FIELD channel (`node.hidden = !state.active`), and a section writing in
  // that same alphabet would be indistinguishable from a field the condition
  // engine had switched off — one layer's next pass would clobber the other's
  // decision. Two layers, two channels, no collision possible.
  //
  // `aria-hidden` + `inert` say the same thing to the accessibility tree and to
  // focus. `display: none` already does both; these are what keep it true if the
  // hiding ever becomes something softer, and `inert` is what stops a
  // keyboard from tabbing into a step nobody can see.
  //
  // It replaces the scrollspy this nav used to run: with one section visible,
  // "which section is the operator reading" is no longer a scroll-position
  // guess — it is the step, and the step is known.
  var stepId = null; // survives htmx swaps within a page load; nothing longer.

  function steps() {
    return document.querySelectorAll("#wizard .form-section");
  }

  function sectionTitle(section) {
    var h2 = section.querySelector("h2");
    return h2 ? h2.textContent.trim() : "section";
  }

  function showStep(id, moveFocus) {
    var list = steps();
    if (!list.length) return;
    var wanted = null;
    for (var i = 0; i < list.length; i++) if (list[i].id === id) wanted = list[i];
    if (!wanted) wanted = list[0];
    stepId = wanted.id;
    for (var j = 0; j < list.length; j++) {
      var on = list[j] === wanted;
      list[j].classList.toggle("step-off", !on);
      list[j].inert = !on;
      if (on) list[j].removeAttribute("aria-hidden");
      else list[j].setAttribute("aria-hidden", "true");
    }
    // The list only: the nav's "Go to review & submit" anchor points at the last
    // section too, and marking it current would say the operator is in two
    // places at once.
    var links = document.querySelectorAll("#wizard-nav ol a[href^='#sect-']");
    for (var k = 0; k < links.length; k++) {
      var here = links[k].getAttribute("href") === "#" + stepId;
      links[k].classList.toggle("current", here);
      if (here) links[k].setAttribute("aria-current", "step");
      else links[k].removeAttribute("aria-current");
    }
    // Focus follows the step, or a Next click leaves focus on a button that has
    // just gone inert and the keyboard starts again from the top of the page.
    if (moveFocus) {
      wanted.setAttribute("tabindex", "-1");
      wanted.focus();
    }
  }

  function stepButton(label, id, klass) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = klass;
    button.setAttribute("data-step", id);
    button.textContent = label;
    return button;
  }

  // Previous / Next at the foot of each step, built here rather than in the
  // template because they only mean anything once this file has run: with no JS
  // the wizard is the single scrolling page it has always been, and a "Next"
  // pointing at a section already on screen would be a lie.
  function stepFeet(list) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].querySelector(".step-foot")) continue;
      var foot = document.createElement("div");
      foot.className = "step-foot";
      if (i > 0) foot.appendChild(stepButton("← " + sectionTitle(list[i - 1]), list[i - 1].id, "prev"));
      if (i < list.length - 1) {
        foot.appendChild(stepButton(sectionTitle(list[i + 1]) + " →", list[i + 1].id, "next"));
      }
      list[i].appendChild(foot);
    }
    // The submit is MOVED into the last step's foot — the same node the server
    // rendered, every attribute intact, never a copy. Two submit buttons in one
    // form is two ways to send the same draft and one of them going stale.
    var submit = document.getElementById("wizard-submit");
    var last = list[list.length - 1].querySelector(".step-foot");
    if (submit && last && submit.parentNode !== last) last.appendChild(submit);
  }

  function stepper() {
    var form = document.getElementById("wizard");
    var list = steps();
    if (!form || !list.length) return;
    stepFeet(list);
    // Already stepping this DOM (an autosave or a person-card swap): keep the
    // operator where they are. Only a freshly rendered form picks a step.
    if (form.hasAttribute("data-stepped")) return showStep(stepId, false);
    form.setAttribute("data-stepped", "1");
    // A refused submit re-renders the whole form with its errors in place, and
    // an error on a step nobody is looking at is an error nobody can fix — so a
    // render carrying one opens there. Otherwise: where they were before the
    // swap, else the first step.
    var start = null;
    for (var i = 0; i < list.length && !start; i++) if (list[i].querySelector(".err")) start = list[i].id;
    showStep(start || stepId || list[0].id, false);
  }

  // ---- Long checkbox groups: type to filter (QA F-001) ----------------------
  //
  // `countriesOfActivity` is 248 checkboxes — a scroll, not a choice. This is a
  // filter over what is already on the page: display only, nothing about the
  // group's markup, names, values or count changes, and the form engine is not
  // involved at all. A CHECKED box is never hidden by a search term, so what the
  // operator has answered cannot disappear behind one.
  //
  // Not attached to a condition-controlled box: conditions.js sets `disabled`
  // and `required` on *every* control inside one, and a search field wearing
  // `required` would block the submit on an empty search term. A conditional
  // group simply keeps the list it always had.
  var FILTER_FROM = 20;

  function choiceFilter(box) {
    var checks = box.querySelectorAll("input[type=checkbox]");
    if (checks.length < FILTER_FROM) return;
    var items = [];
    for (var i = 0; i < checks.length; i++) {
      var label = checks[i].closest("label");
      if (label) items.push({ box: checks[i], label: label, text: label.textContent.toLowerCase() });
    }
    if (!items.length) return;

    var bar = document.createElement("div");
    bar.className = "choice-filter";
    // No `name`: it is not an answer and must never reach the draft body.
    var search = document.createElement("input");
    search.type = "search";
    search.className = "choice-filter-input";
    search.setAttribute("autocomplete", "off");
    search.placeholder = "Filter these " + items.length + " options";
    search.setAttribute("aria-label", "Filter the options below by code or name");
    var count = document.createElement("span");
    count.className = "muted";
    var clear = document.createElement("button");
    clear.type = "button";
    clear.textContent = "Clear";

    function run() {
      var query = search.value.trim().toLowerCase();
      var shown = 0;
      for (var j = 0; j < items.length; j++) {
        var hit = !query || items[j].box.checked || items[j].text.indexOf(query) !== -1;
        items[j].label.classList.toggle("filtered-out", !hit);
        if (hit) shown++;
      }
      count.textContent = shown === items.length
        ? items.length + " options"
        : shown + " of " + items.length + " shown — ticked ones always stay";
    }
    search.addEventListener("input", run);
    // A display-only control has no business waking the autosave: `change`
    // fires on blur and `hx-trigger="change from:#wizard"` would post the draft
    // again for a search term that is not in it.
    search.addEventListener("change", function (event) { event.stopPropagation(); });
    clear.addEventListener("click", function () { search.value = ""; run(); search.focus(); });

    bar.appendChild(search);
    bar.appendChild(clear);
    bar.appendChild(count);
    box.insertBefore(bar, items[0].label);
    run();
  }

  function choiceFilters() {
    var boxes = document.querySelectorAll("#wizard .field.choices");
    for (var i = 0; i < boxes.length; i++) {
      if (boxes[i].querySelector(".choice-filter")) continue;
      if (boxes[i].hasAttribute("data-conditions") || boxes[i].hasAttribute("data-required-when")) continue;
      choiceFilter(boxes[i]);
    }
  }

  // The first control on this form that the browser will refuse to submit.
  function firstInvalid(form) {
    var nodes = form.querySelectorAll("input, select, textarea");
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].willValidate && !nodes[i].checkValidity()) return nodes[i];
    }
    return null;
  }

  function chipSlot(input) {
    return document.querySelector(input.getAttribute("data-target"));
  }

  function csrf() {
    try {
      return JSON.parse(document.body.getAttribute("hx-headers") || "{}");
    } catch (e) {
      return {};
    }
  }

  function upload(input) {
    var file = input.files && input.files[0];
    var slot = chipSlot(input);
    if (!file || !slot) return;
    if (file.size > 10 * 1024 * 1024) {
      slot.insertAdjacentHTML("beforeend", '<span class="chip">File is larger than 10 MB.</span>');
      input.value = "";
      return;
    }
    var query = "?purpose=" + encodeURIComponent(input.getAttribute("data-purpose") || "") +
      "&filename=" + encodeURIComponent(file.name);
    if (input.getAttribute("data-draft")) query += "&draft=" + encodeURIComponent(input.getAttribute("data-draft"));
    if (input.getAttribute("data-person") !== null) query += "&person=" + encodeURIComponent(input.getAttribute("data-person"));
    // **No intent nonce.** An upload's double-submit guard is the request hash,
    // which for this route covers the file's own sha256 plus `purpose`, `name`
    // and `scope` — so a transport retry of the same upload still resolves to
    // the same operation. A client-minted uuid is not a nonce this console
    // issued and is refused outright (`web.intent_of`), and the
    // server-minted alternative is worse than nothing here: one nonce per page
    // render, several uploads per page, so the second file would resolve onto
    // the first one's operation and be chipped as a document nobody stored.

    // Raw body, not multipart: the server reads the bytes and sniffs the type
    // itself (app.documents.sniff), so no multipart parser is needed anywhere.
    var headers = csrf();
    headers["Content-Type"] = "application/octet-stream";
    // The header batchUpload already sends, for the same reason: it tells the
    // middleware this is htmx's request rather than a browser navigation.
    headers["HX-Request"] = "true";
    fetch("/documents" + query, { method: "POST", headers: headers, body: file })
      .then(function (response) {
        // `fetch` RESOLVES on 4xx and 5xx, so the catch below only ever saw a dropped
        // connection: an expired session's `{"detail":"authentication required"}` and a
        // 403's `<h1>` were both grafted into the document list as if they were this
        // route's own chip, and the draft was then told it had changed. The one non-2xx
        // that IS this route's chip is the over-size refusal (`onboarding.upload` →
        // `_chip_error(..., status_code=413)`); every other one is somebody else's
        // body.
        if (!response.ok && response.status !== 413) throw new Error(String(response.status));
        return response.text();
      })
      .then(function (html) {
        slot.insertAdjacentHTML("beforeend", html);
        progress();
        // Persist the new chip's hidden input with the rest of the draft.
        document.body.dispatchEvent(new Event("draft-changed"));
      })
      .catch(function () {
        // Nothing was attached, so nothing is announced: no `draft-changed`
        // (the draft did not change) and no `progress()` (no answer moved).
        slot.insertAdjacentHTML(
          "beforeend",
          '<span class="chip">Upload failed — nothing was attached. Reload the page and try again.</span>'
        );
      })
      // Either way: a file left in the input cannot be re-picked, because
      // choosing the same file again fires no `change`.
      .then(function () { input.value = ""; });
  }

  // A filled batch-payout template. The same raw-body idiom
  // as the document uploader above — the server streams it under a cap and
  // there is no multipart parser anywhere in this app — with the route the
  // operator is on carried as the form's own fields, so the file and the route
  // cannot disagree about which payout is being made.
  //
  // Every outcome the server can produce is a redirect (`HX-Redirect`): a
  // refusal goes back to this page with its sentence in the flash banner, a
  // good file goes to the batch's report. So the only failure this has to
  // render itself is one where no answer arrived at all.
  function batchUpload(input) {
    var file = input.files && input.files[0];
    var form = input.form;
    if (!file || !form) return;
    var slot = form.parentNode.querySelector("[data-batch-error]");
    function fail(text) {
      if (!slot) return;
      slot.textContent = text;
      slot.hidden = false;
    }
    if (slot) slot.hidden = true;
    var params = new URLSearchParams();
    // `FormData` also yields the File itself; only the text fields are the
    // route, so anything that is not a string is skipped.
    new FormData(form).forEach(function (value, name) {
      if (typeof value === "string") params.append(name, value);
    });
    params.set("filename", file.name);
    var headers = csrf();
    headers["Content-Type"] = "text/csv";
    // Asks the server for `HX-Redirect` rather than a 303 this fetch would
    // follow silently and then throw away.
    headers["HX-Request"] = "true";
    fetch(form.getAttribute("action") + "?" + params.toString(), {
      method: "POST",
      headers: headers,
      body: file
    })
      .then(function (response) {
        var to = response.headers.get("HX-Redirect");
        if (to) {
          window.location = to;
          return;
        }
        fail("The upload was not accepted (HTTP " + response.status + "). Nothing was stored — reload and try again.");
      })
      .catch(function () {
        fail("The upload did not reach the console. Nothing was stored — check the connection and try again.");
      })
      .then(function () { input.value = ""; });
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.matches && target.matches("input[type=file][data-upload]")) upload(target);
    else if (target.matches && target.matches("input[type=file][data-batch]")) batchUpload(target);
    else refresh();
  });

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target.closest) return;
    // The tour, from every one of its controls — the offer card's two buttons
    // and the three in the card's foot. First, and unambiguous: nothing else on
    // any page carries `data-tour`.
    var tourHit = target.closest("[data-tour]");
    if (tourHit) {
      var what = tourHit.getAttribute("data-tour");
      if (what === "start") tourStart(tourHit);
      else if (what === "dismiss") {
        var offer = document.getElementById("tour-offer");
        if (offer) offer.hidden = true;
        tourRemember();
      } else if (what === "next") tourGo(1);
      else if (what === "back") tourGo(-1);
      else tourEnd();  // skip
      return;
    }
    // The drawer, from all three directions. Remembering the opener has to
    // happen on the way in, because by the time the swap lands the click is
    // long over and htmx's own event does not know what focus should go back
    // to. htmx issues the request itself — nothing here preventDefaults it.
    var quick = target.closest("[data-quick]");
    if (quick) { drawerOpener = quick; return; }
    if (target.closest("[data-drawer-close]")) { closeDrawer(); return; }
    // Outside: any click that is not in the panel and not on a quick-view button
    // dismisses it. Cheap and non-modal — a link outside still navigates, which is the
    // behaviour an overlay (as opposed to a dialog) is supposed to have. No `return`:
    // dismissing is not what the click was FOR, so whatever else this click meant still
    // happens.
    if (drawerOpen() && !target.closest("#drawer")) closeDrawer();
    // The stepper, from either end: a nav entry (an in-page anchor, so it still
    // works with this file absent) or a Previous/Next button at a step's foot.
    // Direct nav clicks jump anywhere, in any order — the steps are a way to
    // read the form, not a track the operator is pushed along.
    var step = target.closest("#wizard-nav a[href^='#sect-'], #wizard [data-step]");
    if (step) {
      var wanted = step.getAttribute("data-step") || step.getAttribute("href").slice(1);
      if (document.getElementById(wanted)) {
        event.preventDefault();
        showStep(wanted, true);
      }
      return;
    }
    // Submitting from the last step, with something unanswered three steps back:
    // the browser cannot report a validation failure on a control it cannot
    // focus — it refuses the submit and logs "not focusable" instead of showing
    // the message, which is a click that does nothing. This runs before that
    // (the validation is this click's *default action*), so the step holding the
    // first offending control is on screen by the time the browser looks at it.
    var submit = target.closest("#wizard button[type=submit]");
    if (submit) {
      var bad = firstInvalid(document.getElementById("wizard"));
      var section = bad && bad.closest(".form-section");
      if (section) showStep(section.id, false);
      return;
    }
    var remove = target.closest("[data-remove-chip]");
    if (remove) {
      remove.closest(".chip").remove();
      document.body.dispatchEvent(new Event("draft-changed"));
      progress();
      return;
    }
    // The Transact page's move-money launcher. A path parameter cannot come out
    // of a GET form, so the customer the operator picked is turned into a URL
    // here — keyed off `data-launch`, so no other page has one.
    var launch = target.closest("[data-launch]");
    if (launch) {
      var pick = document.getElementById("launch-customer");
      if (pick && pick.value) {
        window.location = "/customers/" + encodeURIComponent(pick.value) + launch.getAttribute("data-launch");
      }
      return;
    }
    // One copy button per deposit-instruction row and per IDV link: the value
    // lives in the field the button points at, so nothing is retyped by hand.
    var copy = target.closest("[data-copy]");
    if (copy) {
      var field = document.querySelector(copy.getAttribute("data-copy"));
      if (field && navigator.clipboard) navigator.clipboard.writeText(field.value);
      else if (field) { field.select(); document.execCommand("copy"); }
      if (field) {
        var was = copy.textContent;
        copy.textContent = "Copied";
        setTimeout(function () { copy.textContent = was; }, 1200);
      }
    }
  });

  // Who owns this button's `disabled` right now. While its form is mid-POST it is
  // htmx's: both money forms carry `hx-disabled-elt` (`#payout-form` starts its list
  // with `find button[type=submit]` and goes on to freeze the route controls too; the
  // convert confirm form → `find button`) and neither names an `hx-indicator`, so
  // `htmx-request` sits on the form itself for the whole flight. The clocks below run
  // once a second off the QUOTE alone, and one tick landing mid-send used to write
  // `disabled = false` over htmx's disable: the money button looked pressable during
  // its own send, and the second click it invited did nothing (`hx-sync="this:drop"`
  // eats it). Exactly-once was never at risk; what was broken is what the operator was
  // told, which is the same defect class as a green "Draft saved" over an unsaved edit.
  // Only the disabled write is skipped — the countdown keeps counting, because that is
  // still true while the send is in flight.
  function sending(control) {
    var form = control.form || (control.closest && control.closest("form"));
    return !!form && form.classList.contains("htmx-request");
  }

  // A quote is indicative and expires. Past `expiresAt` the send button is
  // disabled until the operator refreshes it — the server checks the same
  // timestamp on submit, because disabling a button is a display decision.
  function quoteGuard() {
    var submit = document.getElementById("payout-submit");
    var note = document.getElementById("quote-stale-note");
    var panel = document.getElementById("quote-panel");
    if (!submit) return;
    var expires = panel && panel.getAttribute("data-expires-at");
    // No quote asked for at all: the payout is not gated on one.
    var stale = !!expires && !(Date.parse(expires) > Date.now());
    if (!sending(submit)) submit.disabled = stale;
    if (note) note.hidden = !stale;
  }

  // A quote *option* is a locked price with a deadline, so the confirm screen
  // counts it down to the second rather than merely disabling at the end. Same
  // rule as above: the server re-checks `expiresAt` on submit.
  function optionGuard() {
    var badge = document.getElementById("option-expiry");
    if (!badge) return;
    var expires = badge.getAttribute("data-expires-at");
    var parsed = expires ? Date.parse(expires) : NaN;
    var submit = document.getElementById("confirm-submit");
    // Unknown or unparseable reads as expired — the only safe reading of "we
    // cannot tell when this stops being true" is that it already has.
    var left = isNaN(parsed) ? -1 : Math.floor((parsed - Date.now()) / 1000);
    badge.textContent = left > 0 ? ("expires in " + left + "s") : "expired — re-quote";
    badge.className = left > 0 ? "pill wait" : "pill bad";
    if (submit && !sending(submit)) submit.disabled = !(left > 0);
  }

  function guards() { quoteGuard(); optionGuard(); }

  // **A poll may not swap a region while a mutation from inside it is in flight**
  // (direction (c)).
  //
  // The Execute/Cancel forms on an order, and the Retry/Mark-abandoned forms on an
  // operation, live INSIDE regions that self-replace on a timer (`hx-trigger="every
  // 15s"` + `hx-select` + `hx-swap="outerHTML"`). Each such swap re-renders the forms,
  // and each render mints a fresh `intent` nonce — the console's per-render
  // duplicate-suppression token (OPERATIONS_SPEC §1). So a background refresh the
  // operator never asked for silently replaced the nonce their pending click was made
  // under. If that click's response was then lost, their next press carried a *fresh*
  // nonce, which by design means "a deliberate new attempt": it missed the intent
  // guard, and once the first operation went terminal the request-hash guard had
  // released too, so a second call went on the wire.
  //
  // Persisting intents fixes the case where a consumed nonce comes back. It
  // cannot fix this one, because here the nonce never comes back — the poll
  // threw it away. Freezing the action row instead was rejected: its visibility
  // is derived from status, so a frozen row keeps offering Execute on an order
  // that has already executed, trading a latent nonce issue for a live lying
  // affordance. Pausing only the swap keeps both — the nonce the operator
  // pressed under survives their request, and the actions stay as fresh as the
  // status that derives them.
  //
  // `hx-sync="this:drop"` on the forms already stops a second submit *from the
  // same form* during flight; what it cannot see is the poller, which is a
  // different element issuing a different request.
  //
  // Written here rather than as an `hx-trigger` filter because `[…]` filters are
  // `eval`, and this console ships `script-src 'self'` with no `unsafe-eval`.
  //
  // Matches ANY polled region containing an in-flight form, by
  // attribute rather than by id. The other polled regions on the site do carry
  // forms — applications status has "Fix & resubmit", batch detail has its
  // dispatch and abandon controls — and they are covered by the same attribute
  // test, as is a poller added later without being listed here.
  function pollBlocked(event) {
    var elt = event.detail && event.detail.elt;
    // Only the poll itself: a request the operator started is never blocked,
    // including the submit that makes the region busy in the first place.
    if (!elt || !elt.getAttribute) return;
    var trigger = elt.getAttribute("hx-trigger") || "";
    if (trigger.indexOf("every ") !== 0) return;
    if (elt.querySelector("form.htmx-request")) event.preventDefault();
  }
  document.body && document.body.addEventListener("htmx:beforeRequest", pollBlocked);

  // The autosave's failure state. htmx's `responseHandling` (base.html) makes a
  // 5xx a *no-swap*, and a dropped connection never reaches a swap at all — so
  // without this the span keeps whatever it last said, which after one good save
  // is a green "Draft saved 12:04:31" sitting over unsaved edits. A save state
  // that lies is worse than no save state.
  //
  // Warn, not bad: a failed save is not a terminal failure, it is the console needing a
  // human (DESIGN.md's status rule) — the draft is still on screen and the next change
  // tries again. That last clause is load-bearing copy, so it is asserted in the
  // browser: htmx leaves the `hx-trigger` listener attached across a failed request,
  // and `hx-sync="this:replace"` queues rather than detaches, so the next `change
  // from:#wizard` really does re-fire. Written as a *child* span, the same shape
  // `/save` swaps in, so the next successful save replaces it wholesale and there is no
  // failure class left to clear. The stylesheet tells the two apart on the child's own
  // class.
  function saveFailed(event) {
    var span = document.getElementById("save-state");
    var elt = (event.detail && event.detail.elt) || event.target;
    if (!span || elt !== span) return;
    span.innerHTML =
      '<span class="warn">Save failed — retrying on your next change. Don\'t leave yet.</span>';
  }
  document.body && document.body.addEventListener("htmx:sendError", saveFailed);
  document.body && document.body.addEventListener("htmx:responseError", saveFailed);
  document.body && document.body.addEventListener("htmx:sendError", drawerFailed);
  document.body && document.body.addEventListener("htmx:responseError", drawerFailed);

  // Escape closes the drawer, wherever focus is — including inside it, which is
  // where this pass just put it. Guarded on the drawer actually being open so
  // the key keeps meaning what it means everywhere else on the page (dismissing
  // a native autocomplete, reverting a search box) when it is not.
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && drawerOpen()) {
      event.preventDefault();
      closeDrawer();
      return;
    }
    // The tour's keyboard: Escape out of it from anywhere, and Tab cycles the
    // card's own controls rather than walking off into the page behind. The two
    // modes never coexist (the drawer is a list surface, the tour runs on the
    // Overview), but the order says which would win if they ever did.
    if (tourAt < 0 || !tourNodes) return;
    if (event.key === "Escape") {
      event.preventDefault();
      tourEnd();
      return;
    }
    if (event.key !== "Tab") return;
    var focusable = [];
    var controls = tourNodes.card.querySelectorAll("a[href], button:not([disabled])");
    // `offsetParent` drops the ones that are not on screen — the "see it" link
    // is `hidden` on four of the six steps, and a trap that cycled through it
    // would be a Tab that appears to do nothing.
    for (var i = 0; i < controls.length; i++) {
      if (controls[i].offsetParent) focusable.push(controls[i]);
    }
    if (!focusable.length) return;
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    var here = document.activeElement;
    if (!tourNodes.card.contains(here)) {
      // Focus got out some other way; put it back rather than letting the next
      // Tab walk the whole page.
      event.preventDefault();
      first.focus();
    } else if (event.shiftKey && (here === first || here === tourNodes.card)) {
      // Focus rests on the card itself at the top of every step: forward from
      // there is already the first control, backward has to be sent to the last.
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && here === last) {
      event.preventDefault();
      first.focus();
    }
  });

  // ---- Quick-view drawer ---------------------------------
  //
  // htmx fetches and swaps the body; everything below is the part htmx has no
  // opinion about — dismissal and focus.
  //
  // NON-MODAL by choice (`role="complementary"` in the template, never
  // `dialog`/`aria-modal`): nothing else on the page is made `inert` or
  // `aria-hidden`, there is no focus trap and there is no scrim element. A
  // quick view is a look at one row while the list stays live behind it. What a
  // keyboard operator still gets is the part that matters: focus moves INTO the
  // panel on open, and back to the exact button that opened it on close — a
  // drawer that dumps focus at the top of the document costs more to use than
  // the full page it was meant to save.
  //
  // "Open" is a state of the DOM, not a variable: the body has children, or a
  // request is in flight (htmx's own `htmx-request` class, put on the shell by
  // each button's `hx-indicator`), and the stylesheet shows the panel in
  // exactly those two cases. So a swap, a back button or a re-render cannot
  // leave this file believing something the page has stopped showing — and
  // "open" covers the LOADING panel too, which is what makes Escape work
  // against a drawer that is still empty.
  var drawerOpener = null;

  function drawerPanel() { return document.getElementById("drawer"); }
  function drawerBody() { return document.getElementById("drawer-body"); }
  function drawerBusy() {
    var panel = drawerPanel();
    return !!panel && panel.classList.contains("htmx-request");
  }
  function drawerOpen() {
    var body = drawerBody();
    return (!!body && !!body.firstChild) || drawerBusy();
  }

  function focusDrawer() {
    var panel = drawerPanel();
    if (panel) panel.focus();
  }

  function closeDrawer() {
    var panel = drawerPanel();
    if (!panel) return;
    // Dismissing has to CANCEL, not merely clear. Without this, Escape during a
    // slow load empties a body that is about to be filled: the in-flight
    // response lands a moment later, the drawer reopens by itself and takes
    // focus back off whatever the operator moved on to.
    //
    // `htmx:abort` on `#drawer` is the vendored 2.0.4 contract — htmx's own
    // body-level listener aborts `internalData(event.target).xhr`, and `#drawer`
    // is where the xhr lives because it is the buttons' `hx-sync` master. An
    // aborted xhr raises `htmx:sendAbort`, not `sendError`, so cancelling
    // cannot render the failure card below.
    if (window.htmx && drawerBusy()) window.htmx.trigger(panel, "htmx:abort");
    var body = drawerBody();
    if (body) body.innerHTML = "";
    var opener = drawerOpener;
    drawerOpener = null;
    // Only if it is still on the page: a swap of the table underneath can have
    // replaced the row, and focusing a detached node silently sends focus to
    // <body> instead. Better to leave it where it is than to move it nowhere.
    if (opener && document.contains(opener)) opener.focus();
  }

  // The failure the drawer would otherwise show as nothing at all. The fragment route
  // answers 200 even when Conduit is unreachable (the problem card is rendered inside
  // it), so what reaches here is the middleware's 401/403, a 5xx, or a request that
  // never completed — all of which htmx's `responseHandling` (base.html) deliberately
  // drops without a swap. A click that appears to do nothing is the one outcome this
  // pattern cannot have.
  function drawerFailed(event) {
    var elt = (event.detail && event.detail.elt) || event.target;
    if (!elt || !elt.closest || !elt.closest("[data-quick]")) return;
    var body = drawerBody();
    if (!body) return;
    var xhr = event.detail && event.detail.xhr;
    var why = xhr && xhr.status
      ? "The console answered HTTP " + xhr.status + "."
      : "The request did not reach the console.";
    var box = document.createElement("div");
    box.className = "problem";
    box.setAttribute("role", "alert");
    // textContent throughout: nothing here interpolates a server string, and
    // building it as nodes keeps it that way if someone later wants one.
    var title = document.createElement("strong");
    title.textContent = "The quick view could not be loaded";
    var detail = document.createElement("div");
    // Deliberately says "record", not "application": the same shell now carries
    // a contact's history (`contacts/list.html`), and a failure card that named
    // the wrong kind of record would be this console lying about what it just
    // failed to show.
    detail.textContent = why + " Nothing has been changed.";
    var next = document.createElement("div");
    // The full page is this fragment's URL minus its last segment. Guarded
    // rather than assumed: a way on that points at the wrong page would be
    // worse than the dead click this whole branch exists to prevent.
    var url = elt.getAttribute("hx-get") || "";
    // No Close in here: the shell owns the one dismiss, in every state.
    next.className = "drawer-actions";
    if (/\/quick$/.test(url)) {
      var link = document.createElement("a");
      link.className = "btn primary";
      link.href = url.replace(/\/quick$/, "");
      link.textContent = "Open application";
      next.appendChild(link);
    }
    box.appendChild(title);
    box.appendChild(detail);
    box.appendChild(next);
    // Replaces, never appends: a failed load on top of the PREVIOUS row's facts
    // would leave one application's details under another's error message.
    body.innerHTML = "";
    body.appendChild(box);
    focusDrawer();
  }

  // Timestamps, in the zone the operator actually lives in.
  //
  // The server renders UTC and says so ("Aug 31, 05:26 UTC" — app/web/__init__
  // ::ts). That is the truth of the row and what a support conversation quotes,
  // so it stays: on the `title` as full ISO, and on screen for anyone this pass
  // cannot serve. What it is *not* is comprehensible — an operator reading a
  // payout stamped 05:26 has to do arithmetic to know whether that was before
  // lunch. So the DISPLAY text becomes local, with the zone named: both times
  // on screen are labelled, and neither can be mistaken for the other.
  //
  // Idempotent by construction — it reads `datetime`, never the text it wrote —
  // so re-running it after every swap costs nothing but the walk. Elements
  // without `datetime` (ts's verbatim and overflow fallbacks) are left exactly
  // as the server sent them: an unreadable stamp is not made prettier by
  // guessing at it.
  //
  // Intl, no library. `zoned === null` is "not tried yet", `false` is
  // "this browser has no Intl" — in which case every row keeps the server's UTC.
  var zoned = null;
  function localTimes(root) {
    if (zoned === null) {
      try {
        zoned = window.Intl && new Intl.DateTimeFormat("en-US", {
          month: "short", day: "2-digit", year: "numeric",
          hour: "2-digit", minute: "2-digit", hourCycle: "h23",
          timeZoneName: "short"
        });
      } catch (e) { zoned = false; }
      if (!zoned) zoned = false;
    }
    if (!zoned) return;
    var thisYear = new Date().getFullYear();
    var nodes = (root || document).querySelectorAll("time[datetime]");
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var when = new Date(node.getAttribute("datetime"));
      if (isNaN(when.getTime())) continue;
      var part = {};
      try {
        var parts = zoned.formatToParts(when);
        for (var p = 0; p < parts.length; p++) part[parts[p].type] = parts[p].value;
      } catch (e) { continue; }
      // No zone name, no rewrite: a local time with nothing saying which zone it
      // is would be worse than the UTC it replaced.
      if (!part.month || !part.hour || !part.timeZoneName) continue;
      // The server's year rule, decided in local time — the same stamp can be
      // last year here and this year in UTC, and the compact form must not drop
      // a year the reader's own calendar still needs.
      var date = part.month + " " + part.day;
      if (Number(part.year) !== thisYear) date += " " + part.year;
      node.textContent = date + ", " + part.hour + ":" + part.minute + " " + part.timeZoneName;
    }
  }

  // ---- Product tour ----------------------------------------------
  //
  // Six steps that teach the MODEL, not the buttons: which Conduit this console
  // is wired to, where browsing stops and acting starts, what this page is
  // listing, what a customer is for, what an RFI is, and how money leaves.
  // Native — no library, no dependency, no build step; the overlay, the
  // spotlight ring and the card are three elements this file creates while the
  // tour runs and removes when it ends.
  //
  // MODALITY, and the deliberate departure: the quick-view drawer above is
  // non-modal on purpose (a glance at a row while the list stays live). The
  // tour is the other kind of thing — a mode an operator explicitly entered —
  // so its card IS focus-trapped, and Escape always exits (the card says so, in
  // words, at the foot of every step). The trap is FOCUS-ONLY: nothing on the
  // page is made `inert` or `aria-hidden`, because the page is what the tour is
  // describing and a screen reader must still be able to read it.
  //
  // Every step anchors to a STABLE ELEMENT ID, never to copy text. The tour runs
  // on the Overview — its trigger page — so two of the six subjects live
  // elsewhere; those steps carry a static explainer and a way to go see it, and
  // the spotlight rings nothing at all. Pointing at a nearby innocent element
  // instead would be this console lying about what it is describing.
  var TOUR_KEY = "conduit.console.tour";

  var TOUR = [
    {
      target: "env-badge",
      title: "Which Conduit you are wired to",
      text: "Every page carries this badge: the environment this console's API key points at, "
        + "and the host it calls. Green means sandbox — money that is not real. Amber means "
        + "anything else, because then it is. Colour in this console is always the state of "
        + "money, never decoration."
    },
    {
      target: "ribbon",
      title: "Browse above the rule, act below it",
      text: "The ribbon is split by the Actions rule. Everything above it reads the state of "
        + "money — customers, applications, accounts, contacts, the two ledgers, the RFI "
        + "inbox. Everything below it changes money: onboard someone, send a payout, "
        + "transfer, convert. Nothing above the rule starts a payment."
    },
    {
      target: "attention-table",
      title: "Needs attention is the work that is yours",
      // Every clause here is checked against the code that fills this table:
      // `dashboard.py` selects `ACTIVE_STATES` only, so a terminal state — a refusal
      // included — can never appear on it. That leaves amber (`outcome_unknown`) and
      // red (`stalled`), and the sentences below are their own `NEXT_STEP` copy,
      // compressed: "the reconciler is asking Conduit what happened, nothing to do, and
      // do not resubmit" and "retrying is safe: same request, same idempotency key, so
      // it cannot pay twice".
      text: "Operations this console started and has not seen finish. Amber means it is still "
        + "asking Conduit what happened — nothing to do yet, and don't resubmit. Red means it "
        + "stopped asking without an answer, and settling that one is yours: retrying is safe, "
        + "because it goes out under the same idempotency key and cannot pay twice. The last "
        + "column says what happens next either way. All of it comes from this console's own "
        + "database, so this page renders whether or not Conduit is reachable."
    },
    {
      title: "Every flow names a customer",
      // Re-checked, which is when the second way in appeared: the
      // ribbon's three Transact entries land on the FORMS, and the customer is
      // the first control of step 1 rather than a prefix of the URL. The
      // customer page's row of buttons still works and still lands on the same
      // forms with that customer filled in — so this step describes both, and
      // no longer says there is no free-standing send-money screen, because
      // there now is one.
      //
      // Re-checked again, on the same rule: the quick-actions
      // cluster is a THIRD way in, it is on screen while this very step is
      // being read, and a tour that spotlights the ribbon while three
      // unmentioned pills sit above the page head would be describing a console
      // the operator is not looking at. One clause, not a seventh step — the
      // cluster is another door to the forms this step already explains, and
      // growing the tour for a tweak is the thing to avoid.
      //
      // The second half of the same sentence goes here rather than in a step,
      // for the same reason: "Pay a contact" is a THIRD end to start from —
      // neither the customer nor the route but the destination — and the claim
      // that every one of these ways answers "who" as the form's first field
      // stopped being true of it. It brings its customer with it.
      text: "Conduit hangs every resource off a customer, so every flow names one — but you "
        + "can start from any end. Open a customer and the row of buttons on their page "
        + "begins any flow with them already chosen: applications, contacts and payouts to "
        + "read, and — where you hold payout.create, transfer.create or order.create — send a "
        + "payout, transfer, convert. Or start from the ribbon or the quick actions at "
        + "the top right of any page, and answer “who” as the first field of the form — "
        + "except “Pay a contact”, which starts from the destination and brings its "
        + "customer with it.",
      see: { href: "/customers", label: "Open the customer directory" }
    },
    {
      target: "ribbon-rfis",
      title: "The RFI inbox is Conduit asking",
      text: "A request for information is raised by Conduit, not by you — about a customer or "
        + "about a transaction, which is why it sits under neither. This console only answers "
        + "them. The number counts the open requests it has observed; no number means none "
        + "were observed, or the read failed."
    },
    {
      title: "One payment, or many",
      // "Opens on a fork", not "forks before it asks anything else" (the
      // design pass): since the ribbon lands here, the fork screen leads with
      // the customer picker, so the fork is no longer literally the first
      // question — it shares the screen with "who".
      text: "“Send a payout” opens on a fork — one payment, or many — with “who” asked on the "
        + "same screen. A single payout is a form whose "
        + "fields come from Conduit's live requirements for that route. A batch is a CSV — one "
        + "row per payment, validated and totalled before anything can be dispatched, and "
        + "uploaded for one customer. Both go "
        + "through the same operations ledger, so a second click cannot pay twice.",
      see: { href: "/payouts", label: "Open the payout fork" }
    }
  ];

  var tourAt = -1;       // -1 is "not running"; otherwise the step index.
  var tourNodes = null;
  var tourOpener = null;

  // Storage that may not exist. A private window or blocked site data throws on
  // the WRITE, not on the property read, so the probe has to be a write — and
  // every access in here is wrapped, because chrome that crashes is worse than
  // chrome that forgets.
  //
  // The degradation is deliberate and one-way: localStorage (remembered for
  // good) → sessionStorage (offered once per session, never a nag loop in a
  // private window) → nothing, in which case there is NO auto-offer at all and
  // the ribbon's "Take the tour" link is the only affordance. Skipping is never
  // final in any of the three.
  function tourStore() {
    var names = ["localStorage", "sessionStorage"];
    for (var i = 0; i < names.length; i++) {
      try {
        var store = window[names[i]];
        store.setItem(TOUR_KEY + ".probe", "1");
        store.removeItem(TOUR_KEY + ".probe");
        return store;
      } catch (e) { /* try the next one */ }
    }
    return null;
  }

  // "Seen" covers both dismissal and completion: they mean the same thing to the
  // offer. No store at all reads as seen — that is the no-auto-offer rule.
  function tourSeen() {
    try {
      var store = tourStore();
      return !store || !!store.getItem(TOUR_KEY);
    } catch (e) {
      return true;
    }
  }

  function tourRemember() {
    try {
      var store = tourStore();
      if (store) store.setItem(TOUR_KEY, "1");
    } catch (e) { /* the offer simply comes back; nothing is lost */ }
  }

  function tourReduced() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function tourButton(action, label, klass) {
    var button = document.createElement("button");
    button.type = "button";
    button.setAttribute("data-tour", action);
    if (klass) button.className = klass;
    button.textContent = label;
    return button;
  }

  function tourBuild() {
    if (tourNodes) return tourNodes;
    var scrim = document.createElement("div");
    scrim.id = "tour-scrim";
    var spot = document.createElement("div");
    spot.id = "tour-spot";
    var card = document.createElement("div");
    card.id = "tour-card";
    card.setAttribute("role", "region");
    card.setAttribute("tabindex", "-1");
    // Both ids, in reading order: focus lands on the card at the top of every
    // step, so what is announced is "Step 2 of 6, Browse above the rule, act
    // below it". The count is part of the label rather than a live region —
    // one announcement, not two, and nothing to keep in sync.
    card.setAttribute("aria-labelledby", "tour-count tour-title");
    var count = document.createElement("p");
    count.className = "eyebrow";
    count.id = "tour-count";
    var title = document.createElement("h2");
    title.id = "tour-title";
    var text = document.createElement("p");
    text.id = "tour-text";
    var see = document.createElement("p");
    see.id = "tour-see";
    var link = document.createElement("a");
    link.className = "btn";
    see.appendChild(link);
    var foot = document.createElement("div");
    foot.className = "tour-foot";
    var back = tourButton("back", "Back");
    var next = tourButton("next", "Next", "primary");
    var exit = document.createElement("span");
    exit.className = "muted exit";
    exit.textContent = "Esc closes the tour";
    foot.appendChild(back);
    foot.appendChild(next);
    foot.appendChild(tourButton("skip", "Skip"));
    foot.appendChild(exit);
    card.appendChild(count);
    card.appendChild(title);
    card.appendChild(text);
    card.appendChild(see);
    card.appendChild(foot);
    document.body.appendChild(scrim);
    document.body.appendChild(spot);
    document.body.appendChild(card);
    tourNodes = {
      scrim: scrim, spot: spot, card: card, count: count, title: title,
      text: text, see: see, link: link, back: back, next: next
    };
    return tourNodes;
  }

  // Geometry only — no content, no focus. Called on every step, and again on
  // scroll, resize and any htmx swap, so the ring cannot drift off the thing it
  // is ringing.
  function tourPosition() {
    if (tourAt < 0 || !tourNodes) return;
    var step = TOUR[tourAt];
    var target = step.target ? document.getElementById(step.target) : null;
    var spot = tourNodes.spot;
    var rect = target ? target.getBoundingClientRect() : null;
    if (rect) {
      spot.classList.remove("no-target");
      spot.style.left = rect.left + "px";
      spot.style.top = rect.top + "px";
      spot.style.width = rect.width + "px";
      spot.style.height = rect.height + "px";
    } else {
      // A zero-sized box still casts the whole dim (the overlay is its own
      // box-shadow spread), so an explainer step dims the page and rings
      // nothing rather than ringing something it is not talking about.
      spot.classList.add("no-target");
      spot.style.left = "50%";
      spot.style.top = "0px";
      spot.style.width = "0px";
      spot.style.height = "0px";
    }
    var card = tourNodes.card;
    var wide = card.offsetWidth;
    var high = card.offsetHeight;
    var gap = 16;
    var left, top;
    if (!rect) {
      left = (window.innerWidth - wide) / 2;
      top = (window.innerHeight - high) / 2;
    } else if (rect.right + gap + wide <= window.innerWidth) {
      left = rect.right + gap;
      top = rect.top;
    } else if (rect.bottom + gap + high <= window.innerHeight) {
      left = rect.left;
      top = rect.bottom + gap;
    } else if (rect.top - gap - high >= 0) {
      left = rect.left;
      top = rect.top - gap - high;
    } else {
      left = (window.innerWidth - wide) / 2;
      top = window.innerHeight - high - gap;
    }
    card.style.left = Math.max(gap, Math.min(left, window.innerWidth - wide - gap)) + "px";
    card.style.top = Math.max(gap, Math.min(top, window.innerHeight - high - gap)) + "px";
  }

  function tourShow() {
    var nodes = tourBuild();
    var step = TOUR[tourAt];
    nodes.count.textContent = "Step " + (tourAt + 1) + " of " + TOUR.length;
    nodes.title.textContent = step.title;
    nodes.text.textContent = step.text;
    if (step.see) {
      nodes.link.href = step.see.href;
      nodes.link.textContent = step.see.label;
      nodes.see.hidden = false;
    } else {
      nodes.see.hidden = true;
    }
    nodes.back.disabled = tourAt === 0;
    nodes.next.textContent = tourAt === TOUR.length - 1 ? "Done" : "Next";
    var target = step.target ? document.getElementById(step.target) : null;
    // The one piece of motion in here, and the only thing reduced-motion has to
    // turn off: the spotlight itself has no animation and the card has no
    // transition beyond what every control on the page already has.
    if (target && target.scrollIntoView) {
      target.scrollIntoView({ block: "center", behavior: tourReduced() ? "auto" : "smooth" });
    }
    tourPosition();
    nodes.card.focus();
  }

  function tourStart(opener) {
    var offer = document.getElementById("tour-offer");
    if (offer) offer.hidden = true;
    // Starting counts as having been asked: an operator inside the tour does not
    // need the offer again, whichever way it ends.
    tourRemember();
    tourOpener = opener || null;
    tourAt = 0;
    tourBuild();
    window.addEventListener("scroll", tourPosition, true);
    window.addEventListener("resize", tourPosition);
    tourShow();
    // The vendored DM Sans arrives after this page's first layout (`font-display:
    // swap`), and text that reflows under a ring already drawn leaves the ring
    // behind — the first step is the one this bites, because `?tour=1` opens it
    // at DOMContentLoaded. One reposition when the fonts settle; guarded,
    // because `document.fonts` is optional and a browser without it simply
    // never reflowed.
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(tourPosition);
  }

  function tourEnd() {
    if (tourAt < 0) return;
    tourAt = -1;
    window.removeEventListener("scroll", tourPosition, true);
    window.removeEventListener("resize", tourPosition);
    if (tourNodes) {
      tourNodes.scrim.remove();
      tourNodes.spot.remove();
      tourNodes.card.remove();
      tourNodes = null;
    }
    tourRemember();
    // Back to what opened it if that is still on screen — the offer card's
    // button is not, since starting hid it — else the ribbon's permanent way
    // back in. Never <body>: a mode that dumps focus at the top of the document
    // costs a keyboard operator the page they were on.
    var back = tourOpener && document.contains(tourOpener) && tourOpener.offsetParent
      ? tourOpener
      : document.getElementById("tour-link");
    tourOpener = null;
    if (back) back.focus();
  }

  function tourGo(delta) {
    var at = tourAt + delta;
    if (at < 0) return;
    if (at >= TOUR.length) { tourEnd(); return; }
    tourAt = at;
    tourShow();
  }

  // The first-run offer, on the Overview only (`#tour-offer` is rendered there
  // and nowhere else, always hidden by the server). `?tour=1` is the ribbon
  // link arriving: it starts the tour outright, because that click WAS the ask.
  function tourOffer() {
    var offer = document.getElementById("tour-offer");
    if (!offer) return;
    var asked = false;
    try {
      asked = new URLSearchParams(window.location.search).get("tour") === "1";
    } catch (e) { /* no URLSearchParams: the offer path below still works */ }
    if (asked) { tourStart(document.getElementById("tour-link")); return; }
    if (tourSeen()) return;
    offer.hidden = false;
  }

  // A swap during a tour RE-ANCHORS the step rather than ending it: steps hold
  // ids, so a re-rendered table is simply measured again where it now is. Two
  // things do end it — a target that left the DOM, and a swap that replaced the
  // body the tour's own nodes hang off. Both are the same rule: a ring must
  // never hang over nothing.
  function tourAfterSwap() {
    if (tourAt < 0) return;
    var step = TOUR[tourAt];
    if (!tourNodes || !document.contains(tourNodes.card)) { tourEnd(); return; }
    if (step.target && !document.getElementById(step.target)) { tourEnd(); return; }
    // Next frame, not this instant: the swapped content is in the DOM but the
    // geometry it will occupy is not necessarily laid out yet, and a ring
    // measured a frame early lands on where the row USED to be. The code has to
    // keep the comment's promise, so this really is a frame later.
    if (window.requestAnimationFrame) window.requestAnimationFrame(tourPosition);
    else tourPosition();
  }

  function wizardChrome() { choiceFilters(); stepper(); }

  document.addEventListener("DOMContentLoaded", function () {
    refresh(); guards(); wizardChrome(); localTimes(); tourOffer();
  });
  document.body && document.body.addEventListener("htmx:afterSwap", function (event) {
    refresh();
    guards();
    wizardChrome();
    localTimes();
    tourAfterSwap();
    // The drawer's own swap: focus follows the panel that just appeared. Keyed
    // on the swapped target, so no other swap on the page moves focus.
    var target = event.detail && event.detail.target;
    if (target && target.id === "drawer-body" && target.firstChild) focusDrawer();
  });
  setInterval(guards, 1000);
})();
