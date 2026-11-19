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
  const ICONS = { general: "sliders", detection: "mic", processing: "wave", storage: "folder", secretaria: "feather", privacy: "shield", diagnostics: "activity", about: "info" };
  const CARD_OF = {
    startup: "ui", ui: "ui",
    "secretaria.odoo_url": "odoo", "secretaria.odoo_db": "odoo", "secretaria.odoo_login": "odoo",
    "secretaria.google_gmail": "google", "secretaria.google_calendar": "google", "secretaria.google_drive": "google",
  };

  const S = { schema: [], cur: {}, orig: {}, hasDiag: false, hasVp: false, hasSec: false, groupEls: {}, invalid: new Set(), diagTimer: 0 };

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
      const sec = CARD_OF[f.path] || CARD_OF[f.path.split(".")[0]] || f.path.split(".")[0];
      if (!cards.has(sec)) {
        const title = mic.t("settings.cards." + sec);
        const c = group(title === mic.t("settings.groups." + g.id) ? "" : title);   // no echo of the pane title
        cards.set(sec, c);
        pane.append(c.parentElement);
      }
      cards.get(sec).append(fieldRow(f));
    }
    if (g.id === "secretaria" && S.hasSec) secretariaExtras(pane, cards);
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

  // ------------------------------------------------------------------ secretaria
  const sec = (action, args) => mic.call("secretaria", action, args || {});

  /** A status line: live dot + text, an optional action on the right. */
  function statusRow() {
    const row = el("div", "frow sec-status");
    const left = el("div", "finfo");
    const dot = el("span", "sig");
    dot.dataset.s = "off";
    const text = el("span", "sec-st-text");
    left.append(dot, text);
    const right = el("div", "ctl");
    row.append(left, right);
    return { row, right, set(state, msg) { dot.dataset.s = state; text.textContent = msg; } };
  }

  /** Secrets go in and never come back: the field only says «guardada». */
  function secretRow(kind, label, onSaved) {
    const row = el("div", "frow");
    const info = el("div", "finfo");
    info.append(el("div", "flabel", label));
    const ctl = el("div", "ctl");
    const inp = el("input", "field-input");
    inp.type = "password";
    inp.autocomplete = "off";
    inp.spellcheck = false;
    inp.setAttribute("aria-label", label);
    const b = el("button", "btn btn-sm", mic.t("secretaria.save_secret"));
    b.type = "button";
    b.addEventListener("click", async () => {
      const r = await sec("set_secret", { kind, value: inp.value || null });
      inp.value = "";
      inp.placeholder = r && r.stored ? mic.t("secretaria.stored") : "";
      onSaved();
    });
    ctl.append(inp, b);
    row.append(info, ctl);
    return { row, inp };
  }

  function secretariaExtras(pane, cards) {
    const title = pane.querySelector("h2");
    const consent = el("p", "legal sec-consent");
    consent.innerHTML = mic.icon("lock");
    consent.append(el("span", "", mic.t("secretaria.consent")));
    title.after(consent);

    // Claude: detected Claude Code session + «Probar»; token / API key only in those modes
    const claude = cards.get("secretaria");
    const st = statusRow();
    const test = el("button", "btn btn-sm", mic.t("secretaria.test"));
    test.type = "button";
    st.right.append(test);
    let refresh = () => {};
    const token = secretRow("token", mic.t("secretaria.token"), () => refresh());
    const key = secretRow("api_key", mic.t("secretaria.api_key"), () => refresh());
    claude.append(st.row, token.row, key.row);
    const showSecret = () => {
      const auth = get("secretaria.auth");
      token.row.hidden = auth !== "token";
      key.row.hidden = auth !== "api_key";
    };
    pane.addEventListener("change", showSecret);
    showSecret();
    test.addEventListener("click", async () => {
      test.disabled = true;
      st.set("warn", mic.t("secretaria.testing"));
      const r = await sec("claude_test");
      test.disabled = false;
      if (r && r.ok) st.set("ok", mic.t("secretaria.test_ok", { title: r.title, s: (r.ms / 1000).toFixed(1) }));
      else st.set("fail", mic.t((r && r.error) || "secretaria.err.failed"));
    });

    // Odoo: the key (never shown back) + «Probar»: who you are and what is readable
    const odoo = cards.get("odoo");
    const okey = secretRow("odoo", mic.t("secretaria.odoo_key"), () => refresh());
    const ost = statusRow();
    const otest = el("button", "btn btn-sm", mic.t("secretaria.test"));
    otest.type = "button";
    ost.right.append(otest);
    odoo.append(okey.row, ost.row);
    otest.addEventListener("click", async () => {
      otest.disabled = true;
      ost.set("warn", mic.t("secretaria.testing"));
      const r = await sec("odoo_test", {
        url: get("secretaria.odoo_url"), db: get("secretaria.odoo_db"), login: get("secretaria.odoo_login"),
        key: okey.inp.value || null,
      });
      otest.disabled = false;
      if (r && r.ok) {
        const names = { "crm.lead": "CRM", "res.partner": mic.t("secretaria.contacts"), "mail.activity": mic.t("secretaria.activities") };
        const readable = Object.entries(r.readable || {}).filter(([m]) => names[m]).map(([m, v]) => names[m] + (v ? " ✓" : " ✗")).join(" · ");
        ost.set("ok", mic.t("secretaria.odoo_ok", { user: r.user || "?", version: r.version || "", readable }));
      } else ost.set("fail", mic.t((r && r.error) || "secretaria.err.failed"));
    });

    refresh = async () => {
      const s = await sec("status");
      if (!s || !s.ok) return;
      if (!s.claude_installed) st.set("fail", mic.t("secretaria.not_installed"));
      else if (s.auth === "session") {
        const d = await sec("claude_detect");
        if (d && d.logged_in) st.set("ok", mic.t("secretaria.session_ok", { plan: d.plan ? " · " + String(d.plan).toUpperCase() : "" }));
        else st.set("warn", mic.t("secretaria.session_missing"));
      } else {
        const has = s.auth === "token" ? s.has_token : s.has_api_key;
        if (has) st.set("ok", mic.t("secretaria.secret_ok"));
        else st.set("warn", mic.t("secretaria.secret_missing"));
      }
      okey.inp.placeholder = s.has_odoo_key ? mic.t("secretaria.stored") : "";
      ost.set(s.has_odoo_key ? "off" : "off", s.has_odoo_key ? mic.t("secretaria.odoo_untested") : mic.t("secretaria.odoo_none"));
    };
    refresh();
    if (cards.get("google")) googleCard(cards.get("google"));
    pane.append(learnedCard());
  }

  /** Google: Gmail / Calendar / Drive rows with a live dot. The connectors live in the user's
      Claude account: «Conectar» opens that page, «Comprobar» asks the local claude again. */
  function googleCard(card) {
    const STATE = {
      connected: ["ok", () => mic.t("secretaria.g.connected")],
      needs_auth: ["warn", () => mic.t("secretaria.g.needs_auth")],
      failed: ["fail", () => mic.t("secretaria.g.failed")],
      absent: ["off", () => mic.t("secretaria.g.absent")],
      unknown: ["off", () => mic.t("secretaria.g.unknown")],
    };
    const rows = {};
    for (const svc of ["gmail", "calendar", "drive"]) {
      const row = card.querySelector('.frow[data-path="secretaria.google_' + svc + '"]');
      if (!row) continue;
      const info = row.querySelector(".finfo");
      const dot = el("span", "sig");
      dot.dataset.s = "off";
      const st = el("span", "g-st");
      info.prepend(dot);
      info.append(st);
      const connect = el("button", "btn btn-sm btn-ghost", mic.t("secretaria.g.connect"));
      connect.type = "button";
      connect.hidden = true;
      connect.addEventListener("click", () => sec("google_open"));
      row.querySelector(".ctl").prepend(connect);
      rows[svc] = { dot, st, connect };
    }
    const foot = statusRow();
    const check = el("button", "btn btn-sm", mic.t("secretaria.g.check"));
    check.type = "button";
    foot.right.append(check);
    card.append(foot.row);
    let timer = 0;
    async function load() {
      const r = await sec("google_status");
      if (!r || !r.ok) return r;
      for (const [svc, v] of Object.entries(r.services || {})) {
        const ui = rows[svc];
        if (!ui) continue;
        const [s, text] = STATE[v.state] || STATE.unknown;
        ui.dot.dataset.s = r.checking ? "warn" : s;
        ui.st.textContent = text();
        ui.connect.hidden = v.state === "connected";
      }
      check.disabled = !!r.checking;
      if (r.checking) foot.set("warn", mic.t("secretaria.g.checking"));
      else if (r.auth !== "session") foot.set("warn", mic.t("secretaria.g.session_only"));
      else if (r.checked) foot.set("off", mic.t("secretaria.g.checked", { t: new Date(r.checked * 1000).toLocaleString("es-ES", { dateStyle: "short", timeStyle: "short" }) }));
      else foot.set("off", mic.t("secretaria.g.never"));
      clearTimeout(timer);
      if (r.checking) timer = setTimeout(load, 1000);
      return r;
    }
    check.addEventListener("click", async () => { await sec("google_check"); load(); });
    load().then((r) => { if (r && r.ok && !r.checked && !r.checking) check.click(); });   // first time: detect once
  }

  function learnedCard() {
    const c = group(mic.t("secretaria.learned"));
    const list = el("div");
    c.append(list);
    async function load(r) {
      r = r || (await sec("learned"));
      list.replaceChildren();
      const items = (r && r.items) || [];
      if (!items.length) { list.append(el("p", "empty-note", mic.t("secretaria.learned_none"))); return; }
      items.forEach((it, i) => {
        const row = el("div", "vp sec-learn" + (it.review ? " review" : ""));
        const mid = el("div");
        mid.append(el("div", "n", it.fact), el("div", "s", it.review ? it.day + " · " + mic.t("secretaria.review") : it.day));
        const b = el("button", "btn btn-sm btn-ghost", mic.t("secretaria.forget"));
        b.type = "button";
        b.addEventListener("click", async () => load(await sec("forget", { index: i })));
        row.append(mid, b);
        list.append(row);
      });
    }
    load();
    return c.parentElement;
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
    S.hasSec = !!data.has_secretaria;
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
