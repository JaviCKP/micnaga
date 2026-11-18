/*
 * Micnaga - heuristicas de deteccion de llamada en Google Meet.
 *
 * !!! FRAGIL !!!  Todo lo que depende del DOM de Meet esta AQUI y solo aqui. Meet cambia su DOM
 * y sus textos sin avisar; si la deteccion deja de funcionar, este es el unico fichero que hay
 * que tocar. Estas heuristicas NO se han verificado contra una llamada real de Meet en el
 * desarrollo (sin cuenta ni navegador con Meet disponible): se probaron con un DOM simulado
 * (tests/extension/test_manifest.py). Si fallan, el vigilante nativo sigue funcionando: la
 * extension solo puede ANADIR una llamada positiva (FileExtensionSource.available() es True solo
 * mientras hay una llamada detectada), asi que unas heuristicas rotas nunca silencian las senales
 * nativas.
 *
 * Contrato: detect(doc, loc) -> { state: "in_call" | "not_in_call", title: string|null,
 *                                  code: string|null }
 *
 *  - code:  del pathname, /abc-defg-hij (3-4-3 letras minusculas). Es una URL estable.
 *  - state: "in_call" si hay un boton de "salir de la llamada" (solo existe en la sala, no en el
 *           vestibulo previo ni en la pagina de inicio). Se reconoce por su aria-label, que Meet
 *           localiza: LEAVE_LABELS lista los textos conocidos; anade el de tu idioma si falta.
 *  - title: de document.title ("<nombre> - Meet" / "Meet - abc-defg-hij" ...) sin el sufijo/prefijo
 *           de producto; si queda solo el codigo, null.
 */
(function (root) {
  "use strict";

  var CODE_PATH_RE = /^\/([a-z]{3}-[a-z]{4}-[a-z]{3})(?:$|[/?#])/;

  // aria-label del boton de colgar, por idioma. FRAGIL: no verificado en vivo.
  var LEAVE_LABELS = [
    /leave call/i, // en
    /salir de la llamada/i, // es
    /abandonar la llamada/i, // es (variante)
    /quitter l.appel/i, // fr
    /anruf verlassen/i, // de
    /sair da chamada/i, // pt
    /esci dalla chiamata/i, // it
  ];

  function meetingCode(pathname) {
    var match = CODE_PATH_RE.exec(pathname || "");
    return match ? match[1] : null;
  }

  function hasLeaveButton(doc) {
    var nodes = doc.querySelectorAll("button[aria-label], [role='button'][aria-label]");
    for (var i = 0; i < nodes.length; i++) {
      var label = nodes[i].getAttribute("aria-label") || "";
      for (var j = 0; j < LEAVE_LABELS.length; j++) {
        if (LEAVE_LABELS[j].test(label)) return true;
      }
    }
    return false;
  }

  function cleanTitle(rawTitle, code) {
    var title = String(rawTitle || "").replace(/\s+/g, " ").trim();
    // quita "Meet" / "Google Meet" y separadores en los extremos
    title = title
      .replace(/^(?:google\s+)?meet\s*[-–—|:]\s*/i, "")
      .replace(/\s*[-–—|:]\s*(?:google\s+)?meet$/i, "")
      .trim();
    if (!title || title.toLowerCase() === "meet" || title.toLowerCase() === "google meet") {
      return null;
    }
    if (code && title.toLowerCase() === code) return null;
    if (CODE_PATH_RE.test("/" + title.toLowerCase())) return null; // "abc-defg-hij"
    return title.slice(0, 200);
  }

  function detect(doc, loc) {
    var code = meetingCode(loc && loc.pathname);
    var inCall = code !== null && hasLeaveButton(doc);
    return {
      state: inCall ? "in_call" : "not_in_call",
      title: inCall ? cleanTitle(doc.title, code) : null,
      code: code,
    };
  }

  var api = {
    detect: detect,
    meetingCode: meetingCode,
    hasLeaveButton: hasLeaveButton,
    cleanTitle: cleanTitle,
    LEAVE_LABELS: LEAVE_LABELS,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.MicnagaDetect = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
