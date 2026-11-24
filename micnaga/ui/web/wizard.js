/*
 * First run, one click. On open: the model download starts by itself (start_download is
 * idempotent: if Python already started it, this just joins), the audio checks run on their
 * own (monitor + one soft test sound), and when the models are verified the window says
 * «Listo» and calls finish() — Python closes it and Micnaga lives in the tray.
 * The only control is «Reintentar», and only after a failure.
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const S = { state: "running", timer: 0, seenMic: 0, seenSys: 0, finished: false, testPlayed: false };
  const waves = {
    mic: mic.wave($("m-mic"), { bar: 2.5, gap: 2.5, min: 2, curve: 1.8 }),
    sys: mic.wave($("m-sys"), { bar: 2.5, gap: 2.5, min: 2, curve: 1.8 }),
  };
  const MB = 1048576;
  const mb = (b) => (b >= 100 * MB ? Math.round(b / MB) : (b / MB).toFixed(1).replace(".", ",")) + " MB";

  function setState(s) { S.state = s; $("wiz").dataset.state = s; }
  function setProgress(p) {
    const v = Math.max(0, Math.min(1, p));
    $("overall-fill").parentElement.style.setProperty("--p", v.toFixed(3));
    $("ring-fill").style.setProperty("--p", v.toFixed(3));
    $("overall-bar").setAttribute("aria-valuenow", String(Math.round(v * 100)));
  }

  // ---- download (starts by itself) ----------------------------------------------------
  async function startDownload() {
    $("fail").hidden = true;
    setState("running");
    $("overall-text").textContent = mic.t("wizard.preparing");
    const r = await mic.call("start_download");
    if (r && r.ok === false) { fail(mic.t(r.error || "wizard.err.generic")); return; }
    clearInterval(S.timer);
    S.timer = setInterval(poll, 400);
    poll();
  }

  function eta(s) {
    if (s === null || s === undefined) return "";
    return s >= 90 ? mic.t("wizard.eta_min", { n: Math.round(s / 60) }) : mic.t("wizard.eta_sec", { n: Math.max(1, Math.round(s)) });
  }

  async function poll() {
    let st;
    try { st = await mic.call("download_status"); } catch (e) { return; }
    if (!st) return;
    setProgress(st.overall || 0);
    const busy = (st.models || []).find((m) => m.state === "downloading" || m.state === "verifying");
    const total = st.models && st.models.length ? st.models.length : "?";
    $("overall-text").textContent = st.verifying
      ? mic.t("wizard.verifying")
      : busy && busy.total
        ? mic.t("wizard.status", { pct: Math.round(st.overall * 100), size: st.done_text || mb(st.models.reduce((a, m) => a + (m.done || 0), 0)), n: st.verified, total })
        : mic.t("wizard.verified", { n: st.verified, total });
    // «1,2 GB de 1,7 GB · 11 MB/s · quedan 1 min» once the speed is known
    $("overall-detail").textContent = st.state === "running" && st.speed_text && !st.verifying
      ? mic.t("wizard.detail", { done: st.done_text, total: st.total_text, speed: st.speed_text, eta: eta(st.eta_s) })
      : "";
    $("cancel").hidden = st.state !== "running";
    if (st.state === "done") {
      clearInterval(S.timer);
      done();
    } else if (st.state === "failed") {
      clearInterval(S.timer);
      const key = st.error_key || "";
      const said = key ? mic.t(key, st.error_args || {}) : "";
      fail(said && said !== key ? said : mic.t("wizard.failed", { detail: st.error || "" }));
    } else if (st.state === "cancelled") {
      clearInterval(S.timer);
      fail(mic.t("wizard.cancelled"), true);
    }
  }

  function fail(text, resume) {
    setState("failed");
    $("cancel").hidden = true;
    $("overall-detail").textContent = "";
    $("dl-error").textContent = text;
    $("retry-label").textContent = resume ? mic.t("wizard.resume") : mic.t("wizard.retry");
    $("fail").hidden = false;
    $("retry").focus({ preventScroll: true });
  }
  $("retry").addEventListener("click", startDownload);
  $("cancel").addEventListener("click", () => { $("cancel").hidden = true; mic.send("cancel_download"); });

  function done() {
    if (S.finished) return;
    S.finished = true;
    setProgress(1);
    setState("done");
    $("overall-text").textContent = mic.t("wizard.ready");
    $("live").textContent = mic.t("wizard.ready");
    // a breath to see «Listo», then the window goes and Micnaga lives in the tray
    setTimeout(() => { stopMonitor(); mic.send("finish"); }, mic.motion ? 1600 : 400);
  }

  // ---- audio: passes on its own ---------------------------------------------------------
  const poller = mic.pollLevels((m, s) => {
    const um = mic.levelToUi(m), us = mic.levelToUi(s);
    waves.mic.push(um);
    waves.sys.push(us);
    $("m-mic").setAttribute("aria-valuenow", String(Math.round(um * 100)));
    $("m-sys").setAttribute("aria-valuenow", String(Math.round(us * 100)));
    if (um > 0.35) S.seenMic++;
    if (us > 0.35) S.seenSys++;
    mark("mic", S.seenMic >= 3);
    mark("sys", S.seenSys >= 3);
  });
  function mark(k, ok) {
    const chip = $(k === "mic" ? "c-mic" : "c-sys");
    if (chip.dataset.ok === String(ok)) return;
    chip.dataset.ok = String(ok);
    if (ok) $("live").textContent = mic.t("wizard." + k + "_ok");
  }
  function startMonitor() {
    mic.send("monitor", true);
    poller.start();
    // one soft chime so «system audio» can prove itself without a click
    setTimeout(() => { if (!S.testPlayed && S.state !== "failed") { S.testPlayed = true; mic.send("play_test_sound"); } }, 1800);
  }
  function stopMonitor() { poller.stop(); mic.send("monitor", false); }
  window.addEventListener("beforeunload", () => mic.send("monitor", false));

  // ---- standalone mock ------------------------------------------------------------------
  if (!mic.isHost && mic.params.get("bridge") !== "http") {
    let t0 = 0;
    const models = [["Whisper large-v3-turbo", 1620 * MB], ["Silero VAD", 2 * MB], ["Diarización (WeSpeaker)", 26 * MB]];
    mic.mock.start_download = () => { t0 = performance.now(); return { ok: true }; };
    mic.mock.download_status = () => {
      const el_ = (performance.now() - t0) / 1000;
      const out = models.map(([name, total], i) => {
        const p = Math.max(0, Math.min(1, (el_ - i * 1.2) / 3));
        return { name, state: p >= 1 ? "verified" : p > 0.9 ? "verifying" : p > 0 ? "downloading" : "pending", done: Math.round(total * p), total, message: "" };
      });
      const all = out.every((m) => m.state === "verified");
      const tot = out.reduce((a, m) => a + m.total, 0);
      return { state: all ? "done" : "running", error: "", models: out, overall: all ? 1 : out.reduce((a, m) => a + m.done, 0) / tot, verified: out.filter((m) => m.state === "verified").length };
    };
    mic.mock.levels = () => {
      const t = performance.now() / 1000;
      return [Math.max(0, 0.2 + 0.2 * Math.sin(t * 4) * Math.sin(t * 1.3)), 0.02 + (t % 6 > 3 ? 0.25 : 0)];
    };
  }

  const api = (window.micnaga = window.micnaga || {});
  api.wizard = { get state() { return S.state; }, retry: startDownload };

  mic.i18nReady.then(() => {
    mic.applyIcons();
    startMonitor();
    if (mic.params.get("hold") === "1") { setProgress(Number(mic.params.get("p") || 0.42)); return; }  // screenshots
    startDownload();
  });
})();
