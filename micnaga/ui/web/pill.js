/* Recording pill controller. Python drives it through window.micnaga.pill.* */
(function () {
  "use strict";
  const el = document.getElementById("pill");
  const timeEl = document.getElementById("time");
  const live = document.getElementById("live");
  const bPause = document.getElementById("b-pause");
  const bMarker = document.getElementById("b-marker");
  const swap = document.getElementById("pause-swap");
  swap.innerHTML = mic.icon("pause", "a") + mic.icon("play", "b");
  document.addEventListener("DOMContentLoaded", () => mic.applyIcons());
  mic.applyIcons();

  const meters = { mic: mic.meter(document.getElementById("m-mic")), sys: mic.meter(document.getElementById("m-sys")) };

  const S = { elapsed: 0, since: performance.now(), paused: false, running: false };
  let tickTimer = 0;
  function now() { return S.elapsed + (S.running && !S.paused ? (performance.now() - S.since) / 1000 : 0); }
  function paint() { timeEl.textContent = mic.fmtClock(now()); }
  function startLoop() { if (!tickTimer) tickTimer = setInterval(paint, 250); }
  function stopLoop() { clearInterval(tickTimer); tickTimer = 0; }

  const poll = mic.pollLevels((m, s) => {
    if (S.paused) { meters.mic.set(0); meters.sys.set(0); return; }
    meters.mic.set(mic.levelToUi(m));
    meters.sys.set(mic.levelToUi(s));
  });

  let leaveTimer = 0;
  function setHover(on) {
    clearTimeout(leaveTimer);
    if (on) {
      el.dataset.hover = "true";
      mic.send("pill_hover", true);
    } else {
      el.dataset.hover = "false";
      // shrink the (transparent, click-swallowing) window only after the capsule has narrowed
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

  const api = (window.micnaga = window.micnaga || {});
  api.pill = {
    /** Start/refresh: {elapsed seconds so far, paused?, provisional?} */
    show(o) {
      o = o || {};
      S.elapsed = o.elapsed || 0;
      S.since = performance.now();
      S.running = true;
      el.dataset.provisional = String(!!o.provisional);
      applyPause(!!o.paused);
      startLoop();
      poll.start();
      paint();
    },
    sync(elapsed) { S.elapsed = elapsed; S.since = performance.now(); paint(); },
    setPaused(p) { if (p !== S.paused) applyPause(p); },
    setProvisional(p) { el.dataset.provisional = String(!!p); },
    stop() { S.running = false; stopLoop(); poll.stop(); meters.mic.reset(); meters.sys.reset(); },
    /** for screenshots / standalone */
    setLevels(m, s) { meters.mic.raw(mic.levelToUi(m)); meters.sys.raw(mic.levelToUi(s)); },
    setHover,
  };

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
      if (q.get("hover") === "1") setHover(true);
      if (q.get("levels")) {
        const [a, b] = q.get("levels").split(",").map(Number);
        poll.stop();
        api.pill.setLevels(a, b);
      }
    }
  });
})();
