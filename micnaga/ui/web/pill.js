/* Recording pill controller. Python drives it through window.micnaga.pill.*
 *
 * The pill is also the surface the «¿Estás en una reu?» card grows from (micnaga.surface):
 *   pill -> expanding (300 ms) -> card -> collapsing (40 + 240 ms) -> pill
 * popup.js runs the card itself and asks the surface to expand / collapse. Python never resizes
 * the window: it is a fixed transparent stage, and its region is widened before the shape grows
 * and narrowed after it shrinks (no round-trip during the animation). The shape is a CSS clip
 * (pill.css): the morph runs without layout, and the JS does no per-frame work.
 */
(function () {
  "use strict";
  const el = document.getElementById("pill");
  const timeEl = document.getElementById("time");
  const live = document.getElementById("live");
  const bPause = document.getElementById("b-pause");
  const bMarker = document.getElementById("b-marker");
  const swap = document.getElementById("pause-swap");
  swap.innerHTML = mic.icon("pause", "a") + mic.icon("play", "b");
  mic.applyIcons();

  // Load the card's fonts now. Its text is first drawn while the shape grows, and loading a
  // face then (font-display: block) stalled that frame by 70-110 ms (measured in WebView2).
  if (document.fonts && document.fonts.load) {
    ['700 1em "Barlow Semi Condensed"', '400 1em "Barlow"', '600 1em "Barlow"', '500 1em "JetBrains Mono"']
      .forEach((f) => document.fonts.load(f, "¿ESTÁS 0:9").catch(() => {}));
  }

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

  /** Apply a style change without letting it animate (layout, re-show). Re-entrant. */
  let snapping = 0;
  function snap(fn) {
    snapping++;
    el.classList.add("snap");
    try {
      fn();
    } finally {
      if (--snapping === 0) {
        void el.offsetWidth;
        el.classList.remove("snap");
      }
    }
  }

  // ---- the surface: pill <-> card (one shape: its clip grows and shrinks) ----------------
  const GROW_MS = 300, SHRINK_MS = 280, ENTER_MS = 220, ENTER_HOLD_MS = 420;   // pill.css --grow, --shrink-delay + --shrink
  const SURF = { phase: "pill", timers: [], enteringUntil: 0, fade: null };
  const sLater = (fn, ms) => { const id = setTimeout(fn, ms); SURF.timers.push(id); return id; };
  function clearSurf() { SURF.timers.forEach(clearTimeout); SURF.timers = []; el.dataset.moving = "false"; }
  function setSurface(p) {
    SURF.phase = p;
    el.dataset.surface = p;
    // drag the pill, never the card (pywebview drags from any element inside the region)
    el.classList.toggle("pywebview-drag-region", p === "pill");
  }
  /** will-change only while the shape moves (a layer per frame, released afterwards). */
  function moving(ms) {
    el.dataset.moving = "true";
    sLater(() => { el.dataset.moving = "false"; }, ms + 40);
  }
  function cancelFade() { if (SURF.fade) { SURF.fade.cancel(); SURF.fade = null; } }
  /** Reduced motion: no travelling shape, a short cross-fade through the change. */
  function crossfade(apply) {
    cancelFade();
    const from = getComputedStyle(el).opacity;
    const out = el.animate([{ opacity: from }, { opacity: 0 }], { duration: 80, easing: "ease", fill: "forwards" });
    out.onfinish = () => {
      snap(apply);
      out.cancel();
      el.animate([{ opacity: 0 }, { opacity: getComputedStyle(el).opacity }], { duration: 120, easing: "ease" });
    };
  }
  /** Run ``fn`` once the page has drawn two more frames (the card content that popup.js just
   *  laid out is then already rasterised and the shape's first frame is cheap: no jump at the
   *  start of the grow). Timer fallback: a hidden window gets no frames. */
  function afterFrames(fn) {
    let done = false;
    const go = () => { if (!done) { done = true; fn(); } };
    requestAnimationFrame(() => requestAnimationFrame(go));
    sLater(go, 60);
  }
  /** Once per page, shortly after the pill first shows: draw the card content invisibly (it
   *  sits outside the pill's clip and at 0.2 % opacity) so the first real grow does not pay
   *  for its first draw (a 70-160 ms frame in WebView2, measured; later grows were clean). */
  const lift = document.getElementById("lift");
  let warmed = false;
  function warmUp(delay) {
    if (warmed || !mic.isHost || !mic.motion) return;
    warmed = true;
    setTimeout(() => {
      if (SURF.phase !== "pill" || lift.dataset.phase !== "hidden") return;
      lift.dataset.warm = "true";
      setTimeout(endWarm, 400);
    }, delay);
  }
  function endWarm() { delete lift.dataset.warm; }
  function blurInside() {
    const a = document.activeElement;
    if (a && a !== document.body && el.contains(a)) a.blur();
  }
  /** Grow into the card. Returns the ms until the shape starts moving (a pill that was just
   *  born shows itself first), so the card content can be timed against it. */
  function expand() {
    if (SURF.phase === "expanding" || SURF.phase === "card") return 0;
    clearSurf();
    cancelFade();
    clearTimeout(leaveTimer);
    el.dataset.hover = "false";
    poll.stop();
    const wait = Math.max(0, Math.round(SURF.enteringUntil - performance.now()));
    sLater(() => {
      endWarm();   // a just-born pill rehearsed the card during its hold; the real one comes now
      if (!mic.motion) {
        crossfade(() => setSurface("card"));
        mic.send("surface_phase", "card");
        return;
      }
      moving(GROW_MS + 40);
      afterFrames(() => {
        setSurface("expanding");
        mic.send("surface_phase", "expanding");
        sLater(() => { setSurface("card"); mic.send("surface_phase", "card"); }, GROW_MS);
      });
    }, wait);
    return wait;
  }
  /** Shrink back into the pill. Returns the ms until it is a pill again. */
  function collapse() {
    clearSurf();
    blurInside();
    if (SURF.phase === "pill") return 0;
    mic.send("surface_phase", "collapsing");
    if (!mic.motion) {
      crossfade(() => setSurface("pill"));
      if (S.running) poll.start();
      return 220;
    }
    moving(SHRINK_MS);
    setSurface("collapsing");
    sLater(() => { setSurface("pill"); if (S.running) poll.start(); }, SHRINK_MS);
    return SHRINK_MS;
  }
  /** Python closed the card without waiting for the JS: be a pill now (idempotent). */
  function settle() {
    if (SURF.phase === "pill") return;
    clearSurf();
    blurInside();
    snap(() => setSurface("pill"));
    if (S.running) poll.start();
  }
  /** The recording ended: fade out (Python hides the window afterwards). */
  function vanish() {
    clearSurf();
    cancelFade();
    blurInside();
    SURF.fade = el.animate([{ opacity: getComputedStyle(el).opacity }, { opacity: 0 }], { duration: mic.dur(160) || 1, easing: "ease", fill: "forwards" });
  }
  /** The shape the CSS is heading to, in stage CSS px (tests, diagnostics). */
  function box() {
    const cs = getComputedStyle(el);
    const n = (v) => Math.round(parseFloat(cs.getPropertyValue(v)) * 100) / 100;
    return { left: n("--sl"), top: n("--st"), width: n("--sw"), height: n("--sh") };
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
      // Python keeps the region wide until the capsule has narrowed (host.HOVER_S)
      mic.send("pill_hover", false);
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
      snap(() => { setSurface("pill"); el.dataset.hover = "false"; });
      S.elapsed = o.elapsed || 0;
      S.since = performance.now();
      S.running = true;
      el.dataset.provisional = String(!!o.provisional);
      applyPause(!!o.paused);
      startLoop();
      poll.start();
      paint();
      warmUp(o.enter ? 0 : 600);
      if (o.enter) {
        // born on its own (Meet): appear in place, hold a beat, then the card grows from it
        SURF.enteringUntil = performance.now() + mic.dur(ENTER_HOLD_MS);
        el.animate(
          [{ opacity: 0, transform: "translateY(-4px)" }, { opacity: getComputedStyle(el).opacity, transform: "none" }],
          { duration: mic.dur(ENTER_MS) || 1, easing: "cubic-bezier(0.2, 0.9, 0.25, 1)" },
        );
      } else {
        SURF.enteringUntil = 0;
      }
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
    box,
    get phase() { return SURF.phase; },
  };

  // ---- DPI diagnostics: CSS stage vs the physical window (Python logs it) --------------
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
