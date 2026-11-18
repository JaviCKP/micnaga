/* First-run wizard: welcome/privacy -> model download (SHA-256) -> mic + system audio check. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  const S = { step: 1, dl: "idle", seenMic: 0, seenSys: 0, timer: 0 };
  const meters = { mic: mic.meter(document.querySelector("#m-mic")), sys: mic.meter(document.querySelector("#m-sys")) };
  const MB = 1048576;
  const mb = (b) => (b >= 100 * MB ? Math.round(b / MB) : (b / MB).toFixed(1).replace(".", ",")) + " MB";

  // ---- navigation -------------------------------------------------------------------
  function go(n) {
    S.step = n;
    document.querySelectorAll(".page").forEach((p) => { p.hidden = Number(p.dataset.n) !== n; });
    document.querySelectorAll(".step").forEach((s) => {
      const i = Number(s.dataset.n);
      s.dataset.state = i < n ? "done" : i === n ? "current" : "todo";
      s.setAttribute("aria-current", i === n ? "step" : "false");
      s.querySelector(".bubble").innerHTML = i < n ? mic.icon("check") : String(i);
    });
    document.querySelectorAll(".bar").forEach((b, i) => { b.dataset.done = String(i + 1 < n); });
    $("back").hidden = n === 1;
    const next = $("next");
    next.dataset.i18n = n === 3 ? "wizard.finish" : n === 2 && S.dl !== "done" ? "wizard.download" : "wizard.next";
    next.textContent = mic.t(next.dataset.i18n);
    next.disabled = n === 2 && S.dl === "running";
    if (n === 3) startMonitor(); else stopMonitor();
    $("live").textContent = mic.t("wizard.step" + n + ".title");
    const h = document.querySelector('.page[data-n="' + n + '"] h1');
    if (h) { h.tabIndex = -1; h.focus({ preventScroll: true, focusVisible: false }); }
  }

  $("back").addEventListener("click", () => go(Math.max(1, S.step - 1)));
  $("next").addEventListener("click", async () => {
    if (S.step === 1) { go(2); return; }
    if (S.step === 2) {
      if (S.dl === "done") { go(3); return; }
      await startDownload();
      return;
    }
    $("next").disabled = true;
    await mic.call("finish");
  });

  // ---- step 2: models ---------------------------------------------------------------
  const STATE_KEY = { pending: "wizard.m.pending", downloading: "wizard.m.downloading", verifying: "wizard.m.verifying", verified: "wizard.m.verified", failed: "wizard.m.failed", skipped: "wizard.m.verified" };

  function renderModels(st) {
    const box = $("models");
    const seen = new Set();
    for (const m of st.models) {
      seen.add(m.name);
      let row = box.querySelector('[data-name="' + CSS.escape(m.name) + '"]');
      if (!row) {
        row = el("div", "model");
        row.dataset.name = m.name;
        row.append(el("div", "mn", m.name), el("div", "md"), el("div", "ms"));
        const p = el("div", "progress mp");
        p.append(el("i"));
        row.append(p);
        box.append(row);
      }
      row.dataset.s = m.state;
      const ms = row.querySelector(".ms");
      ms.replaceChildren();
      if (m.state === "downloading" || m.state === "verifying") ms.append(el("span", "spin"));
      if (m.state === "verified" || m.state === "skipped") ms.append(el("span", "sha", "SHA-256"));
      ms.append(mic.t(STATE_KEY[m.state] || "wizard.m.pending"));
      row.querySelector(".md").textContent = m.state === "failed" && m.message ? m.message : m.total ? mb(m.done) + " / " + mb(m.total) : m.message || "";
      row.querySelector(".mp > i").parentElement.style.setProperty("--p", m.total ? Math.min(1, m.done / m.total).toFixed(3) : "0.02");
    }
  }

  async function startDownload() {
    $("dl-error").hidden = true;
    $("overall").hidden = false;
    $("next").disabled = true;
    const r = await mic.call("start_download");
    if (r && r.ok === false) { showError(mic.t(r.error || "wizard.err.generic")); return; }
    S.dl = "running";
    clearInterval(S.timer);
    S.timer = setInterval(poll, 400);
    poll();
  }

  function showError(text) {
    const e = $("dl-error");
    e.textContent = text;
    e.hidden = false;
    S.dl = "failed";
    const n = $("next");
    n.disabled = false;
    n.textContent = mic.t("wizard.retry");
  }

  async function poll() {
    const st = await mic.call("download_status");
    if (!st) return;
    renderModels(st);
    const pct = Math.round(st.overall * 100);
    $("overall-fill").parentElement.style.setProperty("--p", st.overall.toFixed(3));
    $("overall-bar").setAttribute("aria-valuenow", String(pct));
    $("overall-text").textContent = mic.t("wizard.step2.overall", { pct, n: st.verified, total: st.models.length || "?" });
    if (st.state === "done") {
      clearInterval(S.timer);
      S.dl = "done";
      $("next").disabled = false;
      $("next").textContent = mic.t("wizard.next");
      $("live").textContent = mic.t("wizard.step2.done");
    } else if (st.state === "failed") {
      clearInterval(S.timer);
      showError(mic.t("wizard.step2.failed", { detail: st.error || "" }));
    }
  }

  // ---- step 3: live meters ----------------------------------------------------------
  const poller = mic.pollLevels((m, s) => {
    const um = mic.levelToUi(m), us = mic.levelToUi(s);
    meters.mic.set(um);
    meters.sys.set(us);
    $("m-mic").setAttribute("aria-valuenow", String(Math.round(um * 100)));
    $("m-sys").setAttribute("aria-valuenow", String(Math.round(us * 100)));
    if (um > 0.35) S.seenMic++;
    if (us > 0.35) S.seenSys++;
    mark("mic", S.seenMic >= 3);
    mark("sys", S.seenSys >= 3);
  });
  function mark(k, ok) {
    const card = $(k === "mic" ? "c-mic" : "c-sys");
    if (card.dataset.ok === String(ok)) return;
    card.dataset.ok = String(ok);
    $(k === "mic" ? "t-mic" : "t-sys").innerHTML = ok ? mic.icon("check") : "";
    $(k === "mic" ? "h-mic" : "h-sys").textContent = mic.t(ok ? "wizard.step3." + k + "_ok" : "wizard.step3." + k + "_hint");
    if (ok) $("live").textContent = mic.t("wizard.step3." + k + "_ok");
  }
  function startMonitor() { mic.send("monitor", true); poller.start(); }
  function stopMonitor() { poller.stop(); mic.send("monitor", false); meters.mic.reset(); meters.sys.reset(); }
  $("b-test").addEventListener("click", () => mic.send("play_test_sound"));
  window.addEventListener("beforeunload", () => mic.send("monitor", false));

  // ---- standalone mock --------------------------------------------------------------
  if (!mic.isHost && mic.params.get("bridge") !== "http") {
    let t0 = 0;
    const models = [["Parakeet TDT 0.6B v3 (int8)", 640 * MB], ["Silero VAD", 2 * MB], ["Diarización (WeSpeaker)", 26 * MB]];
    mic.mock.start_download = () => { t0 = performance.now(); return { ok: true }; };
    mic.mock.download_status = () => {
      const el_ = (performance.now() - t0) / 1000;
      const out = models.map(([name, total], i) => {
        const p = Math.max(0, Math.min(1, (el_ - i * 1.2) / 3));
        return { name, state: p >= 1 ? "verified" : p > 0.9 ? "verifying" : p > 0 ? "downloading" : "pending", done: Math.round(total * p), total, message: "" };
      });
      const done = out.every((m) => m.state === "verified");
      const tot = out.reduce((a, m) => a + m.total, 0);
      return { state: done ? "done" : "running", error: "", models: out, overall: done ? 1 : out.reduce((a, m) => a + m.done, 0) / tot, verified: out.filter((m) => m.state === "verified").length };
    };
    mic.mock.levels = () => {
      const t = performance.now() / 1000;
      return [Math.max(0, 0.2 + 0.2 * Math.sin(t * 4) * Math.sin(t * 1.3)), 0.02 + (t % 6 > 3 ? 0.25 : 0)];
    };
  }

  mic.i18nReady.then(() => {
    const start = Number(mic.params.get("step") || 1);
    go(start);
    if (start === 2 && mic.params.get("demo") === "1") startDownload();
  });
})();
