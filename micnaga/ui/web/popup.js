/*
 * Popup controller. Python drives it with window.micnaga.popup.show/accept/dismiss/end/expire
 * and gets events back through api.popup_* (see micnaga/ui/bridge.py:PopupApi).
 *
 *  show      -> rise 16 px + fade, 220 ms
 *  Grabar    -> check for 300 ms, then the card shrinks into the pill (400 ms)
 *  Ahora no  -> down + fade, 180 ms
 *  timeout   -> morphs into the pill labelled «provisional» (or leaves, if on_timeout=discard)
 *  end       -> fade, 180 ms (meeting ended before an answer)
 *
 * Countdown authority: when Python exposes `popup_state()` (the watcher's countdown), the bar
 * only draws it and the popup NEVER decides a timeout itself: Python calls expire(). Without
 * it (older host, tests, standalone page) the local countdown below is the fallback.
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const lift = $("lift"), card = $("card");
  const bRec = $("b-rec"), bNo = $("b-no"), notice = $("notice"), live = $("live");
  bRec.querySelector(".swap-b").innerHTML = mic.icon("check");
  mic.applyIcons();

  const waves = { mic: mic.wave($("m-mic"), { bar: 2, gap: 2.5, min: 2 }), sys: mic.wave($("m-sys"), { bar: 2, gap: 2.5, min: 2 }) };
  const countdownBar = $("countdown");
  const cdFill = countdownBar.querySelector("i");

  const S = {
    phase: "hidden", total: 15, remaining: 15, hovering: false, onTimeout: "provisional",
    lastTick: 0, raf: 0, shownAt: 0, ariaT: 0, timers: [], frozen: false,
    authority: "local",            // "python" when popup_state() answers
    remotePaused: false, pollTimer: 0, pollBusy: false, showSeq: 0,
  };
  const later = (fn, ms) => { const id = setTimeout(fn, ms); S.timers.push(id); return id; };
  const clearTimers = () => { S.timers.forEach(clearTimeout); S.timers = []; };

  const poll = mic.pollLevels((m, s) => {
    const um = mic.levelToUi(m), us = mic.levelToUi(s);
    waves.mic.push(um);
    waves.sys.push(us);
    const t = performance.now();
    if (t - S.ariaT > 500) {          // screen readers get 2 updates/s, not 20
      S.ariaT = t;
      $("m-mic").setAttribute("aria-valuenow", String(Math.round(um * 100)));
      $("m-sys").setAttribute("aria-valuenow", String(Math.round(us * 100)));
    }
  });

  function setPhase(p) { S.phase = p; lift.dataset.phase = p; }

  // ---- countdown (drawn every frame; decided by Python when it can) -------------------
  function paintBar() {
    const p = S.total > 0 ? Math.max(0, Math.min(1, S.remaining / S.total)) : 0;
    cdFill.style.setProperty("--p", p.toFixed(4));
    countdownBar.setAttribute("aria-valuenow", String(Math.round(p * 100)));
  }
  function tick(t) {
    S.raf = 0;
    if (S.phase !== "shown") return;
    const dt = S.lastTick ? (t - S.lastTick) / 1000 : 0;
    S.lastTick = t;
    const held = S.hovering || S.frozen || (S.authority === "python" && S.remotePaused);
    if (!held) S.remaining = Math.max(0, S.remaining - dt);
    paintBar();
    if (S.remaining <= 0 && S.authority === "local") { timeout(); return; }
    S.raf = requestAnimationFrame(tick);
  }
  function startCountdown() {
    S.lastTick = 0;
    if (!S.raf) S.raf = requestAnimationFrame(tick);
  }
  function stopCountdown() {
    if (S.raf) cancelAnimationFrame(S.raf);
    S.raf = 0;
    clearInterval(S.pollTimer);
    S.pollTimer = 0;
  }

  /** Read Python's countdown. Returns false when the host has no (usable) popup_state. */
  async function readRemote() {
    if (S.pollBusy) return true;
    S.pollBusy = true;
    try {
      const r = await mic.call("popup_state");
      if (!r || typeof r !== "object" || typeof r.remaining_s !== "number" || r.authoritative === false) return false;
      if (typeof r.total_s === "number" && r.total_s > 0) S.total = r.total_s;
      S.remaining = Math.max(0, r.remaining_s);
      S.remotePaused = !!r.paused;
      card.dataset.held = String(S.remotePaused);
      return true;
    } catch (e) {
      return false;
    } finally {
      S.pollBusy = false;
    }
  }
  function hostHasPopupState() {
    if (mic.isHost) return !!(window.pywebview && window.pywebview.api && window.pywebview.api.popup_state);
    return mic.params.get("bridge") === "http";      // dev bridge: try it, fall back on error
  }
  async function negotiateAuthority(seq) {
    if (!hostHasPopupState()) return;
    const ok = await readRemote();
    if (seq !== S.showSeq || S.phase !== "shown" || !ok) return;
    S.authority = "python";
    paintBar();
    S.pollTimer = setInterval(() => { if (S.phase === "shown") readRemote(); }, 250);
  }

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
    later(() => morph(false), mic.motion ? 300 : 0);
  }
  function dismiss() {
    if (S.phase !== "shown") return;
    stopCountdown();
    mic.send("popup_decide", "dismiss");
    leave("leaving");
  }
  /** Local fallback only: the popup decided the timeout itself. */
  function timeout() {
    if (S.phase !== "shown") return;
    stopCountdown();
    mic.send("popup_decide", "timeout");
    settleTimeout();
  }
  /** Python decided the timeout (watcher): play the outcome, do not report a decision. */
  function expire() {
    if (S.phase !== "shown") return;
    stopCountdown();
    settleTimeout();
  }
  function settleTimeout() {
    if (S.onTimeout === "discard") { leave("leaving"); return; }
    live.textContent = mic.t("popup.provisional_started");
    morph(true);
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
    /** {title, code, timeoutS, onTimeout, remindNotice} — no code = a generic app (Teams, Zoom…) */
    show(o) {
      o = o || {};
      clearTimers();
      stopCountdown();
      S.showSeq++;
      S.authority = "local";
      S.remotePaused = false;
      S.total = S.remaining = Math.max(1, Number(o.timeoutS) || 15);
      S.onTimeout = o.onTimeout === "discard" ? "discard" : "provisional";
      S.hovering = false;
      S.shownAt = performance.now();
      card.classList.add("reset");          // a reused window: snap out of the last morph
      card.dataset.hover = "false";
      card.dataset.held = "false";
      card.dataset.morph = "false";
      card.dataset.provisional = "false";
      void card.offsetWidth;
      card.classList.remove("reset");
      const generic = !o.code;
      card.dataset.kind = generic ? "app" : "meet";
      $("p-ico").innerHTML = mic.icon(generic ? "app" : "video");
      const meet = $("p-meet");
      meet.textContent = o.title || "";
      meet.title = o.code ? (o.title ? o.title + " · " : "") + o.code : (o.title || "");
      const code = $("p-code");
      code.textContent = o.code || "";
      code.hidden = !o.code;
      notice.classList.toggle("gone", o.remindNotice === false);
      waves.mic.reset(); waves.sys.reset();
      paintBar();
      setPhase("hidden");
      void lift.offsetWidth;                 // commit the hidden pose, then animate to rest
      const seq = S.showSeq;
      requestAnimationFrame(() => {
        if (seq !== S.showSeq) return;
        setPhase("shown");
        poll.start();
        startCountdown();
        negotiateAuthority(seq);
        bRec.focus({ preventScroll: true, focusVisible: false });
        live.textContent = mic.t("popup.announce", { title: o.title || "" });
      });
    },
    accept, dismiss, end, expire,
    setLevels(m, s) { waves.mic.fill(mic.levelToUi(m)); waves.sys.fill(mic.levelToUi(s)); },
    get phase() { return S.phase; },
    get authority() { return S.authority; },
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
        title: q.get("title") || "Revisión semanal",
        code: q.get("generic") === "1" ? "" : (q.get("code") || "abc-defg-hij"),
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
      if (q.get("phase") === "confirming") setTimeout(() => { stopCountdown(); setPhase("confirming"); }, 400);
    }
  });
})();
