/* Settings: the whole of config.toml as a form, generated from the Python schema. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const ICONS = { general: "sliders", detection: "mic", processing: "wave", storage: "folder", privacy: "shield", diagnostics: "activity", about: "info" };
  const CARD_OF = { startup: "ui", ui: "ui" };

  const S = { schema: [], cur: {}, orig: {}, hasDiag: false, hasVp: false, groupEls: {}, invalid: new Set(), diagTimer: 0 };

  const clone = (o) => JSON.parse(JSON.stringify(o));
  const get = (path) => { const [s, k] = path.split("."); return S.cur[s][k]; };
  function set(path, value) {
    const [s, k] = path.split(".");
    S.cur[s][k] = value;
    refreshDirty();
  }
  function refreshDirty() {
    const d = JSON.stringify(S.cur) !== JSON.stringify(S.orig);
    $("savebar").dataset.dirty = String(d);
    $("save").disabled = S.invalid.size > 0;
    if (d) setMsg("");
  }
  function setMsg(text, cls) {
    const m = $("save-msg");
    m.textContent = text;
    m.className = "save-msg" + (cls ? " " + cls : "");
  }

  // ------------------------------------------------------------------ controls
  function control(f) {
    const label = mic.t("settings.f." + f.path);
    const val = get(f.path);
    const wrap = el("div", "ctl");
    if (f.kind === "bool") {
      const sw = el("label", "switch");
      const inp = el("input");
      inp.type = "checkbox";
      inp.checked = !!val;
      inp.setAttribute("role", "switch");
      inp.setAttribute("aria-label", label);
      inp.addEventListener("change", () => set(f.path, inp.checked));
      sw.append(inp, el("span", "track"), el("span", "thumb"));
      wrap.append(sw);
    } else if (f.kind === "int" || f.kind === "float") {
      const inp = el("input", "field-input num");
      inp.type = "number";
      if (f.min !== null) inp.min = f.min;
      if (f.max !== null) inp.max = f.max;
      if (f.step !== null) inp.step = f.step;
      inp.value = val;
      inp.setAttribute("aria-label", label);
      inp.addEventListener("input", () => {
        const row = inp.closest(".frow");
        const n = f.kind === "int" ? parseInt(inp.value, 10) : parseFloat(inp.value);
        const bad = Number.isNaN(n) || (f.min !== null && n < f.min) || (f.max !== null && n > f.max);
        row.dataset.invalid = String(bad);
        if (bad) S.invalid.add(f.path); else { S.invalid.delete(f.path); set(f.path, n); }
        refreshDirty();
      });
      wrap.append(inp);
      if (f.unit) wrap.append(el("span", "unit", f.unit));
    } else if (f.kind === "choice") {
      const sel = el("select", "field-select");
      sel.setAttribute("aria-label", label);
      for (const c of f.choices) {
        const o = el("option", "", mic.t("settings.opt." + f.path + "." + c));
        o.value = c;
        sel.append(o);
      }
      sel.value = val;
      sel.addEventListener("change", () => set(f.path, sel.value));
      wrap.append(sel);
    } else if (f.kind === "list") {
      const ta = el("textarea", "field-input");
      ta.rows = 4;
      ta.value = (val || []).join("\n");
      ta.spellcheck = false;
      ta.setAttribute("aria-label", label);
      ta.addEventListener("input", () => set(f.path, ta.value.split("\n").map((x) => x.trim()).filter(Boolean)));
      wrap.append(ta);
    } else if (f.kind === "hotkey") {
      wrap.append(hotkeyButton(f, val, label));
    } else if (f.kind === "path") {
      const inp = el("input", "field-input wide");
      inp.type = "text";
      inp.value = val;
      inp.spellcheck = false;
      inp.placeholder = f.path === "storage.models_dir" ? mic.t("settings.models_default") : "";
      inp.setAttribute("aria-label", label);
      inp.addEventListener("input", () => set(f.path, inp.value));
      const b = el("button", "btn btn-sm", mic.t("settings.choose"));
      b.type = "button";
      b.addEventListener("click", async () => {
        const p = await mic.call("pick_folder", inp.value);
        if (p) { inp.value = p; set(f.path, p); }
      });
      wrap.append(inp, b);
    } else {
      const inp = el("input", "field-input wide");
      inp.type = "text";
      inp.value = val;
      inp.setAttribute("aria-label", label);
      inp.addEventListener("input", () => set(f.path, inp.value));
      wrap.append(inp);
    }
    return wrap;
  }

  function renderKeys(btn, combo) {
    btn.replaceChildren();
    const parts = String(combo).split("+").filter(Boolean);
    if (!parts.length) { btn.textContent = mic.t("settings.hotkey_none"); return; }
    parts.forEach((p) => btn.append(el("span", "keycap", p.length === 1 ? p.toUpperCase() : p[0].toUpperCase() + p.slice(1))));
  }
  function hotkeyButton(f, val, label) {
    const btn = el("button", "hotkey");
    btn.type = "button";
    btn.setAttribute("aria-label", label + ": " + val);
    renderKeys(btn, val);
    let listening = false;
    const stop = (restore) => {
      listening = false;
      btn.dataset.listening = "false";
      if (restore) renderKeys(btn, get(f.path));
      document.removeEventListener("keydown", onKey, true);
    };
    function onKey(e) {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "Escape") { stop(true); return; }
      if (["Control", "Alt", "Shift", "Meta"].includes(e.key)) return;
      const mods = [];
      if (e.ctrlKey) mods.push("ctrl");
      if (e.altKey) mods.push("alt");
      if (e.shiftKey) mods.push("shift");
      if (!mods.length) return; // un atajo global necesita al menos un modificador
      let key = e.key.length === 1 ? e.key.toLowerCase() : e.key.toLowerCase().replace("arrow", "");
      if (e.code.startsWith("Key")) key = e.code.slice(3).toLowerCase();
      if (e.code.startsWith("Digit")) key = e.code.slice(5);
      const combo = [...mods, key].join("+");
      set(f.path, combo);
      btn.setAttribute("aria-label", label + ": " + combo);
      stop(true);
    }
    btn.addEventListener("click", () => {
      if (listening) { stop(true); return; }
      listening = true;
      btn.dataset.listening = "true";
      btn.textContent = mic.t("settings.hotkey_listening");
      document.addEventListener("keydown", onKey, true);
    });
    btn.addEventListener("blur", () => { if (listening) stop(true); });
    return btn;
  }

  /** A small mark beside a label: its words show on hover / keyboard focus (and to readers). */
  function mark(icon, text) {
    const b = el("button", "fmark " + icon);
    b.type = "button";
    b.innerHTML = mic.icon(icon);
    b.dataset.tip = text;
    b.setAttribute("aria-label", text);
    return b;
  }

  function fieldRow(f) {
    const stacked = f.kind === "list" || f.kind === "path";
    const row = el("div", "frow" + (stacked ? " col" : ""));
    row.dataset.path = f.path;
    const info = el("div", "finfo");
    info.append(el("div", "flabel", mic.t("settings.f." + f.path)));
    const help = mic.t("settings.f." + f.path + ".help");
    if (help) info.append(mark("help", help));                     // the why, on demand
    if (f.restart) info.append(mark("restart", mic.t("settings.restart_note")));
    row.append(info, control(f));
    return row;
  }

  // ------------------------------------------------------------------ panes
  /** A titled group: caption above, rows on one glass card (no card-in-card). */
  function group(title) {
    const wrap = el("section", "group");
    if (title) wrap.append(el("h3", "caps", title));
    const c = el("div", "card");
    wrap.append(c);
    return c;
  }

  function buildGroup(g) {
    const pane = el("section", "pane");
    pane.id = "pane-" + g.id;
    pane.setAttribute("role", "tabpanel");
    pane.setAttribute("aria-labelledby", "tab-" + g.id);
    pane.hidden = true;
    pane.append(el("h2", "display", mic.t("settings.groups." + g.id)));
    const cards = new Map();
    const adv = [];
    for (const f of g.fields) {
      if (f.advanced) { adv.push(f); continue; }
      const sec = CARD_OF[f.path.split(".")[0]] || f.path.split(".")[0];
      if (!cards.has(sec)) {
        const title = mic.t("settings.cards." + sec);
        const c = group(title === mic.t("settings.groups." + g.id) ? "" : title);   // no echo of the pane title
        cards.set(sec, c);
        pane.append(c.parentElement);
      }
      cards.get(sec).append(fieldRow(f));
    }
    if (g.id === "privacy" && S.hasVp) pane.append(voiceprintCard());
    if (g.id === "privacy") pane.append(legalCard());
    if (adv.length) {
      const c = group("");
      const t = el("button", "adv-toggle");
      t.type = "button";
      t.setAttribute("aria-expanded", "false");
      t.innerHTML = mic.icon("chevron");
      t.append(mic.t("settings.advanced"));
      const box = el("div", "adv");
      const body = el("div", "adv-body");
      body.id = "adv-" + g.id;
      t.setAttribute("aria-controls", body.id);
      adv.forEach((f) => body.append(fieldRow(f)));
      box.append(body);
      t.addEventListener("click", () => {
        const open = !box.classList.contains("open");
        box.classList.toggle("open", open);
        t.setAttribute("aria-expanded", String(open));
      });
      c.append(t, box);
      pane.append(c.parentElement);
    }
    return pane;
  }

  function legalCard() {
    const d = el("p", "legal");
    d.innerHTML = mic.icon("shield");
    d.append(el("span", "", mic.t("settings.legal")));
    return d;
  }

  function voiceprintCard() {
    const c = group(mic.t("settings.cards.voiceprints"));
    const list = el("div");
    c.append(list);
    async function load() {
      const vps = (await mic.call("list_voiceprints")) || [];
      list.replaceChildren();
      if (!vps.length) { list.append(el("p", "empty-note", mic.t("settings.voiceprints_none"))); return; }
      for (const v of vps) {
        const r = el("div", "vp");
        r.append(el("div", "av", v.name.slice(0, 1).toUpperCase()));
        const mid = el("div");
        mid.append(el("div", "n", v.name), el("div", "s", v.meetings ? mic.t("settings.voiceprint_meetings", { n: v.meetings }) : ""));
        const b = el("button", "btn btn-sm btn-danger", mic.t("settings.voiceprint_delete"));
        b.type = "button";
        b.setAttribute("aria-label", mic.t("settings.voiceprint_delete_for", { name: v.name }));
        let armed = 0;
        b.addEventListener("click", async () => {
          if (!armed) {
            b.textContent = mic.t("settings.voiceprint_sure");
            armed = setTimeout(() => { armed = 0; b.textContent = mic.t("settings.voiceprint_delete"); }, 3000);
            return;
          }
          clearTimeout(armed);
          const res = await mic.call("delete_voiceprint", v.name);
          if (res && res.ok) load();
          else setMsg(mic.t((res && res.error) || "library.err.op_failed"), "err"), ($("savebar").dataset.visible = "true");
        });
        r.append(mid, b);
        list.append(r);
      }
    }
    load();
    return c.parentElement;
  }

  function diagnosticsPane() {
    const pane = el("section", "pane");
    pane.id = "pane-diagnostics";
    pane.setAttribute("role", "tabpanel");
    pane.setAttribute("aria-labelledby", "tab-diagnostics");
    pane.hidden = true;
    pane.append(el("h2", "display", mic.t("settings.groups.diagnostics")));
    const c = group(mic.t("settings.cards.signals"));
    const ul = el("ul", "diag");
    ul.id = "diag-list";
    ul.setAttribute("aria-live", "off");
    c.append(ul);
    pane.append(c.parentElement);
    return pane;
  }
  async function pollDiag() {
    const list = $("diag-list");
    if (!list) return;
    const items = (await mic.call("diagnostics")) || [];
    list.replaceChildren();
    if (!items.length) { const li = el("li"); li.append(el("span"), el("span", "dl", mic.t("settings.diag_none"))); list.append(li); return; }
    for (const it of items) {
      const li = el("li");
      const dot = el("span", "sig");
      dot.dataset.s = it.state;
      dot.setAttribute("role", "img");
      dot.setAttribute("aria-label", mic.t("settings.diag_state." + it.state));
      li.append(dot, el("span", "dl", it.label), el("span", "dd", it.detail || mic.t("settings.diag_state." + it.state)));
      list.append(li);
    }
  }

  function aboutPane() {
    const pane = el("section", "pane");
    pane.id = "pane-about";
    pane.setAttribute("role", "tabpanel");
    pane.setAttribute("aria-labelledby", "tab-about");
    pane.hidden = true;
    pane.append(el("h2", "display", mic.t("settings.groups.about")));
    const hero = el("div", "card about-hero");
    hero.append(el("div", "word display", "Micnaga"));
    const by = el("div", "by");
    const brand = el("span", "brand");
    brand.innerHTML = '<img class="on-dark" src="assets/bitnaga-on-dark.svg" alt="Bitnaga"><img class="on-light" src="assets/bitnaga-on-light.svg" alt="Bitnaga">';
    by.append(el("span", "", mic.t("settings.about.by")), brand);
    hero.append(by);
    pane.append(hero);
    const c = group(mic.t("settings.cards.licenses"));
    c.classList.add("about");
    const ICON = { "settings.about.license": "feather", "settings.about.models": "cpu", "settings.about.privacy": "lock" };
    for (const k of ["settings.about.license", "settings.about.models", "settings.about.privacy"]) {
      const line = el("p", "line");
      line.innerHTML = mic.icon(ICON[k]);
      line.append(el("span", "", mic.t(k)));
      c.append(line);
    }
    pane.append(c.parentElement);
    return pane;
  }

  // ------------------------------------------------------------------ tabs
  function select(id) {
    document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.id === id)));
    document.querySelectorAll(".pane").forEach((p) => { p.hidden = p.id !== "pane-" + id; });
    clearInterval(S.diagTimer);
    if (id === "diagnostics") { pollDiag(); S.diagTimer = setInterval(pollDiag, 1000); }
    try { sessionStorage.setItem("mic.settings.tab", id); } catch (e) { /* private window */ }
  }

  async function save() {
    $("save").disabled = true;
    const r = await mic.call("save_settings", S.cur);
    $("savebar").dataset.visible = "true";
    if (r && r.ok) {
      S.orig = clone(S.cur);
      $("savebar").dataset.dirty = "false";
      setMsg(mic.t("settings.saved"), "ok");
      setTimeout(() => { if ($("savebar").dataset.dirty === "false") delete $("savebar").dataset.visible; }, 2200);
    } else {
      const detail = r && r.detail ? " (" + r.detail + ")" : "";
      setMsg(mic.t((r && r.error) || "settings.err.save_failed") + detail, "err");
    }
    $("save").disabled = S.invalid.size > 0;
  }

  async function init() {
    await mic.i18nReady;
    const data = await mic.call("get_settings");
    if (!data) return;
    S.schema = data.schema;
    S.cur = clone(data.values);
    S.orig = clone(data.values);
    S.hasDiag = !!data.has_diagnostics;
    S.hasVp = !!data.has_voiceprints;
    const nav = $("nav"), panes = $("panes");
    const ids = [];
    const addTab = (id) => {
      ids.push(id);
      const b = el("button", "tab");
      b.type = "button";
      b.id = "tab-" + id;
      b.dataset.id = id;
      b.setAttribute("role", "tab");
      b.setAttribute("aria-controls", "pane-" + id);
      b.innerHTML = mic.icon(ICONS[id] || "sliders");
      b.append(el("span", "", mic.t("settings.groups." + id + ".nav")));
      b.addEventListener("click", () => select(id));
      b.addEventListener("keydown", (e) => {
        const i = ids.indexOf(id);
        const next = e.key === "ArrowDown" ? ids[(i + 1) % ids.length] : e.key === "ArrowUp" ? ids[(i - 1 + ids.length) % ids.length] : null;
        if (next) { e.preventDefault(); select(next); $("tab-" + next).focus(); }
      });
      nav.append(b);
    };
    for (const g of S.schema) { addTab(g.id); panes.append(buildGroup(g)); }
    if (S.hasDiag) { addTab("diagnostics"); panes.append(diagnosticsPane()); }
    addTab("about");
    panes.append(aboutPane());
    let start = new URLSearchParams(location.search).get("tab");
    try { start = start || sessionStorage.getItem("mic.settings.tab"); } catch (e) { /* ignore */ }
    select(ids.includes(start) ? start : ids[0]);
    $("save").addEventListener("click", save);
    $("discard").addEventListener("click", () => location.reload());
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (!$("save").disabled) save(); }
    });
    refreshDirty();
  }
  init();
})();
