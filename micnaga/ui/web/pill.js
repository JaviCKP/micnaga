/* Recording pill controller. Python drives it through window.micnaga.pill.*
 *
 * The pill is also the surface that shows the «¿Estás en una reu?» card (micnaga.surface).
 * Two states, no morph (DECISIONS §29):
 *   pill -> expanding (content fades out; Python resizes the window) -> card (fades in)
 *   card -> collapsing (content fades out; Python resizes the window) -> pill (fades in)
 * popup.js runs the card itself and asks the surface to expand / collapse.
 *
 * In the app (?fit=1) the window is OPAQUE and Python sizes the native window to the visible
 * shape (pill, wide pill, undo chip, card): the shape fills the window. Standalone (no fit)
 * the pill and the card sit in the page and swap with the same fade.
 */
(function () {
  "use strict";
  const FIT = mic.params.get("fit") === "1";
  if (FIT) document.documentElement.dataset.fit = "window";
  const el = document.getElementById("pill");
  const timeEl = document.getElementById("time");
  const live = document.getElementById("live");
  const bPause = document.getElementById("b-pause");
  const bMarker = document.getElementById("b-marker");
  const swap = document.getElementById("pause-swap");
  swap.innerHTML = mic.icon("pause", "a") + mic.icon("play", "b");
  mic.applyIcons();

  const meters = { mic: mic.wave(document.getElementById("pm-mic"), { bar: 2, gap: 2, min: 1.5, fade: 0.2 }), sys: mic.wave(document.getElementById("pm-sys"), { bar: 2, gap: 2, min: 1.5, fade: 0.2 }) };

  const S = { elapsed: 0, since: performance.now(), paused: false, running: false };
  let tickTimer = 0;
  function now() { return S.elapsed + (S.running && !S.paused ? (performance.now() - S.since) / 1000 : 0); }
  function paint() { timeEl.textContent = mic.fmtClock(now()); }
  function startLoop() { if (!tickTimer) tickTimer = setInterval(paint, 250); }
  function stopLoop() { clearInterval(tickTimer); tickTimer = 0; }

  const poll = mic.pollLevels((m, s) => {
    if (S.paused) { meters.mic.push(0); meters.sys.push(0); return; }
    meters.mic.push(mic.levelToUi(m));
    meters.sys.push(mic.levelToUi(s));
  });

  /** Apply a style change without letting it animate (layout, re-show). */
  let snapping = 0;
  function snap(fn) {
    snapping++;
    el.style.transition = "none";
    try {
      fn();
    } finally {
      if (--snapping === 0) {
        void el.offsetWidth;
        el.style.transition = "";
      }
    }
  }

  // ---- the surface: pill <-> card (two states, no morph) ---------------------------------
  // The content fades out, the window takes the other size while it is hidden (Python:
  // surface_resize -> micnaga.surface.resized()), and the content fades back in. Nothing ever
  // shows one size of window with the other state's content. Reduced motion: instant.
  const FADE_MS = 160;
  const SURF = { phase: "pill", timers: [], fade: null, sized: false };
  const sLater = (fn, ms) => { const id = setTimeout(fn, ms); SURF.timers.push(id); return id; };
  function clearSurf() { SURF.timers.forEach(clearTimeout); SURF.timers = []; }
  function setSurface(p) {
    SURF.phase = p;
    snap(() => { el.dataset.surface = p; });   // the shape never tweens between the two states
    // drag the pill, never the card (pywebview drags from any element inside the window)
    el.classList.toggle("pywebview-drag-region", p === "pill");
  }
  function cancelFade() { if (SURF.fade) { SURF.fade.cancel(); SURF.fade = null; } }
  /** Hide (on) or show the content. Instant when asked or when motion is off. */
  function veil(on, instant) {
    const v = on ? "on" : "off";
    if (!instant && mic.motion) { el.dataset.veil = v; return; }
    const kids = Array.from(el.children);   // the fade lives on the children
    kids.forEach((k) => { k.style.transition = "none"; });
    el.dataset.veil = v;
    void el.offsetWidth;
    kids.forEach((k) => { k.style.transition = ""; });
  }
  /** After the page has painted (two frames), with a timer fallback: a hidden window gives no
   *  frames and the card must still appear. */
  function afterPaint(fn) {
    let done = false;
    const go = () => { if (!done) { done = true; fn(); } };
    requestAnimationFrame(() => requestAnimationFrame(go));
    SURF.timers.push(setTimeout(go, 80));
  }
  function blurInside() {
    const a = document.activeElement;
    if (a && a !== document.body && el.contains(a)) a.blur();
  }
  /** The window has the card's size now: show the card. */
  function openCard() {
    clearSurf();
    setSurface("card");
    afterPaint(() => { veil(false); mic.send("surface_phase", "card"); });
  }
  /** Become the card. Returns 0: popup.js may show the card content at once (it is veiled). */
  function expand() {
    if (SURF.phase === "expanding" || SURF.phase === "card") return 0;
    clearSurf();
    cancelFade();
    clearTimeout(leaveTimer);
    el.dataset.hover = "false";
    poll.stop();
    const sized = SURF.sized || !FIT;   // standalone: no window to resize
    SURF.sized = false;
    if (sized) { veil(true, true); openCard(); return 0; }
    setSurface("expanding");
    veil(true);
    sLater(() => afterPaint(() => mic.send("surface_resize", "card")), mic.dur(FADE_MS));
    return 0;
  }
  /** Back to the pill: the card fades out. Returns the ms until the window can shrink. */
  function collapse() {
    clearSurf();
    blurInside();
    if (SURF.phase === "pill") return 0;
    mic.send("surface_phase", "collapsing");
    setSurface("collapsing");
    veil(true);
    const ms = mic.dur(FADE_MS);
    sLater(() => {
      setSurface("pill");
      if (S.running) poll.start();
      if (!FIT) veil(false);   // standalone: nothing to resize, fade the pill in here
    }, ms);
    return ms;
  }
  /** Python: the window now has the size of the current state; fade the content in. */
  function resized() {
    if (SURF.phase === "expanding") { openCard(); return; }
    if (SURF.phase === "collapsing") { clearSurf(); setSurface("pill"); if (S.running) poll.start(); }
    afterPaint(() => veil(false));
  }
  /** Python closed the card without waiting for the JS: be a pill now (idempotent). The
   *  content stays hidden until Python has resized the window (resized()). */
  function settle() {
    if (SURF.phase === "pill") return;
    clearSurf();
    blurInside();
    veil(true, true);
    setSurface("pill");
    if (S.running) poll.start();
  }
  /** The recording ended: fade out (Python hides the window afterwards). */
  function vanish() {
    clearSurf();
    cancelFade();
    blurInside();
    SURF.fade = el.animate([{ opacity: getComputedStyle(el).opacity }, { opacity: 0 }], { duration: mic.dur(160) || 1, easing: "ease", fill: "forwards" });
  }

  let leaveTimer = 0;
  function setHover(on) {
    clearTimeout(leaveTimer);
    if (on) {
      if (SURF.phase !== "pill" || el.dataset.mode === "undo") return;
      el.dataset.hover = "true";
      mic.send("pill_hover", true);
    } else {
      el.dataset.hover = "false";
      // shrink the clickable region only after the capsule has narrowed
      leaveTimer = setTimeout(() => mic.send("pill_hover", false), mic.dur(240) + 20);
    }
  }
  el.addEventListener("pointerenter", () => setHover(true));
  el.addEventListener("pointerleave", () => { if (!el.matches(":focus-within")) setHover(false); });
  el.addEventListener("focusout", () => { if (!el.matches(":hover")) setHover(false); });
  // buttons must not start a window drag
  el.querySelectorAll(".pb").forEach((b) => b.addEventListener("mousedown", (e) => e.stopPropagation()));

  bMarker.addEventListener("click", () => {
    bMarker.classList.remove("flash");
    void bMarker.offsetWidth;
    bMarker.classList.add("flash");
    live.textContent = mic.t("pill.marker_added", { t: mic.fmtClock(now()) });
    mic.send("pill_marker");
  });
  bPause.addEventListener("click", () => mic.send("pill_toggle_pause"));
  document.getElementById("b-save").addEventListener("mousedown", (e) => e.stopPropagation());
  document.getElementById("b-save").addEventListener("click", () => mic.send("pill_save"));

  // ---- «Descartada · Deshacer» (la cuenta atras real es la del vigilante) ---------------
  const undoBar = document.getElementById("undo-bar");
  const bUndo = document.getElementById("b-undo");
  const U = { timer: 0, until: 0, total: 1 };
  bUndo.addEventListener("mousedown", (e) => e.stopPropagation());
  bUndo.addEventListener("click", () => { mic.send("pill_undo"); undoEnd(); });
  function undoTick() {
    const left = Math.max(0, U.until - performance.now());
    undoBar.style.setProperty("--p", (left / U.total).toFixed(4));
    if (left <= 0) { clearInterval(U.timer); U.timer = 0; }
  }
  function undo(o) {
    o = o || {};
    const s = Math.max(1, Number(o.seconds) || 8);
    if (SURF.phase !== "pill") settle();
    cancelFade();
    U.total = s * 1000;
    U.until = performance.now() + U.total;
    el.dataset.mode = "undo";
    el.dataset.hover = "false";
    clearInterval(U.timer);
    U.timer = setInterval(undoTick, 50);
    undoTick();
    live.textContent = mic.t("undo.announce");
  }
  function undoEnd() {
    clearInterval(U.timer);
    U.timer = 0;
    el.dataset.mode = "pill";
  }
  document.getElementById("b-stop").addEventListener("click", () => mic.send("pill_stop"));

  function applyPause(paused) {
    S.elapsed = now();
    S.since = performance.now();
    S.paused = paused;
    el.dataset.state = paused ? "paused" : "recording";
    bPause.setAttribute("aria-pressed", String(paused));
    const label = mic.t(paused ? "pill.resume" : "pill.pause");
    bPause.setAttribute("aria-label", label);
    bPause.title = label;
    live.textContent = mic.t(paused ? "pill.paused_notice" : "pill.resumed_notice");
    paint();
  }

  function layout(o) {
    if (!o) return;
    snap(() => {
      if (typeof o.px === "number") el.style.setProperty("--px", o.px + "px");
      if (typeof o.py === "number") el.style.setProperty("--py", o.py + "px");
      if (typeof o.cardL === "number") el.style.setProperty("--card-l", o.cardL + "px");
    });
  }

  const api = (window.micnaga = window.micnaga || {});
  api.pill = {
    /** Start/refresh: {elapsed seconds so far, paused?, provisional?, layout?, enter?} */
    show(o) {
      o = o || {};
      undoEnd();
      clearSurf();
      cancelFade();
      layout(o.layout);
      snap(() => { setSurface("pill"); el.dataset.hover = "false"; el.dataset.veil = "off"; });
      S.elapsed = o.elapsed || 0;
      S.since = performance.now();
      S.running = true;
      el.dataset.provisional = String(!!o.provisional);
      applyPause(!!o.paused);
      startLoop();
      poll.start();
      paint();
    },
    layout,
    sync(elapsed) { S.elapsed = elapsed; S.since = performance.now(); paint(); },
    setPaused(p) { if (p !== S.paused) applyPause(p); },
    setProvisional(p) { el.dataset.provisional = String(!!p); },
    /** Secretaria: provisional title (shown on hover, next to the timer). */
    setTitle(t) {
      const node = document.getElementById("ptitle");
      t = String(t || "").trim();
      node.textContent = t;
      node.hidden = !t;
      el.dataset.hasTitle = String(!!t);
      if (t) el.setAttribute("title", t); else el.removeAttribute("title");
    },
    stop() { S.running = false; stopLoop(); poll.stop(); meters.mic.reset(); meters.sys.reset(); api.pill.setTitle(""); },
    settle,
    vanish,
    /** for screenshots / standalone */
    setLevels(m, s) { meters.mic.fill(mic.levelToUi(m)); meters.sys.fill(mic.levelToUi(s)); },
    setHover,
    undo,
    undoEnd,
  };
  api.surface = {
    expand,
    collapse,
    settle,
    resized,
    /** Python: the window already has the card's size (a window that was hidden): the next
     *  expand() shows the card without asking for a resize. */
    prepare(sized) { SURF.sized = !!sized; },
    get phase() { return SURF.phase; },
  };

  // ---- DPI diagnostics: CSS viewport vs the physical window (Python logs it) -----------
  function reportMetrics() {
    if (mic.isHost) mic.send("surface_metrics", innerWidth, innerHeight, window.devicePixelRatio || 1);
  }
  let metricsTimer = 0;
  addEventListener("resize", () => { clearTimeout(metricsTimer); metricsTimer = setTimeout(reportMetrics, 150); });
  reportMetrics();

  // ---- standalone / mock -------------------------------------------------------------
  mic.mock.levels = () => {
    const t = performance.now() / 1000;
    return [Math.max(0, 0.18 + 0.15 * Math.sin(t * 3.1) * Math.sin(t * 1.3)), Math.max(0, 0.1 + 0.1 * Math.sin(t * 2.2 + 1))];
  };
  mic.mock.pill_toggle_pause = () => api.pill.setPaused(!S.paused);

  mic.i18nReady.then(() => {
    if (!mic.isHost) {
      const q = mic.params;
      api.pill.show({ elapsed: Number(q.get("elapsed") || 754), paused: q.get("paused") === "1", provisional: q.get("provisional") === "1" });
      if (q.get("undo") === "1") api.pill.undo({ seconds: 8 });
      if (q.get("title")) api.pill.setTitle(q.get("title"));
      if (q.get("hover") === "1") setHover(true);
      if (q.get("levels")) {
        const [a, b] = q.get("levels").split(",").map(Number);
        poll.stop();
        api.pill.setLevels(a, b);
      }
    }
  });
})();
