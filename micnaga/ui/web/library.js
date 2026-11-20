/* Micnaga library: meeting list, instant search, transcript viewer with synced audio. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const strip = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

  const S = {
    meetings: [], folder: null, data: null, query: "", canEdit: false, vpEnabled: false, vpNames: new Set(),
    segs: [], playingIdx: -1, userScrollAt: 0, highlightQuery: "",
  };

  // ------------------------------------------------------------------ audio
  const player = (() => {
    const els = { yo: null, remoto: null };
    const muted = { yo: false, remoto: false };
    let master = null, total = 0, raf = 0, drift = 0, playing = false, rate = 1, onTime = () => {}, onState = () => {};
    function make(url) {
      const a = new Audio();
      a.preload = "metadata";
      a.src = url;
      a.addEventListener("error", () => snack(mic.t("library.err.audio")));
      return a;
    }
    function loop() {
      raf = 0;
      if (!master) return;
      onTime(master.currentTime);
      if (playing) raf = requestAnimationFrame(loop);
    }
    function resync() {
      if (!master) return;
      for (const k of ["yo", "remoto"]) {
        const a = els[k];
        if (a && a !== master && Math.abs(a.currentTime - master.currentTime) > 0.12) a.currentTime = master.currentTime;
      }
    }
    return {
      load(audio, dur) {
        this.unload();
        total = dur || 0;
        for (const k of ["yo", "remoto"]) if (audio[k]) els[k] = make(audio[k]);
        master = els.remoto || els.yo;
        if (master) master.addEventListener("ended", () => { playing = false; onState(false); });
        for (const k of ["yo", "remoto"]) if (els[k]) els[k].muted = muted[k];
        onState(false);
        return !!master;
      },
      unload() {
        playing = false;
        clearInterval(drift);
        cancelAnimationFrame(raf);
        for (const k of ["yo", "remoto"]) if (els[k]) { els[k].pause(); els[k].removeAttribute("src"); els[k].load(); els[k] = null; }
        master = null;
      },
      get available() { return !!master; },
      get has() { return { yo: !!els.yo, remoto: !!els.remoto }; },
      get time() { return master ? master.currentTime : 0; },
      get duration() { return Math.max(total, master && isFinite(master.duration) ? master.duration : 0); },
      get playing() { return playing; },
      async play() {
        if (!master) return;
        resync();
        await Promise.all(Object.values(els).filter(Boolean).map((a) => { a.playbackRate = rate; return a.play().catch(() => {}); }));
        playing = true;
        onState(true);
        clearInterval(drift);
        drift = setInterval(resync, 400);
        if (!raf) raf = requestAnimationFrame(loop);
      },
      pause() {
        Object.values(els).filter(Boolean).forEach((a) => a.pause());
        playing = false;
        clearInterval(drift);
        onState(false);
        onTime(this.time);
      },
      seek(t) {
        t = Math.max(0, Math.min(t, this.duration || t));
        Object.values(els).filter(Boolean).forEach((a) => { a.currentTime = Math.min(t, isFinite(a.duration) ? a.duration : t); });
        onTime(t);
      },
      setRate(r) { rate = r; Object.values(els).filter(Boolean).forEach((a) => { a.playbackRate = r; }); },
      setMuted(k, m) { muted[k] = m; if (els[k]) els[k].muted = m; },
      isMuted: (k) => muted[k],
      on(t, s) { onTime = t; onState = s; },
    };
  })();

  // ------------------------------------------------------------------ helpers
  let snackTimer = 0;
  function snack(text) {
    const s = $("snack");
    s.textContent = text;
    s.classList.add("show");
    clearTimeout(snackTimer);
    snackTimer = setTimeout(() => s.classList.remove("show"), 3200);
  }
  const errText = (r) => mic.t(r && r.error ? r.error : "library.err.op_failed");

  function colorOf(id) {
    if (id === "me") return "var(--sp-me)";
    const n = parseInt(String(id).replace(/\D/g, ""), 10) || 1;
    return "var(--sp-" + (((n - 1) % 6) + 1) + ")";
  }
  const nameOf = (id) => {
    const s = S.data && S.data.transcript.speakers.find((x) => x.id === id);
    return s ? s.name : id;
  };

  /** Append `text` to `parent`, wrapping the words that start with a query token in <mark>. */
  function highlightInto(parent, text, query) {
    const toks = strip(query).split(/[^a-z0-9ñ]+/).filter(Boolean);
    if (!toks.length) { parent.textContent = text; return; }
    let norm = "";
    const map = [];
    for (let i = 0; i < text.length; i++) {
      const base = strip(text[i]);
      for (const c of base) { norm += c; map.push(i); }
    }
    const ranges = [];
    const re = /[a-z0-9ñ]+/g;
    let m;
    while ((m = re.exec(norm))) {
      const w = m[0];
      const tok = toks.find((t) => w.startsWith(t));
      if (tok) ranges.push([map[m.index], map[m.index + tok.length - 1] + 1]);
    }
    let pos = 0;
    for (const [a, b] of ranges) {
      if (a < pos) continue;
      if (a > pos) parent.append(text.slice(pos, a));
      parent.append(Object.assign(el("mark"), { textContent: text.slice(a, b) }));
      pos = b;
    }
    parent.append(text.slice(pos));
  }

  // ------------------------------------------------------------------ list
  const dayDiff = (iso) => {
    const d = new Date(iso);
    const t0 = new Date(); t0.setHours(0, 0, 0, 0);
    d.setHours(0, 0, 0, 0);
    return Math.round((t0 - d) / 86400000);
  };
  const groupKey = (iso) => {
    const n = dayDiff(iso);
    return n <= 0 ? "today" : n === 1 ? "yesterday" : n < 7 ? "week" : "earlier";
  };

  function renderList() {
    const side = $("side");
    side.replaceChildren();
    $("count").textContent = S.meetings.length ? mic.t("library.count", { n: S.meetings.length }) : "";
    $("count").setAttribute("aria-label", S.meetings.length ? mic.t("library.count_label", { n: S.meetings.length }) : "");
    if (!S.meetings.length) {
      const d = el("div", "nores");
      d.innerHTML = mic.icon("library");
      d.append(el("div", "t", mic.t("library.none_title")));
      side.append(d);
      return;
    }
    let last = "";
    for (const m of S.meetings) {
      const g = groupKey(m.start);
      if (g !== last) { side.append(el("div", "group-h caps", mic.t("library.group." + g))); last = g; }
      const b = el("button", "row");
      b.type = "button";
      b.dataset.folder = m.folder;
      if (m.folder === S.folder) b.setAttribute("aria-current", "true");
      b.append(el("div", "row-title", m.title));
      const sub = el("div", "row-sub tnum");
      sub.append(el("span", "", mic.fmtDate(m.start)), el("span", "dot", "·"), el("span", "", mic.fmtDuration(m.duration_s)));
      const n = m.speakers.length;
      if (n) {
        const dots = el("span", "who-dots");
        dots.setAttribute("role", "img");
        dots.setAttribute("aria-label", mic.t(n === 1 ? "library.speakers_one" : "library.speakers_other", { n }));
        dots.title = m.speakers.map((x) => x.name).join(" · ");
        for (const sp of m.speakers.slice(0, 6)) { const i = el("i"); i.style.setProperty("--c", colorOf(sp.id)); dots.append(i); }
        sub.append(dots);
      }
      b.append(sub);
      b.addEventListener("click", () => openMeeting(m.folder));
      side.append(b);
    }
  }

  function renderHits(res) {
    const side = $("side");
    side.replaceChildren();
    $("count").textContent = "";
    if (!res.hits.length) {
      const d = el("div", "nores");
      d.innerHTML = mic.icon("search");
      d.append(el("div", "t", mic.t("library.nores_title")));
      side.append(d);
      return;
    }
    side.append(el("div", "res-info caps tnum", mic.t("library.results", { n: res.hits.length, ms: res.ms })));
    for (const h of res.hits) {
      const b = el("button", "row hit");
      b.type = "button";
      const top = el("div", "hit-top");
      top.append(el("b", "", h.title), el("span", "t", mic.fmtClock(h.start)));
      const tx = el("div", "hit-text");
      tx.append(el("span", "hit-who", h.speaker + ": "));
      for (const p of h.parts) tx.append(p.hit ? el("mark", "", p.t) : document.createTextNode(p.t));
      b.append(top, tx);
      b.addEventListener("click", () => openMeeting(h.folder, h.start, res.query));
      side.append(b);
    }
  }

  let searchTimer = 0, searchSeq = 0;
  function onQuery() {
    const q = $("q").value.trim();
    S.query = q;
    $("q-clear").hidden = !q;
    document.querySelector(".search").classList.toggle("has-value", !!q);
    clearTimeout(searchTimer);
    if (!q) { renderList(); return; }
    searchTimer = setTimeout(async () => {
      const seq = ++searchSeq;
      const res = await mic.call("search", q, 60);
      if (seq === searchSeq && res) renderHits(res);
    }, 110);
  }

  // ------------------------------------------------------------------ viewer
  async function openMeeting(folder, seekT, query) {
    const r = await mic.call("get_meeting", folder);
    if (!r || !r.ok) { snack(errText(r)); return; }
    const sameMeeting = S.folder === folder;
    S.folder = folder;
    S.data = r;
    S.highlightQuery = query || "";
    renderViewer(sameMeeting);
    document.querySelectorAll(".row[data-folder]").forEach((b) => { if (b.dataset.folder === folder) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current"); });
    if (typeof seekT === "number" && !isNaN(seekT)) {
      if (player.available) player.seek(seekT);
      focusPhraseAt(seekT, true);
    }
  }

  function renderViewer(keepPlayback) {
    const tr = S.data.transcript;
    S.segs = tr.segments;
    $("empty").hidden = true;
    $("viewer").hidden = false;
    $("m-title").textContent = tr.meeting.title;
    const meta = $("m-meta");
    meta.replaceChildren();
    meta.append(el("span", "", mic.fmtDate(tr.meeting.start)), el("span", "sep", "·"), el("span", "", mic.fmtDuration(tr.meeting.duration_s)));
    if (tr.meeting.meet_code) meta.append(el("span", "tag-mono", tr.meeting.meet_code));
    renderSpeakers();
    renderMarkers();
    renderOriginalToggle();
    renderTranscript();
    $("open-folder").onclick = async () => {
      const x = await mic.call("open_folder", S.folder);
      if (x && !x.ok) snack(errText(x));
    };
    // audio
    const wasPlaying = keepPlayback && player.playing;
    const t = keepPlayback ? player.time : 0;
    if (!keepPlayback) {
      player.load(S.data.audio, tr.meeting.duration_s);
      $("play").disabled = !player.available;
    }
    renderTracks();
    if (keepPlayback && wasPlaying) player.seek(t);
    $("total").textContent = mic.fmtClock(player.duration || tr.meeting.duration_s);
    updateTime(keepPlayback ? t : 0);
    renderTicks(tr);
    hqRender();
    document.dispatchEvent(new CustomEvent("mic:meeting", { detail: { folder: S.folder } }));
  }

  // «Mejorar transcripcion»: icono en la cabecera, confirmacion en linea y linea de progreso
  const HQ = { timer: 0, folder: "", info: null };
  function hqShow(text, go, no) {
    const bar = $("hq-bar");
    bar.hidden = false;
    $("hq-text").textContent = text;
    $("hq-go").hidden = !go; $("hq-no").hidden = !no;
    if (go) { $("hq-go").textContent = go[0]; $("hq-go").onclick = go[1]; }
    if (no) { $("hq-no").textContent = no[0]; $("hq-no").onclick = no[1]; }
  }
  function hqHide() {
    clearTimeout(HQ.timer);
    $("hq-bar").hidden = true; $("hq-progress").hidden = true;
    $("hq-btn").setAttribute("aria-pressed", "false");
  }
  async function hqRender() {
    const btn = $("hq-btn");
    clearTimeout(HQ.timer);
    if (!S.hq) { btn.hidden = true; $("hq-bar").hidden = true; return; }
    btn.hidden = false;
    btn.onclick = hqAsk;
    HQ.folder = S.folder;
    hqHide();
    const r = await mic.call("hq", "status", S.folder);
    if (!r || !r.ok || HQ.folder !== S.folder) return;
    if (r.status && r.status.state === "running") hqPoll();
    else if (r.previa) hqOffer();
  }
  function hqOffer() {
    hqShow("", null, [mic.t("library.hq_restore"), hqRestore]);
  }
  function hqPhase(fase, v) {
    if (fase === "descarga") return mic.t("library.hq_phase_descarga", v);
    if (fase === "prepara") return mic.t("library.hq_phase_prepara", v);
    if (fase === "transcribe") return mic.t("library.hq_phase_transcribe", v);
    if (fase === "ensambla") return mic.t("library.hq_phase_ensambla", v);
    return mic.t("library.hq_phase_cola", v);
  }
  async function hqAsk() {
    if (!$("hq-bar").hidden) { hqHide(); return; }
    const r = await mic.call("hq", "info", S.folder);
    if (!r || !r.ok) { snack(errText(r)); return; }
    if (r.status && r.status.state === "running") { hqPoll(); return; }
    $("hq-btn").setAttribute("aria-pressed", "true");
    const vars = { min: r.minutes, mb: r.download_mb };
    const ask = r.download_mb ? mic.t("library.hq_ask_dl", vars) : mic.t("library.hq_ask", vars);
    hqShow(ask,
      [mic.t("library.hq_go"), hqStart], [mic.t("library.hq_cancel"), hqHide]);
  }
  async function hqStart() {
    const r = await mic.call("hq", "start", S.folder);
    if (!r || !r.ok) { snack(errText(r)); hqHide(); return; }
    hqPoll();
  }
  async function hqPoll() {
    const folder = S.folder;
    const r = await mic.call("hq", "status", folder);
    if (folder !== S.folder) return;
    const st = (r && r.ok && r.status) || null;
    if (st && st.state === "running") {
      const pct = Math.round((st.pct || 0) * 100);
      hqShow(hqPhase(st.fase, { pct }), null,
        [mic.t("library.hq_cancel"), () => mic.call("hq", "cancel", folder)]);
      const bar = $("hq-progress");
      bar.hidden = false;
      bar.style.setProperty("--p", String(st.pct || 0));
      HQ.timer = setTimeout(hqPoll, 1500);
      return;
    }
    hqHide();
    if (st && st.state === "done") { snack(mic.t("library.hq_done")); await openMeeting(folder); return; }
    if (st && st.state === "failed") {
      snack(st.error === "HqRejected" ? mic.t("library.hq_rejected") : mic.t("library.hq_failed"));
    }
    if (st && st.state === "cancelled") snack(mic.t("library.hq_cancelled"));
    if (r && r.previa) hqOffer();
  }
  async function hqRestore() {
    const r = await mic.call("hq", "restore", S.folder);
    if (!r || !r.ok) { snack(errText(r)); return; }
    snack(mic.t("library.hq_restored"));
    await openMeeting(S.folder);
  }

  function renderTracks() {
    const box = $("tracks");
    box.replaceChildren();
    const defs = [["yo", "mic", "library.track_me"], ["remoto", "speaker", "library.track_them"]];
    for (const [k, icon, key] of defs) {
      if (!player.has[k]) continue;
      const b = el("button", "tr-btn");
      b.type = "button";
      b.innerHTML = mic.icon(icon);
      b.setAttribute("aria-label", mic.t(key));
      b.setAttribute("aria-pressed", String(!player.isMuted(k)));
      b.title = mic.t(key) + " · " + mic.t("library.track_toggle");
      b.addEventListener("click", () => {
        player.setMuted(k, !player.isMuted(k));
        b.setAttribute("aria-pressed", String(!player.isMuted(k)));
      });
      box.append(b);
    }
  }

  function renderTicks(tr) {
    const ticks = $("ticks");
    ticks.replaceChildren();
    const dur = player.duration || tr.meeting.duration_s || 1;
    for (const m of tr.markers) {
      const b = document.createElement("b");
      b.style.left = Math.min(100, (m.t / dur) * 100) + "%";
      ticks.append(b);
    }
  }

  function renderSpeakers() {
    const box = $("speakers");
    box.replaceChildren();
    for (const sp of S.data.transcript.speakers) {
      const chip = el("div", "spk" + (sp.id === "me" ? " me" : ""));
      chip.setAttribute("role", "listitem");
      chip.style.setProperty("--c", colorOf(sp.id));
      chip.append(el("span", "dotc"));
      const nameBtn = el("button", "name", sp.name);
      nameBtn.type = "button";
      if (sp.id !== "me" && S.canEdit) {
        nameBtn.title = mic.t("library.rename_hint");
        nameBtn.addEventListener("click", () => editName(chip, nameBtn, sp));
      } else {
        nameBtn.disabled = true;
        nameBtn.style.opacity = "1";
      }
      chip.append(nameBtn);
      if (S.vpNames.has(sp.name)) {
        const vp = el("span", "vp");
        vp.innerHTML = mic.icon("shield");
        vp.title = mic.t("library.has_voiceprint");
        chip.append(vp);
      }
      if (sp.id !== "me" && S.canEdit) {
        const more = el("button", "icon-btn more");
        more.type = "button";
        more.innerHTML = mic.icon("chevdown");
        more.setAttribute("aria-label", mic.t("library.speaker_menu", { name: sp.name }));
        more.setAttribute("aria-haspopup", "menu");
        more.addEventListener("click", (e) => { e.stopPropagation(); speakerMenu(more, sp); });
        chip.append(more);
      }
      box.append(chip);
    }
  }

  function editName(chip, btn, sp) {
    const input = el("input");
    input.type = "text";
    input.value = sp.name;
    input.maxLength = 60;
    input.setAttribute("aria-label", mic.t("library.rename_label", { name: sp.name }));
    btn.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const finish = async (commit) => {
      if (done) return;
      done = true;
      const v = input.value.trim();
      if (commit && v && v !== sp.name) {
        const r = await mic.call("rename_speaker", S.folder, sp.id, v);
        if (r && r.ok) { applyUpdate(r); snack(mic.t("library.renamed", { name: v })); refreshListEntry(); return; }
        snack(errText(r));
      }
      renderSpeakers();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
  }

  async function refreshListEntry() {
    S.meetings = (await mic.call("list_meetings", 500, 0)) || S.meetings;
    if (!S.query) renderList();
  }

  function applyUpdate(r) {
    S.data = r;
    S.segs = r.transcript.segments;
    renderSpeakers();
    renderTranscript();
  }

  // ---- speaker menu / merge / voiceprints
  let menuClose = null;
  function closeMenu() {
    const m = $("menu");
    m.hidden = true;
    m.replaceChildren();
    if (menuClose) { document.removeEventListener("pointerdown", menuClose, true); menuClose = null; }
  }
  function showMenu(anchor, items) {
    closeMenu();
    const m = $("menu");
    for (const it of items) {
      if (it.hr) { m.append(el("hr")); continue; }
      if (it.h) { m.append(el("div", "h", it.h)); continue; }
      const b = el("button", it.danger ? "danger" : "");
      b.type = "button";
      b.setAttribute("role", "menuitem");
      if (it.icon) b.innerHTML = mic.icon(it.icon);
      b.append(el("span", "", it.label));
      b.addEventListener("click", () => { closeMenu(); it.run(); });
      m.append(b);
    }
    m.hidden = false;
    const r = anchor.getBoundingClientRect();
    const mw = m.offsetWidth, mh = m.offsetHeight;
    m.style.left = Math.max(8, Math.min(r.left, innerWidth - mw - 8)) + "px";
    m.style.top = Math.min(r.bottom + 6, innerHeight - mh - 8) + "px";
    menuClose = (e) => { if (!m.contains(e.target)) closeMenu(); };
    setTimeout(() => document.addEventListener("pointerdown", menuClose, true), 0);
    const first = m.querySelector("button");
    if (first) first.focus();
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("menu").hidden) { closeMenu(); } });

  function speakerMenu(anchor, sp) {
    const others = S.data.transcript.speakers.filter((x) => x.id !== sp.id && x.id !== "me");
    const items = [{ icon: "edit", label: mic.t("library.rename"), run: () => {
      const chip = anchor.closest(".spk");
      editName(chip, chip.querySelector(".name"), sp);
    } }];
    if (others.length) {
      items.push({ hr: true }, { h: mic.t("library.merge_into", { name: sp.name }) });
      for (const o of others) items.push({ icon: "merge", label: o.name, run: () => confirmMerge(sp, o) });
    }
    if (S.vpEnabled) {
      items.push({ hr: true });
      if (S.vpNames.has(sp.name)) {
        items.push({ icon: "trash", danger: true, label: mic.t("library.delete_voiceprint", { name: sp.name }), run: () => deleteVoiceprint(sp.name) });
      } else {
        items.push({ icon: "shield", label: mic.t("library.save_voiceprint"), run: () => consentThenSave(sp) });
      }
    }
    showMenu(anchor, items);
  }

  async function confirmMerge(from, into) {
    const dlg = $("dlg-merge");
    $("dm-text").textContent = mic.t("library.merge.text", { from: from.name, into: into.name });
    const ok = await openDialog(dlg);
    if (!ok) return;
    const r = await mic.call("merge_speakers", S.folder, into.id, from.id);
    if (r && r.ok) { applyUpdate(r); snack(mic.t("library.merged", { from: from.name, into: into.name })); refreshListEntry(); }
    else snack(errText(r));
  }

  function openDialog(dlg) {
    return new Promise((resolve) => {
      dlg.returnValue = "";
      dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
      dlg.showModal();
    });
  }

  async function consentThenSave(sp) {
    const dlg = $("dlg-consent");
    $("dc-person").textContent = sp.name;
    $("dc-check").checked = false;
    $("dc-ok").disabled = true;
    const ok = await openDialog(dlg);
    if (!ok) return;
    const r = await mic.call("save_voiceprint", S.folder, sp.id, true);
    if (r && r.ok) {
      S.vpNames = new Set((r.voiceprints || []).map((v) => v.name));
      renderSpeakers();
      snack(mic.t("library.voiceprint_saved", { name: sp.name }));
    } else snack(errText(r));
  }
  $("dc-check").addEventListener("change", (e) => { $("dc-ok").disabled = !e.target.checked; });

  async function deleteVoiceprint(name) {
    const r = await mic.call("delete_voiceprint", name);
    if (r && r.ok) {
      S.vpNames = new Set((r.voiceprints || []).map((v) => v.name));
      renderSpeakers();
      snack(mic.t("library.voiceprint_deleted", { name }));
    } else snack(errText(r));
  }

  // ---- markers
  function renderMarkers() {
    const tr = S.data.transcript;
    const box = $("markers");
    box.hidden = !tr.markers.length;
    const list = $("marker-list");
    list.replaceChildren();
    for (const m of tr.markers) {
      const b = el("button", "mk");
      b.type = "button";
      b.append(el("time", "", mic.fmtClock(m.t)), el("span", "", m.label || mic.t("library.marker_unnamed")));
      b.addEventListener("click", () => { jumpTo(m.t, true); });
      list.append(b);
    }
  }

  // ---- transcript
  function markerSegIndexes(tr) {
    const out = new Set();
    for (const m of tr.markers) {
      let idx = -1;
      for (let i = 0; i < tr.segments.length; i++) if (tr.segments[i].start <= m.t) idx = i; else break;
      if (idx < 0 && tr.segments.length) idx = 0;
      if (idx >= 0) out.add(idx);
    }
    return out;
  }

  /** Phrase text: the AI-corrected words get a faint underline with the heard words on hover;
   *  with «Original» on, the ASR text as it was. Search highlights apply inside each piece. */
  function fillText(tx, s) {
    const q = S.highlightQuery;
    if (S.showOriginal && typeof s.text_original === "string") { highlightInto(tx, s.text_original, q); return; }
    const fix = Array.isArray(s.fix) ? s.fix : [];
    if (!fix.length) { highlightInto(tx, s.text, q); return; }
    let pos = 0;
    for (const [a, b, o] of fix) {
      if (a < pos || b > s.text.length) continue;
      if (a > pos) { const t = el("span"); highlightInto(t, s.text.slice(pos, a), q); tx.append(...t.childNodes); }
      const f = el("span", "fx");
      f.title = mic.t("library.fixed_from", { o: o || "—" });
      highlightInto(f, s.text.slice(a, b), q);
      tx.append(f);
      pos = b;
    }
    const t = el("span");
    highlightInto(t, s.text.slice(pos), q);
    tx.append(...t.childNodes);
  }

  function renderOriginalToggle() {
    const b = $("orig-toggle");
    const has = S.data.transcript.segments.some((s) => typeof s.text_original === "string");
    b.hidden = !has;
    if (!has) S.showOriginal = false;
    b.setAttribute("aria-pressed", String(!!S.showOriginal));
    $("tr").classList.toggle("orig", !!S.showOriginal);
    b.onclick = () => { S.showOriginal = !S.showOriginal; renderOriginalToggle(); renderTranscript(); };
  }

  function renderTranscript() {
    const tr = S.data.transcript;
    const box = $("tr");
    box.replaceChildren(...[$("sec")].filter(Boolean));   // the Secretaria panel stays on top
    S.playingIdx = -1;
    const marks = markerSegIndexes(tr);
    let prev = null;
    tr.segments.forEach((s, i) => {
      const row = el("div", "ph" + (marks.has(i) ? " hasmark" : "") + (s.speaker === prev ? " same" : ""));
      prev = s.speaker;
      row.dataset.i = String(i);
      row.setAttribute("role", "button");
      row.tabIndex = 0;
      row.style.setProperty("--c", colorOf(s.speaker));
      row.setAttribute("aria-label", mic.t("library.phrase_label", { t: mic.fmtClock(s.start), who: nameOf(s.speaker), text: s.text }));
      row.append(el("span", "ts", mic.fmtClock(s.start)));
      const who = el("span", "who");
      who.append(el("i"), el("span", "", nameOf(s.speaker)));
      row.append(who);
      const tx = el("span", "tx");
      fillText(tx, s);
      row.append(tx);
      if (marks.has(i)) {
        const f = el("span", "flag");
        f.innerHTML = mic.icon("flag");
        f.title = mic.t("library.markers");
        row.append(f);
      }
      const play = () => { jumpTo(s.start, true); };
      row.addEventListener("click", (e) => { if (!window.getSelection().toString()) play(); });
      row.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); play(); } });
      box.append(row);
    });
    box.scrollTop = 0;
  }

  function precedingIndex(t) {
    const segs = S.segs;
    let lo = 0, hi = segs.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (segs[mid].start <= t + 0.05) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }
  function segIndexAt(t) {
    const ans = precedingIndex(t);
    return ans >= 0 && t > S.segs[ans].end + 1.5 ? -1 : ans;
  }

  function focusPhraseAt(t, flash) {
    const i = precedingIndex(t + 0.1);
    const row = i >= 0 ? $("tr").querySelector('.ph[data-i="' + i + '"]') : null;
    if (!row) return;
    row.scrollIntoView({ block: "center" });
    if (flash) { row.classList.remove("found"); void row.offsetWidth; row.classList.add("found"); }
  }

  function jumpTo(t, play) {
    if (player.available) {
      player.seek(t);
      if (play) player.play();
    }
    focusPhraseAt(t, false);
  }

  // ------------------------------------------------------------------ transport UI
  function updateTime(t) {
    const dur = player.duration || (S.data ? S.data.transcript.meeting.duration_s : 0) || 1;
    const p = Math.max(0, Math.min(1, t / dur));
    $("scrub").style.setProperty("--p", p.toFixed(4));
    $("scrub").setAttribute("aria-valuenow", String(Math.round(p * 100)));
    $("scrub").setAttribute("aria-valuetext", mic.fmtClock(t));
    $("clock").textContent = mic.fmtClock(t);
    const i = segIndexAt(t);
    if (i !== S.playingIdx) {
      const box = $("tr");
      const old = box.querySelector(".ph.playing");
      if (old) { old.classList.remove("playing"); old.removeAttribute("aria-current"); }
      S.playingIdx = i;
      if (i >= 0) {
        const row = box.querySelector('.ph[data-i="' + i + '"]');
        if (row) {
          row.classList.add("playing");
          row.setAttribute("aria-current", "true");
          if (player.playing && performance.now() - S.userScrollAt > 3000) row.scrollIntoView({ block: "center" });
        }
      }
    }
  }
  function updateState(playing) {
    const b = $("play");
    b.setAttribute("aria-pressed", String(playing));
    const label = mic.t(playing ? "library.pause" : "library.play");
    b.setAttribute("aria-label", label);
    b.title = label;
  }
  player.on(updateTime, updateState);
  $("play").querySelector(".swap").innerHTML = mic.icon("play", "a") + mic.icon("pause", "b");
  $("tr").addEventListener("wheel", () => { S.userScrollAt = performance.now(); }, { passive: true });
  $("play").addEventListener("click", () => (player.playing ? player.pause() : player.play()));

  // scrubbing
  (() => {
    const sc = $("scrub");
    let down = false;
    const at = (e) => {
      const r = sc.getBoundingClientRect();
      return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * (player.duration || 0);
    };
    sc.addEventListener("pointerdown", (e) => { down = true; sc.setPointerCapture(e.pointerId); player.seek(at(e)); });
    sc.addEventListener("pointermove", (e) => { if (down) player.seek(at(e)); });
    sc.addEventListener("pointerup", () => { down = false; });
    sc.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight") { e.preventDefault(); player.seek(player.time + 5); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); player.seek(player.time - 5); }
    });
  })();

  document.querySelectorAll("#rate button").forEach((b) => b.addEventListener("click", () => {
    player.setRate(parseFloat(b.dataset.rate));
    document.querySelectorAll("#rate button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  }));

  // ------------------------------------------------------------------ keyboard
  document.addEventListener("keydown", (e) => {
    const tag = e.target.tagName;
    const typing = tag === "INPUT" || tag === "TEXTAREA" || e.target.isContentEditable;
    if (e.key === "/" && !typing) { e.preventDefault(); $("q").focus(); $("q").select(); return; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("q").focus(); $("q").select(); return; }
    if (e.key === "Escape" && e.target === $("q")) { $("q").value = ""; onQuery(); $("q").blur(); return; }
    if (typing || !player.available) return;
    if (e.key === " " && tag !== "BUTTON" && !e.target.getAttribute("role")) { e.preventDefault(); player.playing ? player.pause() : player.play(); }
    else if (e.key === "ArrowRight" && tag !== "INPUT" && e.target.id !== "scrub") { player.seek(player.time + 5); }
    else if (e.key === "ArrowLeft" && e.target.id !== "scrub") { player.seek(player.time - 5); }
  });
  $("q").addEventListener("input", onQuery);
  $("settings-btn").addEventListener("click", () => showSettings());
  $("q-clear").addEventListener("click", () => { $("q").value = ""; onQuery(); $("q").focus(); });

  // ------------------------------------------------------------------ «Descartadas»
  const heldBox = $("held"), heldList = $("held-list"), heldHead = $("held-h");
  heldHead.addEventListener("click", () => {
    const open = heldHead.getAttribute("aria-expanded") !== "true";
    heldHead.setAttribute("aria-expanded", String(open));
    heldList.hidden = !open;
  });
  function daysLeft(iso) {
    const ms = new Date(iso) - new Date();
    return Math.max(0, Math.ceil(ms / 86400000));
  }
  function renderHeld(items) {
    S.held = items || [];
    heldBox.hidden = !S.held.length;
    $("held-n").textContent = S.held.length ? String(S.held.length) : "";
    heldList.replaceChildren();
    for (const h of S.held) {
      const row = el("div", "held-row");
      const txt = el("div", "");
      txt.append(el("div", "held-t", h.title || mic.t("library.held.untitled")));
      const when = h.started_at ? mic.fmtDate(h.started_at) : "";
      const left = mic.t("library.held.days_left", { n: daysLeft(h.expires_at) });
      txt.append(el("div", "held-s tnum", [when, mic.fmtDuration(h.duration_s), left].filter(Boolean).join(" · ")));
      const rec = el("button", "held-rec", mic.t("library.held.recover"));
      rec.type = "button";
      rec.title = mic.t("library.held.recover_tip");
      rec.addEventListener("click", async () => {
        rec.disabled = true;
        const r = await mic.call("recover_held", h.name);
        if (r && r.ok) { renderHeld(r.held); snack(mic.t("library.held.recovered")); }
        else { rec.disabled = false; snack(mic.t((r && r.error) || "library.err.op_failed")); }
      });
      const del = el("button", "held-del");
      del.type = "button";
      del.innerHTML = mic.icon("x");
      del.setAttribute("aria-label", mic.t("library.held.delete"));
      del.title = mic.t("library.held.delete");
      del.addEventListener("click", async () => {
        const r = await mic.call("delete_held", h.name);
        if (r && r.ok) renderHeld(r.held);
      });
      row.append(txt, rec, del);
      heldList.append(row);
    }
  }

  // ------------------------------------------------------------------ Ajustes in this window
  function settingsFrame() {
    const v = $("settings-view");
    let f = v.querySelector("iframe");
    if (!f) {
      const root = document.documentElement;
      const p = new URLSearchParams({ embed: "1" });
      if (root.dataset.theme) p.set("theme", root.dataset.theme);
      if (root.dataset.motion === "off") p.set("motion", "off");
      if (root.dataset.backdrop) p.set("backdrop", root.dataset.backdrop);
      f = document.createElement("iframe");
      f.src = "settings.html?" + p.toString();
      f.title = mic.t("library.open_settings");
      v.append(f);
    }
    return f;
  }
  function showSettings() {
    const v = $("settings-view");
    const f = settingsFrame();
    const app = document.querySelector(".app");
    if (window.micDither && mic.motion && !v.classList.contains("on")) {
      // the dithered wipe (dither.js) covers the window, the view swaps underneath, it uncovers
      window.micDither.wipe(() => {
        v.hidden = false; app.inert = true; app.classList.add("behind"); v.classList.add("on");
        try { f.contentWindow.focus(); } catch (e) { /* not loaded yet */ }
      });
      return;
    }
    v.hidden = false;
    app.inert = true;
    app.classList.add("behind");
    setTimeout(() => {
      v.classList.add("on");
      try { f.contentWindow.focus(); } catch (e) { /* not loaded yet */ }
    }, 16);
  }
  function hideSettings() {
    const v = $("settings-view");
    const app = document.querySelector(".app");
    if (window.micDither && mic.motion && v.classList.contains("on")) {
      window.micDither.wipe(() => {
        v.classList.remove("on"); v.hidden = true; app.inert = false; app.classList.remove("behind");
        $("settings-btn").focus({ preventScroll: true, focusVisible: false });
      });
      return;
    }
    v.classList.remove("on");
    app.inert = false;
    app.classList.remove("behind");
    setTimeout(() => { if (!v.classList.contains("on")) v.hidden = true; }, mic.dur(200));
    $("settings-btn").focus({ preventScroll: true, focusVisible: false });
  }
  // the host pushes theme/motion to this window only: mirror them into the embedded Ajustes
  document.addEventListener("mic:theme", () => {
    const f = $("settings-view").querySelector("iframe");
    const w = f && f.contentWindow;
    if (w && w.mic) w.mic.setTheme(document.documentElement.dataset.theme);
  });

  // ------------------------------------------------------------------ boot
  const api = (window.micnaga = window.micnaga || {});
  api.library = {
    open(folder, t) { return openMeeting(folder, typeof t === "number" ? t : undefined); },
    get player() { return player; },
    jumpTo,
    /** after the Secretaria rewrote the transcript (names, words): reload it in place */
    async reload() {
      const r = await mic.call("get_meeting", S.folder);
      if (r && r.ok) { applyUpdate(r); renderMarkers(); refreshListEntry(); }
    },
    refreshList: refreshListEntry,
    showSettings,
    hideSettings,
  };

  (async () => {
    await mic.i18nReady;
    const boot = await mic.call("bootstrap");
    if (boot) { S.canEdit = !!boot.can_edit; S.vpEnabled = !!boot.voiceprints_enabled; S.hq = !!boot.hq; }
    if (S.vpEnabled && S.canEdit) {
      const vps = await mic.call("list_voiceprints");
      S.vpNames = new Set((vps || []).map((v) => v.name));
    }
    S.meetings = (await mic.call("list_meetings", 500, 0)) || [];
    renderList();
    if (boot && boot.has_held) renderHeld(await mic.call("list_held"));
    const q = mic.params;
    if (q.get("folder")) await openMeeting(q.get("folder"), q.get("t") ? parseFloat(q.get("t")) : undefined);
    else if (q.get("select") === "first" && S.meetings.length) await openMeeting(S.meetings[0].folder);
    if (q.get("view") === "settings") showSettings();
    if (q.get("q")) { $("q").value = q.get("q"); onQuery(); }
  })();
})();
