/*
 * Micnaga web bridge — shared by every surface.
 *
 *   mic.call(name, ...args)   -> Promise: pywebview.api[name](...)   (or mic.mock[name] standalone)
 *   mic.t(key, vars)          -> localised string from i18n/es.json
 *   mic.applyI18n(root)       -> fills data-i18n / data-i18n-<attr> nodes
 *   mic.icon(name, cls)       -> inline SVG markup (one icon set, currentColor)
 *   mic.meter(el)             -> smoothed level meter controller (bar)
 *   mic.wave(canvas)          -> scrolling waveform meter painted on a canvas
 *   mic.motion                -> false when animations are off (Windows setting / reduced motion)
 *   mic.dur(ms)               -> ms, or 0 when motion is off
 *
 * URL parameters (set by Python or by the screenshot tool):
 *   ?theme=light|dark  ?motion=off  ?backdrop=mica  ?lang=es  ?stage=1 (demo backdrop)
 *
 * All text lives in i18n/es.json; no user-visible string is hard-coded in HTML/JS.
 */
(function () {
  "use strict";
  const params = new URLSearchParams(location.search);
  const root = document.documentElement;

  const theme = params.get("theme");
  if (theme === "light" || theme === "dark") root.dataset.theme = theme;
  if (params.get("motion") === "off") root.dataset.motion = "off";
  if (params.get("backdrop")) root.dataset.backdrop = params.get("backdrop");
  if (params.get("stage")) document.addEventListener("DOMContentLoaded", () => document.body.classList.add("stage-demo"));

  const reducedMq = window.matchMedia ? matchMedia("(prefers-reduced-motion: reduce)") : null;
  const mic = (window.mic = window.mic || {});
  mic.params = params;
  mic.mock = mic.mock || {};
  Object.defineProperty(mic, "motion", {
    get() { return root.dataset.motion !== "off" && !(reducedMq && reducedMq.matches); },
  });
  mic.dur = (ms) => (mic.motion ? ms : 0);
  mic.isHost = !!(window.chrome && window.chrome.webview);

  // ---- python <-> js ----------------------------------------------------------------
  let readyResolve;
  mic.ready = new Promise((r) => { readyResolve = r; });
  if (mic.isHost) {
    if (window.pywebview && window.pywebview.api) readyResolve();
    else window.addEventListener("pywebviewready", () => readyResolve());
  } else {
    readyResolve();
  }
  mic.call = async function (name, ...args) {
    await mic.ready;
    if (mic.isHost && window.pywebview && window.pywebview.api && window.pywebview.api[name]) {
      return window.pywebview.api[name](...args);
    }
    // dev/test only: ?bridge=http&surface=library posts to the local server's dev_api
    if (params.get("bridge") === "http") {
      const r = await fetch("api/" + (params.get("surface") || "") + "/" + name, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(args),
      });
      if (!r.ok) throw new Error("api " + name + " " + r.status);
      return r.json();
    }
    const fn = mic.mock[name];
    return fn ? fn(...args) : undefined;
  };
  // fire-and-forget (never throws into UI code)
  mic.send = function (name, ...args) {
    mic.call(name, ...args).catch((e) => console.warn("api", name, e));
  };

  // Python -> JS: window.mic.setTheme('dark'), setMotion(false)
  mic.setTheme = (t) => {
    if (t === "light" || t === "dark") root.dataset.theme = t; else delete root.dataset.theme;
    document.dispatchEvent(new Event("mic:theme"));
  };
  if (window.matchMedia) matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => document.dispatchEvent(new Event("mic:theme")));
  mic.setMotion = (on) => { if (on) delete root.dataset.motion; else root.dataset.motion = "off"; };
  mic.setBackdrop = (b) => { if (b) root.dataset.backdrop = b; else delete root.dataset.backdrop; };

  // ---- i18n -------------------------------------------------------------------------
  let strings = {};
  mic.t = function (key, vars) {
    let s = strings[key];
    if (s === undefined) { console.warn("i18n missing", key); return key; }
    if (vars) s = s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m));
    return s;
  };
  const ATTRS = ["title", "aria-label", "placeholder", "alt", "aria-description", "data-tip"];
  mic.applyI18n = function (scope) {
    scope = scope || document;
    scope.querySelectorAll("[data-i18n]").forEach((el) => { if (el.dataset.i18n in strings) el.textContent = mic.t(el.dataset.i18n); });
    for (const a of ATTRS) {
      const dk = "i18n" + a.replace(/(^|-)(\w)/g, (m, p, c) => c.toUpperCase());
      scope.querySelectorAll("[data-i18n-" + a + "]").forEach((el) => { el.setAttribute(a, mic.t(el.dataset[dk])); });
    }
  };
  mic.i18nReady = (async () => {
    const lang = params.get("lang") || "es";
    try {
      const r = await fetch("i18n/" + lang + ".json");
      strings = await r.json();
    } catch (e) {
      console.warn("i18n load failed", e);
    }
    if (document.readyState === "loading") await new Promise((r) => document.addEventListener("DOMContentLoaded", r));
    root.lang = lang;
    mic.applyI18n();
  })();

  // ---- formatting -------------------------------------------------------------------
  mic.fmtClock = function (s) {
    s = Math.max(0, Math.floor(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const mm = String(m).padStart(2, "0"), ss = String(sec).padStart(2, "0");
    return h ? h + ":" + mm + ":" + ss : mm + ":" + ss;
  };
  mic.fmtDuration = function (s) {
    const m = Math.round(s / 60);
    if (m < 60) return mic.t("fmt.minutes", { n: m });
    const h = Math.floor(m / 60), r = m % 60;
    return r ? mic.t("fmt.hm", { h, m: r }) : mic.t("fmt.hours", { h });
  };
  mic.fmtDate = function (iso) {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const day = new Date(d); day.setHours(0, 0, 0, 0);
    const diff = Math.round((today - day) / 86400000);
    const time = d.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
    if (diff === 0) return mic.t("fmt.today", { time });
    if (diff === 1) return mic.t("fmt.yesterday", { time });
    return d.toLocaleDateString("es-ES", { day: "numeric", month: "short", year: day.getFullYear() === today.getFullYear() ? undefined : "numeric" }) + " · " + time;
  };

  // ---- icons ------------------------------------------------------------------------
  const P = {
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
    speaker: '<path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a8 8 0 0 1 0 11"/>',
    flag: '<path d="M6 21V4M6 5h11l-2.5 3.5L17 12H6"/>',
    pause: '<path d="M8 5v14M16 5v14"/>',
    play: '<path d="M8 5.5v13l11-6.5z"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2.5"/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    x: '<path d="M6 6l12 12M18 6 6 18"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4-4"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8"/>',
    folder: '<path d="M3.5 7.5a2 2 0 0 1 2-2H10l2 2.5h6.5a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
    user: '<circle cx="12" cy="8.5" r="3.5"/><path d="M5 20c.8-3.6 3.6-5.5 7-5.5s6.2 1.9 7 5.5"/>',
    users: '<circle cx="9" cy="8.5" r="3"/><path d="M3.5 19c.6-3.2 2.9-5 5.5-5s4.9 1.8 5.5 5M16 6.2a3 3 0 0 1 0 5.6M17.5 14.3c1.7.6 2.8 2.2 3.2 4.7"/>',
    merge: '<path d="M6 4v5a4 4 0 0 0 4 4h4a4 4 0 0 1 4 4v3M6 20v-3M18 4v3"/>',
    trash: '<path d="M4.5 7h15M10 7V4.5h4V7M6.5 7l.8 12.5h9.4L17.5 7M10 11v5M14 11v5"/>',
    info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.8v.2"/>',
    chevron: '<path d="m9 6 6 6-6 6"/>',
    chevdown: '<path d="m6 9 6 6 6-6"/>',
    shield: '<path d="M12 3.5 5 6v5.5c0 4.2 2.8 7.4 7 9 4.2-1.6 7-4.8 7-9V6z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
    download: '<path d="M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14"/>',
    bookmark: '<path d="M7 4h10v16l-5-3.5L7 20z"/>',
    edit: '<path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17z"/>',
    arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    external: '<path d="M14 4h6v6M20 4l-9 9M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
    refresh: '<path d="M4.5 12a7.5 7.5 0 0 1 13-5l2 2M19.5 4v5h-5M19.5 12a7.5 7.5 0 0 1-13 5l-2-2M4.5 20v-5h5"/>',
    library: '<path d="M5 4.5v15M10 4.5v15M15 6l4 .9L16 19.5l-4-.9"/>',
    activity: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
    bell: '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 1.5h-15zM10 20.5h4"/>',
    power: '<path d="M12 3.5v8M6.7 6.8a7.5 7.5 0 1 0 10.6 0"/>',
    copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/><path d="M15.5 8.5V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7.5a2 2 0 0 0 2 2h2.5"/>',
    volume: '<path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5z"/>',
    mute: '<path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5zM16 9.5l5 5M21 9.5l-5 5"/>',
    sliders: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    wave: '<path d="M4 12v0M8 8v8M12 4v16M16 8v8M20 12v0"/>',
    megaphone: '<path d="M4 9.5v5l12.5 4.5v-14zM7.5 15.8 9 20h2.5l-1-3.2M19.5 10v4"/>',
    video: '<rect x="3.5" y="6.5" width="12" height="11" rx="2.5"/><path d="m15.5 10.5 5-3v9l-5-3"/>',
    app: '<rect x="3.5" y="5" width="17" height="14" rx="3"/><path d="M3.5 9.5h17M6.5 7.2h.01M9 7.2h.01"/>',
    restart: '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3L4.5 9M4.5 4.5V9H9"/>',
    help: '<circle cx="12" cy="12" r="8.5"/><path d="M9.8 9.6a2.3 2.3 0 0 1 4.4.9c0 1.6-2.2 2-2.2 3.4M12 16.8v.2"/>',
    lock: '<rect x="5" y="10.5" width="14" height="9.5" rx="2.5"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
    feather: '<path d="M5 19 15.5 8.5M7 17h6.5A6.5 6.5 0 0 0 20 10.5V4h-6.5A6.5 6.5 0 0 0 7 10.5z"/>',
    cpu: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/><path d="M9.5 3.5v3M14.5 3.5v3M9.5 17.5v3M14.5 17.5v3M3.5 9.5h3M3.5 14.5h3M17.5 9.5h3M17.5 14.5h3"/>',
    eye: '<path d="M2.8 12S6 5.5 12 5.5 21.2 12 21.2 12 18 18.5 12 18.5 2.8 12 2.8 12z"/><circle cx="12" cy="12" r="3"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    calendar: '<rect x="4" y="5.5" width="16" height="14.5" rx="2.5"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/>',
    dots: '<circle cx="6" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="18" cy="12" r="1.2"/>',
  };
  mic.icon = function (name, cls) {
    return '<svg class="ico ' + (cls || "") + '" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' + (P[name] || "") + "</svg>";
  };
  mic.applyIcons = function (scope) {
    (scope || document).querySelectorAll("[data-icon]").forEach((el) => {
      if (el.dataset.iconDone) return;
      el.insertAdjacentHTML("afterbegin", mic.icon(el.dataset.icon, el.dataset.iconClass || ""));
      el.dataset.iconDone = "1";
    });
  };
  document.addEventListener("DOMContentLoaded", () => mic.applyIcons());

  // ---- levels -----------------------------------------------------------------------
  // Python hands over LINEAR amplitude 0..1 (peak or RMS). Perceptual mapping lives here:
  // -54 dBFS .. 0 dBFS -> 0..1 so a quiet room still moves the bar a little.
  mic.levelToUi = function (x) {
    if (!(x > 0)) return 0;
    const db = 20 * Math.log10(Math.min(1, x));
    return Math.max(0, Math.min(1, (db + 54) / 54));
  };

  /** Smoothed meter: fast attack, slow release, driven by requestAnimationFrame. */
  mic.meter = function (el) {
    let target = 0, shown = 0, raf = 0, last = 0, alive = true;
    const step = (t) => {
      raf = 0;
      if (!alive) return;
      const dt = Math.min(0.1, (t - (last || t)) / 1000); last = t;
      const rate = target > shown ? 28 : 5.5;           // 1/s
      shown += (target - shown) * (1 - Math.exp(-rate * dt));
      if (Math.abs(target - shown) < 0.002) shown = target;
      el.style.setProperty("--lv", shown.toFixed(3));
      if (shown !== target) raf = requestAnimationFrame(step); else last = 0;
    };
    return {
      set(v) { target = Math.max(0, Math.min(1, v)); if (!raf && alive) raf = requestAnimationFrame(step); },
      raw(v) { target = shown = v; el.style.setProperty("--lv", String(v)); },
      reset() { target = shown = 0; el.style.setProperty("--lv", "0"); },
      stop() { alive = false; if (raf) cancelAnimationFrame(raf); },
      start() { alive = true; },
      get value() { return shown; },
    };
  };

  /**
   * Live waveform: a scrolling history of levels painted on a <canvas> (one 2D draw per push,
   * ~20 fps; no layout, no per-bar DOM). Bars grow from the centre, older bars fade out, colour
   * is the canvas' CSS `color` (so themes and states restyle it for free).
   *   const w = mic.wave(canvasEl, { bar: 3, gap: 2, min: 2 });  w.push(uiLevel0to1); w.reset();
   */
  mic.wave = function (canvas, opts) {
    const o = Object.assign({ bar: 3, gap: 2, min: 2, fade: 0.28, round: true, curve: 2.2 }, opts || {});
    const ctx = canvas.getContext("2d");
    let W = 0, H = 0, dpr = 1, n = 0, hist = new Float32Array(0), head = 0, color = "#888", frame = 0, last = 0;
    function size() {
      dpr = window.devicePixelRatio || 1;
      const r = canvas.getBoundingClientRect();
      W = Math.max(1, Math.round(r.width)); H = Math.max(1, Math.round(r.height));
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
      const nn = Math.max(1, Math.floor((W + o.gap) / (o.bar + o.gap)));
      if (nn !== n) { const old = hist; n = nn; hist = new Float32Array(n); head = 0; if (old.length) hist.fill(0); }
      draw();
    }
    function readColor() { color = getComputedStyle(canvas).color || color; }
    function draw() {
      if (!n) return;
      if (frame++ % 20 === 0) readColor();
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      ctx.fillStyle = color;
      const used = n * (o.bar + o.gap) - o.gap, x0 = W - used;
      for (let i = 0; i < n; i++) {
        const v = hist[(head + i) % n];                     // i = 0 oldest .. n-1 newest
        const h = Math.max(o.min, Math.min(H, Math.pow(v, o.curve) * H));   // curve: speech reads as speech
        const x = x0 + i * (o.bar + o.gap), y = (H - h) / 2;
        ctx.globalAlpha = o.fade + (1 - o.fade) * (i / Math.max(1, n - 1));
        if (o.round && ctx.roundRect) { ctx.beginPath(); ctx.roundRect(x, y, o.bar, h, o.bar / 2); ctx.fill(); }
        else ctx.fillRect(x, y, o.bar, h);
      }
      ctx.globalAlpha = 1;
    }
    if (window.ResizeObserver) new ResizeObserver(size).observe(canvas);
    document.addEventListener("mic:theme", () => requestAnimationFrame(() => { readColor(); draw(); }));
    size();
    return {
      /** v: 0..1 UI level. A light attack/release keeps the bars organic, not jittery. */
      push(v) {
        v = Math.max(0, Math.min(1, Number(v) || 0));
        const s = v > last ? v : last * 0.55 + v * 0.45;
        last = s;
        if (!n) return;
        hist[head] = s; head = (head + 1) % n;
        draw();
      },
      fill(v) { hist.fill(Math.max(0, Math.min(1, v))); last = v; draw(); },
      reset() { hist.fill(0); last = 0; draw(); },
      redraw() { readColor(); draw(); },
      get level() { return last; },
    };
  };

  /** Poll `api.levels()` at 20 fps while `active()`; feed [mic, system] (linear) to cb. */
  mic.pollLevels = function (cb, opts) {
    const hz = (opts && opts.hz) || 20;
    let timer = 0, busy = false;
    const tick = async () => {
      if (busy) return;
      busy = true;
      try {
        const v = await mic.call("levels");
        if (Array.isArray(v) && v.length >= 2) cb(Number(v[0]) || 0, Number(v[1]) || 0);
      } catch (e) { /* ignore a missed frame */ }
      busy = false;
    };
    return {
      start() { if (!timer) timer = setInterval(tick, 1000 / hz); },
      stop() { if (timer) clearInterval(timer); timer = 0; },
    };
  };

  mic.esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  mic.sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // Block the WebView's own context menu / zoom keys: this is an app, not a web page.
  document.addEventListener("contextmenu", (e) => { if (!/INPUT|TEXTAREA/.test(e.target.tagName)) e.preventDefault(); });
  window.addEventListener("wheel", (e) => { if (e.ctrlKey) e.preventDefault(); }, { passive: false });
})();
