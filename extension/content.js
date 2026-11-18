/*
 * Micnaga - content script (solo https://meet.google.com/*). Usa MicnagaDetect (detect.js).
 *
 * Manda al service worker {type:"micnaga", state, title, code}:
 *   - al cambiar el estado, el titulo o el codigo;
 *   - un latido cada HEARTBEAT_MS mientras dure la llamada (el host caduca la pestana si dejan
 *     de llegar: asi una pestana colgada o cerrada a la fuerza no deja la llamada "viva").
 * Un MutationObserver (que Chrome no estrangula en pestanas ocultas) reevalua con un maximo de
 * una vez cada MIN_INTERVAL_MS; el setInterval es solo respaldo y latido.
 */
(function () {
  "use strict";

  var HEARTBEAT_MS = 5000;
  var MIN_INTERVAL_MS = 500;
  var last = null;
  var lastSent = 0;
  var lastRun = 0;
  var trailing = null;

  function signature(s) {
    return s.state + "|" + (s.code || "") + "|" + (s.title || "");
  }

  function send(snapshot) {
    lastSent = Date.now();
    try {
      chrome.runtime.sendMessage({
        type: "micnaga",
        state: snapshot.state,
        title: snapshot.title,
        code: snapshot.code,
      });
    } catch (e) {
      // "Extension context invalidated" tras actualizar/recargar la extension: nada que hacer.
    }
  }

  function evaluate() {
    lastRun = Date.now();
    var current = MicnagaDetect.detect(document, location);
    var changed = last === null || signature(current) !== signature(last);
    last = current;
    if (changed || (current.state === "in_call" && Date.now() - lastSent >= HEARTBEAT_MS)) {
      send(current);
    }
  }

  function schedule() {
    var wait = MIN_INTERVAL_MS - (Date.now() - lastRun);
    if (wait <= 0) {
      evaluate();
    } else if (trailing === null) {
      trailing = setTimeout(function () {
        trailing = null;
        evaluate();
      }, wait);
    }
  }

  new MutationObserver(schedule).observe(document.documentElement, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ["aria-label"],
  });
  setInterval(schedule, 1000);
  window.addEventListener("pagehide", function () {
    send({ state: "not_in_call", title: null, code: null });
  });
  evaluate();
})();
