/* Condition evaluator — the browser twin of forms.py (FORM_ENGINE_SPEC §4).
 *
 * Python is authoritative at runtime; this only decides what the operator sees.
 * Both are held to tests/condition_vectors.json, executed on both sides in CI.
 *
 * Vanilla, no build step: loads as a plain <script> (window.Conditions) and as
 * a CommonJS module under node (the parity test).
 */
(function (root) {
  "use strict";

  // Missing ≡ absent key, "", [] or null/undefined. `false` and `0` are present.
  function present(value) {
    if (value === undefined || value === null || value === "") return false;
    if (Array.isArray(value) && value.length === 0) return false;
    return true;
  }

  // `===` already refuses true==1, which is what Python's forms.same() had to
  // be taught. Arrays compare element-wise, as they do on the Python side.
  function equal(a, b) {
    if (Array.isArray(a) && Array.isArray(b)) {
      return a.length === b.length && a.every(function (x, i) { return equal(x, b[i]); });
    }
    return a === b;
  }

  // Dotted path lookup; returns undefined for anything missing.
  //
  // hasOwnProperty, not `in`: `in` walks the prototype chain, so a condition on
  // a path like "constructor" or "toString" found a function on every object
  // and reported `exists: true` where Python's dict lookup says absent.
  function lookup(path, values) {
    var node = values || {};
    var parts = Array.isArray(path) ? path : String(path).split(".");
    for (var i = 0; i < parts.length; i++) {
      if (node === null || typeof node !== "object") return undefined;
      if (!Object.prototype.hasOwnProperty.call(node, parts[i])) return undefined;
      node = node[parts[i]];
    }
    return node;
  }

  function evaluateCondition(condition, root, person) {
    var scope = condition.scope === "person" ? person : root;
    var value = lookup(condition.path, scope);
    var has = present(value);

    switch (condition.operator) {
      case "exists":
        return has;
      case "is_true":
        return value === true;
      case "is_false":
        return value === false;
      case "eq":
        return has && equal(value, condition.value);
      case "in":
      case "not_in": {
        var candidates = condition.values || [];
        // An enumArray gate holds a list: membership is intersection.
        var hit = Array.isArray(value)
          ? value.some(function (v) { return candidates.some(function (c) { return equal(v, c); }); })
          : candidates.some(function (c) { return equal(value, c); });
        // not_in on an absent value is false: an unanswered gate never
        // activates its dependents.
        return has && (condition.operator === "in" ? hit : !hit);
      }
      default:
        return false;
    }
  }

  // The shared parity contract: {active, required} for one field.
  function evaluateField(spec, root, person) {
    var conditions = spec.conditions || [];
    var active = conditions.every(function (c) { return evaluateCondition(c, root, person); });
    var required = !!spec.required ||
      !!(spec.requiredWhen && evaluateCondition(spec.requiredWhen, root, person));
    return { active: active, required: required };
  }

  // Hide + disable inactive fields so they never submit, and keep the required
  // state honest as answers change (spec §4). The server re-decides both in
  // forms.active_fields / forms.validate; this is what the operator sees.
  function apply(form, root, person) {
    var nodes = form.querySelectorAll("[data-conditions], [data-required-when]");
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var spec = {
        conditions: JSON.parse(node.getAttribute("data-conditions") || "[]"),
        requiredWhen: JSON.parse(node.getAttribute("data-required-when") || "null"),
        required: node.getAttribute("data-required") === "true",
      };
      var state = evaluateField(spec, root, person);
      node.hidden = !state.active;
      var inputs = node.querySelectorAll("input, select, textarea");
      for (var j = 0; j < inputs.length; j++) {
        inputs[j].disabled = !state.active;
        // A hidden field is never required — the browser refuses to submit a
        // form containing a required control it cannot focus.
        var required = state.active && state.required;
        inputs[j].required = required;
        inputs[j].setAttribute("aria-required", required ? "true" : "false");
      }
    }
  }

  var api = {
    present: present,
    lookup: lookup,
    evaluateCondition: evaluateCondition,
    evaluateField: evaluateField,
    apply: apply,
  };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Conditions = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
