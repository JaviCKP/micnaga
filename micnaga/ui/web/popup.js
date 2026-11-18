/*
 * Popup controller. Python drives it with window.micnaga.popup.show/accept/dismiss/end and gets
 * events back through api.popup_* (see micnaga/ui/bridge.py:PopupApi).
 *
 *  show      -> rise 16 px + fade, 220 ms
 *  Grabar    -> check for 300 ms, then the card shrinks into the pill (400 ms)
 *  Ahora no  -> down + fade, 180 ms
 *  timeout   -> morphs into the pill labelled «provisional»  (or leaves, if on_timeout=discard)
 *  end       -> fade, 180 ms (meeting ended before an answer)
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const lift = $("lift"), card = $("card");
  const bRec = $("b-rec"), bNo = $("b-no"), notice = $("notice"), live = $("live");
  $("b-rec").querySelector(".swap-b").innerHTML = mic.icon("check");
  document.addEventListener("DOMContentLoaded", () => mic.applyIcons());
  mic.applyIcons();

  const meters = { mic: mic.meter($("m-mic")), sys: mic.meter($("m-sys")) };
  const countdownBar = $("countdown");
  const cdFill = countdownBar.querySelector("i");

  const S = {
    phase: "hidden", total: 15, remaining: 15, hovering: false, onTimeout: "provisional",
    lastTick: 0, raf: 0, shownAt: 0, ariaT: 0, timers: [],
  };
  const later = (fn, ms) => { const id = setTimeout(fn, ms); S.timers.push(id); return id; };
  const clearTimers = () => { S.timers.forEach(clearTimeout); S.timers = []; };

  const poll = mic.pollLevels((m, s) => {
    meters.mic.set(mic.levelToUi(m));
    meters.sys.set(mic.levelToUi(s));
    const t = performance.now();
    if (t - S.ariaT > 500) {          // screen readers get 2 updates/s, not 20
      S.ariaT = t;
      $("m-mic").setAttribute("aria-valuenow", String(Math.round(mic.levelToUi(m) * 100)));
      $("m-sys").setAttribute("aria-valuenow", String(Math.round(mic.levelToUi(s) * 100)));
    }
  });

  function setPhase(p) { S.phase = p; lift.dataset.phase = p; }

  // ---- countdown --------------------------------------------------------------------
  function tick(t) {
    S.raf = 0;
    if (S.phase !== "shown") return;
    const dt = S.lastTick ? (t - S.lastTick) / 1000 : 0;
    S.lastTick = t;
    if (!S.hovering && !S.frozen) S.remaining = Math.max(0, S.remaining - dt);
    cdFill.style.setProperty("--p", (S.remaining / S.total).toFixed(4));
    countdownBar.setAttribute("aria-valuenow", String(Math.round((S.remaining / S.total) * 100)));
    if (S.remaining <= 0) { timeout(); return; }
    S.raf = requestAnimationFrame(tick);
  }
  function startCountdown() {
    S.lastTick = 0;
    if (!S.raf) S.raf = requestAnimationFrame(tick);
  }
  function stopCountdown() { if (S.raf) cancelAnimationFrame(S.raf); S.raf = 0; }

  card.addEventListener("pointerenter", () => hover(true));
  card.addEventListener("pointerleave", () => hover(false));
  function hover(on) {
    if (S.phase !== "shown" || S.hovering === on) return;
    S.hovering = on;
    card.dataset.hover = String(on);
    mic.send("popup_hover", on);
  }
  // a deliberate click inside the card (not on a control) asks Python to give it keyboard focus
  card.addEventListener("pointerdown", (e) => {
    if (S.phase === "shown" && !e.target.closest("button")) { mic.send("popup_activate"); bRec.focus({ preventScroll: true }); }
  });

  // ---- outcomes ---------------------------------------------------------------------
  function accept() {
    if (S.phase !== "shown") return;
    stopCountdown();
    setPhase("confirming");
    live.textContent = mic.t("popup.recording_started");
    mic.send("popup_decide", "record");
    later(() => morph(false), 300);
  }
  function dismiss() {
    if (S.phase !== "shown") return;
    stopCountdown();
    mic.send("popup_decide", "dismiss");
    leave("leaving");
  }
  function timeout() {
    if (S.phase !== "shown") return;
    stopCountdown();
    if (S.onTimeout === "discard") {
      mic.send("popup_decide", "timeout");
      leave("leaving");
    } else {
      mic.send("popup_decide", "timeout");
      live.textContent = mic.t("popup.provisional_started");
      morph(true);
    }
  }
  function end() {        // the meeting ended before an answer
    if (S.phase === "hidden" || S.phase === "done") return;
    stopCountdown();
    leave("fading");
  }
  function leave(kind) {
    setPhase(kind);
    poll.stop();
    later(() => { setPhase("done"); mic.send("popup_closed", kind); }, mic.dur(180) + 10);
  }
  function morph(provisional) {
    $("echo-time").textContent = mic.fmtClock((performance.now() - S.shownAt) / 1000);
    card.dataset.provisional = String(provisional);
    setPhase("morphing");
    card.dataset.morph = "true";
    mic.send("popup_morph", provisional);
    later(() => { setPhase("done"); poll.stop(); mic.send("popup_closed", "morphed"); }, mic.dur(400) + 20);
  }

  bRec.addEventListener("click", accept);
  bNo.addEventListener("click", dismiss);
  $("notice-x").addEventListener("click", () => {
    notice.classList.add("gone");
    mic.send("popup_notice_dismissed");
    bRec.focus({ preventScroll: true });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); dismiss(); }
    else if (e.key === "Enter" && document.activeElement !== bNo && document.activeElement !== $("notice-x")) { e.preventDefault(); accept(); }
  });

  // ---- Python -> JS -----------------------------------------------------------------
  const api = (window.micnaga = window.micnaga || {});
  api.popup = {
    /** {title, code, timeoutS, onTimeout, remindNotice} */
    show(o) {
      o = o || {};
      clearTimers();
      stopCountdown();
      S.total = S.remaining = Math.max(1, Number(o.timeoutS) || 15);
      S.onTimeout = o.onTimeout === "discard" ? "discard" : "provisional";
      S.hovering = false;
      S.shownAt = performance.now();
      card.dataset.hover = "false";
      card.dataset.morph = "false";
      $("p-meet").textContent = o.title || "";
      const code = $("p-code");
      code.textContent = o.code || "";
      code.hidden = !o.code;
      notice.classList.toggle("gone", o.remindNotice === false);
      meters.mic.reset(); meters.sys.reset();
      cdFill.style.setProperty("--p", "1");
      setPhase("hidden");
      void lift.offsetWidth;                 // commit the hidden pose, then animate to rest
      requestAnimationFrame(() => {
        setPhase("shown");
        poll.start();
        startCountdown();
        bRec.focus({ preventScroll: true, focusVisible: false });
        live.textContent = mic.t("popup.announce", { title: o.title || "" });
      });
    },
    accept, dismiss, end,
    setLevels(m, s) { meters.mic.raw(mic.levelToUi(m)); meters.sys.raw(mic.levelToUi(s)); },
    get phase() { return S.phase; },
  };

  // ---- standalone / mock -------------------------------------------------------------
  mic.mock.levels = () => {
    const t = performance.now() / 1000;
    return [Math.max(0, 0.12 + 0.12 * Math.sin(t * 5.3) * Math.sin(t * 1.7)), Math.max(0, 0.06 + 0.07 * Math.sin(t * 3.1 + 1.3) * Math.sin(t * 0.9))];
  };
  mic.i18nReady.then(() => {
    if (!mic.isHost) {
      const q = mic.params;
      api.popup.show({
        title: q.get("title") || "Revisión semanal", code: q.get("code") || "abc-defg-hij",
        timeoutS: Number(q.get("timeout") || 15), onTimeout: q.get("on_timeout") || "provisional",
        remindNotice: q.get("notice") !== "0",
      });
      if (q.get("levels")) {
        const [a, b] = q.get("levels").split(",").map(Number);
        poll.stop(); api.popup.setLevels(a, b);
      }
      if (q.get("progress")) {          // freeze the countdown at a fraction (screenshots)
        S.frozen = true;
        S.remaining = S.total * Number(q.get("progress"));
      }
    }
  });
})();
