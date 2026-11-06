/* ============================================================
   path-format.js — long-path display shared by the shell and task editor.

   The filesystem value always stays in data-full-path; the visible input
   only carries a middle-elided label while it is not focused. Focusing the
   field restores the full value for editing, and blur re-elides it.
   Classic script so both the module shell and taskedit.html can use it.
   ============================================================ */
(function (root) {
  "use strict";

  function elideMiddle(text, max) {
    const value = String(text == null ? "" : text);
    const limit = Math.max(8, Math.floor(Number(max) || 0));
    if (value.length <= limit) return value;
    const available = Math.max(3, limit - 1);
    let head = Math.ceil(available * 0.42);
    let tail = available - head;
    if (/^[A-Za-z]:/.test(value)) head = Math.max(3, head);
    if (tail < 4) {
      tail = Math.min(4, available - 1);
      head = Math.max(1, available - tail);
    }
    return value.slice(0, head) + "…" + value.slice(-tail);
  }

  function limitForViewport(win) {
    const w = win && Number(win.innerWidth);
    return w > 0 && w <= 767 ? 24 : 46;
  }

  function setPath(input, full, max) {
    if (!input) return "";
    const value = String(full == null ? "" : full);
    input.dataset.fullPath = value;
    input.title = value;
    input.value = elideMiddle(
      value,
      max || limitForViewport(typeof window !== "undefined" ? window : null),
    );
    return input.value;
  }

  function fullPath(input) {
    if (!input) return "";
    return input.dataset && input.dataset.fullPath !== undefined
      ? input.dataset.fullPath
      : input.value;
  }

  function bind(input, opts) {
    if (!input || input.dataset.pathBound === "1") return input;
    const o = opts || {};
    input.dataset.pathBound = "1";
    if (o.full !== undefined) setPath(input, o.full, o.max);
    input.addEventListener("focus", function () {
      input.value = fullPath(input);
    });
    input.addEventListener("input", function () {
      if (document.activeElement === input) input.dataset.fullPath = input.value;
    });
    input.addEventListener("blur", function () {
      setPath(input, fullPath(input), o.max);
    });
    return input;
  }

  root.TVGPath = {
    elideMiddle: elideMiddle,
    setPath: setPath,
    fullPath: fullPath,
    bind: bind,
    limitForViewport: limitForViewport,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
