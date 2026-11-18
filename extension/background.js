/*
 * Micnaga - service worker. Reenvia los mensajes de los content scripts al host nativo
 * "es.micnaga.host" (native messaging: no hay red). Mientras el puerto nativo esta abierto Chrome
 * mantiene vivo el service worker (Chrome >= 105).
 *
 * Mensaje al host: {state: "in_call" | "not_in_call", title, code, tab}  (tab = id de pestana).
 */
"use strict";

var HOST = "es.micnaga.host";
var port = null;

function connect() {
  if (port !== null) return port;
  try {
    port = chrome.runtime.connectNative(HOST);
    port.onDisconnect.addListener(function () {
      // Host no instalado o cerrado: se reintenta con el siguiente mensaje.
      void chrome.runtime.lastError;
      port = null;
    });
  } catch (e) {
    port = null;
  }
  return port;
}

function post(message, connectIfNeeded) {
  var p = connectIfNeeded ? connect() : port;
  if (p === null) return;
  try {
    p.postMessage(message);
  } catch (e) {
    port = null;
  }
}

// Mensajes de los content scripts de Meet: aqui SI se abre el puerto (arranca el host).
chrome.runtime.onMessage.addListener(function (msg, sender) {
  if (!msg || msg.type !== "micnaga" || !sender || !sender.tab) return;
  post({ state: msg.state, title: msg.title, code: msg.code, tab: sender.tab.id }, true);
});

// Pestana cerrada: su llamada termina. Solo se avisa si el puerto YA esta abierto: cerrar una
// pestana cualquiera no debe arrancar el host.
// (No hay manejador de tabs.onUpdated: sin el permiso "tabs", changeInfo.url solo aparece para
// URLs de Meet, asi que nunca avisaria de una navegacion fuera de Meet. Esa navegacion la cubre el
// "pagehide" del content script y, en ultimo caso, la caducidad por falta de latido en el host.)
chrome.tabs.onRemoved.addListener(function (tabId) {
  post({ state: "not_in_call", title: null, code: null, tab: tabId }, false);
});
