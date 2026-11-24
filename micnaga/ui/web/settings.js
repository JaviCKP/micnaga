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
    "secretaria.horas_nudge": "odoo",
  };

  const S = { schema: [], cur: {}, orig: {}, hasDiag: false, hasVp: false, hasSec: false, hasRemote: false, groupEls: {}, invalid: new Set(), diagTimer: 0, remoteTimer: 0 };

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
    if (g.id === "privacy" && S.hasRemote) pane.append(remoteCard());
    if (g.id === "privacy" && S.hasVp) pane.append(voiceprintCard());
    if (g.id === "privacy") pane.append(legalCard());
    if (g.id === "processing") pane.append(modelsCard());
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

  function remoteCard() {
    const card = group(mic.t("settings.remote.title"));
    const form = el("form", "frow col");
    const intro = el("p", "empty-note", mic.t("settings.remote.intro"));
    const label = el("label", "flabel", mic.t("settings.remote.url"));
    const url = el("input", "field-input wide");
    url.id = "remote-url";
    url.type = "url";
    url.required = true;
    url.maxLength = 500;
    url.spellcheck = false;
    label.htmlFor = url.id;
    const keyLabel = el("label", "flabel", mic.t("settings.remote.key"));
    const key = el("input", "field-input wide");
    key.id = "remote-key";
    key.type = "password";
    key.required = true;
    key.maxLength = 60;
    key.autocomplete = "off";
    keyLabel.htmlFor = key.id;
    const connect = el("button", "btn btn-sm btn-primary", mic.t("settings.remote.connect"));
    connect.type = "submit";
    form.append(intro, label, url, keyLabel, key, connect);

    const linked = el("div", "frow col");
    linked.hidden = true;
    const workspace = el("div", "flabel");
    const detail = el("input", "field-input wide");
    detail.readOnly = true;
    detail.setAttribute("aria-label", mic.t("settings.remote.url"));
    detail.addEventListener("focus", () => detail.select());
    const controls = el("div", "ctl");
    const claude = el("button", "btn btn-sm btn-primary", mic.t("settings.remote.claude"));
    const refresh = el("button", "btn btn-sm", mic.t("settings.remote.refresh"));
    const disconnect = el("button", "btn btn-sm btn-ghost", mic.t("settings.remote.disconnect"));
    controls.append(claude, refresh, disconnect);
    linked.append(workspace, detail, controls, el("p", "empty-note", mic.t("settings.remote.claude_help")));
    const status = el("p", "frow empty-note");
    status.setAttribute("role", "status");
    card.append(form, linked, status);

    const states = {
      off: mic.t("settings.remote.off"), ready: mic.t("settings.remote.ready"),
      pending: mic.t("settings.remote.pending"), offline: mic.t("settings.remote.offline"),
      paused: mic.t("settings.remote.paused"), credentials: mic.t("settings.remote.credentials"),
      error: mic.t("settings.remote.error"), library_changed: mic.t("settings.remote.library_changed"),
    };
    let working = false;
    let problem = "";
    function paint(r) {
      if (!r || !r.ok) return;
      form.hidden = r.state !== "off";
      linked.hidden = !form.hidden;
      if (r.url) detail.value = r.url;
      workspace.textContent = r.workspace || "";
      status.textContent = problem || states[r.state] || states.error;
    }
    async function action(name, ...args) {
      if (working) return;
      const read = name === "status";
      if (!read) {
        working = true;
        problem = "";
        [connect, claude, refresh, disconnect].forEach((b) => { b.disabled = true; });
      }
      try {
        const r = await mic.call("remote_library", name, ...args);
        if (read && working) return;
        if (r && r.ok) paint(r);
        else if (!read) {
          problem = mic.t((r && r.error) || "settings.remote.unavailable");
          status.textContent = problem;
        }
      } catch (e) {
        if (!read) status.textContent = mic.t("settings.remote.unavailable");
      } finally {
        if (!read) {
          working = false;
          [connect, claude, refresh, disconnect].forEach((b) => { b.disabled = false; });
        }
      }
    }
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const value = key.value;
      key.value = "";
      action("connect", url.value, value);
    });
    claude.addEventListener("click", () => action("claude"));
    refresh.addEventListener("click", () => action("refresh"));
    disconnect.addEventListener("click", () => action("disconnect"));
    S.pollRemote = () => action("status");
    return card.parentElement;
  }

  // ------------------------------------------------------------------ secretaria
  const sec = (action, args) => mic.call("secretaria", action, args || {});

  /** A status line: live dot + text, an optional action on the right. With `card`, the same
      state also lights a dot beside that card's title (Claude, Odoo and Google read alike). */
  function statusRow(card) {
    const row = el("div", "frow sec-status");
    const left = el("div", "finfo");
    const dot = el("span", "sig");
    dot.dataset.s = "off";
    const text = el("span", "sec-st-text");
    left.append(dot, text);
    const right = el("div", "ctl");
    row.append(left, right);
    let head = null;
    const h3 = card && card.parentElement && card.parentElement.querySelector(":scope > h3");
    if (h3) {
      head = el("span", "sig sig-h");
      head.dataset.s = "off";
      h3.append(head);
    }
    return {
      row, right,
      set(state, msg) {
        dot.dataset.s = state;
        text.textContent = msg;
        if (head) head.dataset.s = state;
      },
    };
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
    const st = statusRow(claude);
    const test = el("button", "btn btn-sm", mic.t("secretaria.test"));
    test.type = "button";
    st.right.append(test);
    let refresh = () => {};
    const token = secretRow("token", mic.t("secretaria.token"), () => refresh());
    const key = secretRow("api_key", mic.t("secretaria.api_key"), () => refresh());
    claude.prepend(st.row);
    claude.append(token.row, key.row);
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
    const ost = statusRow(odoo);
    const otest = el("button", "btn btn-sm", mic.t("secretaria.test"));
    otest.type = "button";
    ost.right.append(otest);
    const caps = capsRow();
    odoo.prepend(ost.row);
    const nudge = odoo.querySelector('.frow[data-path="secretaria.horas_nudge"]');
    if (nudge) odoo.querySelector('.frow[data-path="secretaria.odoo_login"]').after(okey.row);
    else odoo.append(okey.row);
    odoo.append(caps.row);
    let known = null;   // the last «Probar» this Odoo answered (no network to know it)
    sec("caps").then((r) => {
      caps.set(r && r.ok ? r.capacidades : []);
      if (r && r.ok && r.comprobado && (r.capacidades || []).length) known = r.comprobado;
      refresh();
    });
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
        caps.set(r.capacidades || []);
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
      if (otest.disabled) return;   // a «Probar» is answering right now
      if (s.has_odoo_key && known) ost.set("ok", mic.t("secretaria.odoo_known", { t: shortWhen(known) }));
      else ost.set("off", s.has_odoo_key ? mic.t("secretaria.odoo_untested") : mic.t("secretaria.odoo_none"));
    };
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
    const foot = statusRow(card);
    const check = el("button", "btn btn-sm", mic.t("secretaria.g.check"));
    check.type = "button";
    foot.right.append(check);
    card.prepend(foot.row);
    const gcaps = capsRow();
    card.append(gcaps.row);
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
      const svc = r.services || {};
      const live = Object.keys(svc).filter((k) => svc[k].enabled && svc[k].state === "connected");
      const any = Object.keys(svc).some((k) => svc[k].enabled);
      const when = r.checked ? shortWhen(new Date(r.checked * 1000).toISOString()) : "";
      if (r.checking) foot.set("warn", mic.t("secretaria.g.checking"));
      else if (r.auth !== "session") foot.set("warn", mic.t("secretaria.g.session_only"));
      else if (live.length) foot.set("ok", mic.t("secretaria.g.live", { n: live.length, t: when }));
      else if (r.checked) foot.set(any ? "warn" : "off", mic.t("secretaria.g.checked", { t: when }));
      else foot.set("off", mic.t("secretaria.g.never"));
      gcaps.set(GOOGLE_CAPS.filter((c) => live.includes(c.svc)).map((c) => ({ clave: c.icon, etiqueta: c.label(), nivel: "escritura" })));
      clearTimeout(timer);
      if (r.checking) timer = setTimeout(load, 1000);
      return r;
    }
    check.addEventListener("click", async () => { await sec("google_check"); load(); });
    load().then((r) => { if (r && r.ok && !r.checked && !r.checking) check.click(); });   // first time: detect once
  }

  /** What the Secretaria can do in this Odoo (from «Probar»): icon + short label per area. */
  const CAP_ICON = {
    horas: "clock", tareas: "task", calendario: "calendar", actividades: "bell", gastos: "receipt", contactos: "users", crm: "trend",
    mail: "mail", meet: "video", doc: "doc",
  };
  /** …and with Google, from what is switched on and connected. */
  const GOOGLE_CAPS = [
    { svc: "gmail", icon: "mail", label: () => mic.t("secretaria.g.cap.drafts") },
    { svc: "calendar", icon: "meet", label: () => mic.t("secretaria.g.cap.events") },
    { svc: "drive", icon: "doc", label: () => mic.t("secretaria.g.cap.acta") },
  ];
  function shortWhen(iso) {
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleString("es-ES", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }
  function capsRow() {
    const row = el("div", "frow sec-caps");
    const info = el("div", "finfo");
    info.append(el("div", "flabel", mic.t("secretaria.caps.title")));
    const list = el("div", "sec-caplist");
    info.append(list);
    row.append(info);
    return {
      row,
      set(items) {
        list.replaceChildren();
        row.hidden = !items || !items.length;
        if (!items || !items.length) return;
        for (const c of items) {
          const chip = el("span", "sec-cap" + (c.nivel === "lectura" ? " ro" : ""));
          chip.innerHTML = mic.icon(CAP_ICON[c.clave] || "check");
          chip.append(el("span", "", c.etiqueta));
          if (c.nivel === "lectura") chip.append(el("span", "sec-capro", mic.t("secretaria.caps.reading")));
          list.append(chip);
        }
      },
    };
  }

  /** «Conocer tu Odoo»: when it last read this Odoo (read-only) and what it found, + scan now. */
  function odooKnowRow(onDone) {
    const row = el("div", "frow sec-status");
    row.hidden = true;
    const info = el("div", "finfo");
    const text = el("span", "sec-st-text");
    info.append(text);
    const ctl = el("div", "ctl");
    const scan = el("button", "btn btn-sm", mic.t("secretaria.know.scan"));
    scan.type = "button";
    const view = el("button", "btn btn-sm btn-ghost", mic.t("secretaria.know.view"));
    view.type = "button";
    view.addEventListener("click", () => sec("odoo_learn_open"));
    ctl.append(view, scan);
    row.append(info, ctl);
    let timer = 0;
    let was = false;
    async function load(r) {
      r = r || (await sec("odoo_learn"));
      if (!r || !r.ok || !r.conectado) { row.hidden = true; return; }
      row.hidden = false;
      view.hidden = !r.hay;
      scan.disabled = !!r.escaneando;
      const n = r.contadores || {};
      if (r.escaneando) text.textContent = mic.t("secretaria.know.scanning");
      else if (r.actualizado) text.textContent = mic.t("secretaria.know.done", { t: shortWhen(r.actualizado), e: n.etapas || 0, c: n.clientes || 0, p: n.proyectos || 0 });
      else if (r.error) text.textContent = mic.t("secretaria.know.failed");
      else text.textContent = mic.t("secretaria.know.never");
      clearTimeout(timer);
      if (r.escaneando) timer = setTimeout(load, 1500);
      else if (was) onDone();
      was = !!r.escaneando;
    }
    scan.addEventListener("click", async () => load(await sec("odoo_learn_refresh")));
    load();
    return row;
  }

  function learnedCard() {
    const c = group(mic.t("secretaria.learned"));
    const list = el("div");
    c.append(odooKnowRow(() => load()), list);
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

  // ------------------------------------------------------------------ models (Transcripción)
  const pct = (x) => Math.max(0, Math.min(100, Math.round(x * 100)));
  function etaText(s) {
    if (s === null || s === undefined) return "";
    return s >= 90 ? mic.t("wizard.eta_min", { n: Math.round(s / 60) }) : mic.t("wizard.eta_sec", { n: Math.max(1, Math.round(s)) });
  }
  function errText(key, args, fallback) {
    const t = key ? mic.t(key, args || {}) : "";
    return t && t !== key ? t : fallback;
  }

  /** What the saved configuration needs on disk, and a resumable download with live progress. */
  function modelsCard() {
    const c = group(mic.t("settings.cards.models"));
    const row = el("div", "frow sec-status models-row");
    const info = el("div", "finfo models-info");
    const dot = el("span", "sig");
    dot.dataset.s = "off";
    const text = el("span", "sec-st-text");
    const bar = el("div", "progress models-bar");
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", "100");
    bar.append(el("i"));
    bar.hidden = true;
    const detail = el("span", "models-detail tnum");
    info.append(dot, text);
    const ctl = el("div", "ctl");
    const go = el("button", "btn btn-sm", mic.t("settings.models.download"));
    go.type = "button";
    const stop = el("button", "btn btn-sm btn-ghost", mic.t("wizard.cancel"));
    stop.type = "button";
    ctl.append(stop, go);
    const col = el("div", "models-col");
    col.append(info, bar, detail);
    row.append(col, ctl);
    c.append(row);
    let timer = 0;
    async function load() {
      const r = await mic.call("models_status");
      clearTimeout(timer);
      if (!r) return;
      const j = r.job || {};
      const running = j.state === "running";
      bar.hidden = !running;
      stop.hidden = !running;
      go.hidden = running || (r.ready && j.state !== "failed");
      go.disabled = !r.can_download;
      detail.textContent = "";
      if (running) {
        dot.dataset.s = "warn";
        bar.style.setProperty("--p", (j.overall || 0).toFixed(3));
        bar.setAttribute("aria-valuenow", String(pct(j.overall || 0)));
        text.textContent = j.verifying ? mic.t("wizard.verifying") : mic.t("settings.models.downloading", { pct: pct(j.overall || 0), done: j.done_text, total: j.total_text });
        if (j.speed_text && !j.verifying) detail.textContent = j.speed_text + "/s · " + etaText(j.eta_s);
        timer = setTimeout(load, 700);
      } else if (j.state === "failed") {
        dot.dataset.s = "fail";
        text.textContent = errText(j.error_key, j.error_args, mic.t("models.err.generic"));
        go.textContent = mic.t("wizard.retry");
      } else if (j.state === "cancelled") {
        dot.dataset.s = "warn";
        text.textContent = mic.t("wizard.cancelled");
        go.textContent = mic.t("wizard.resume");
      } else if (r.ready) {
        dot.dataset.s = "ok";
        text.textContent = j.state === "done" ? mic.t("settings.models.done") : mic.t("settings.models.ready", { size: r.total_text });
      } else {
        dot.dataset.s = "warn";
        text.textContent = mic.t("settings.models.missing", { size: r.need_text });
        go.textContent = mic.t("settings.models.download");
      }
    }
    go.addEventListener("click", async () => { go.disabled = true; await mic.call("models_download"); load(); });
    stop.addEventListener("click", async () => { await mic.call("models_cancel"); setTimeout(load, 300); });
    document.addEventListener("mic:saved", load);
    load();
    return c.parentElement;
  }

  // ------------------------------------------------------------------ about: version + updates
  function shortTime(sec) {
    return sec ? shortWhen(new Date(sec * 1000).toISOString()) : "";
  }

  /** «Buscar actualizaciones», the status line and, with a new version, its notes + install. */
  function updatesCard(onStatus) {
    const c = group(mic.t("settings.cards.updates"));
    const row = el("div", "frow sec-status upd-row");
    const col = el("div", "models-col");
    const info = el("div", "finfo");
    const dot = el("span", "sig");
    dot.dataset.s = "off";
    const text = el("span", "sec-st-text");
    text.setAttribute("role", "status");
    text.setAttribute("aria-live", "polite");
    info.append(dot, text);
    const bar = el("div", "progress models-bar");
    bar.append(el("i"));
    bar.hidden = true;
    col.append(info, bar);
    const ctl = el("div", "ctl");
    const check = el("button", "btn btn-sm", mic.t("updates.check"));
    check.type = "button";
    const skip = el("button", "btn btn-sm btn-ghost", mic.t("updates.skip"));
    skip.type = "button";
    const install = el("button", "btn btn-sm btn-primary", mic.t("updates.install"));
    install.type = "button";
    ctl.append(check, skip, install);
    row.append(col, ctl);
    const notes = el("div", "upd-notes");
    const notesTitle = el("h4", "caps");
    const notesBody = el("p", "upd-text");
    notes.append(notesTitle, notesBody);
    notes.hidden = true;
    c.append(row, notes);
    let timer = 0;
    const ACTIVE = ["checking", "downloading", "verifying", "waiting", "installing"];
    function paint(st) {
      if (!st) return;
      onStatus(st);
      clearTimeout(timer);
      const s = st.state;
      const busy = ACTIVE.includes(s);
      c.parentElement.hidden = s === "unavailable";   // no app behind (demo): just the version
      check.hidden = st.available || s === "skipped" || busy || s === "unavailable" || s === "disabled";
      check.disabled = busy;
      install.hidden = !(st.available || s === "skipped") || busy;
      install.disabled = !st.can_install;
      skip.hidden = !st.available || busy;
      bar.hidden = s !== "downloading";
      if (s === "downloading" && st.total) {
        const p = st.done / st.total;
        bar.style.setProperty("--p", p.toFixed(3));
        text.textContent = mic.t("updates.downloading", { version: st.latest, pct: pct(p) });
      } else if (s === "checking") text.textContent = mic.t("updates.checking");
      else if (s === "verifying") text.textContent = mic.t("updates.verifying");
      else if (s === "waiting") text.textContent = mic.t("updates.waiting");
      else if (s === "installing") text.textContent = mic.t("updates.installing");
      else if (s === "error") text.textContent = errText(st.error, {}, mic.t("updates.err.generic"));
      else if (st.available) text.textContent = mic.t("updates.available", { version: st.latest });
      else if (s === "skipped") text.textContent = mic.t("updates.skipped", { version: st.latest });
      else if (s === "uptodate") text.textContent = mic.t("updates.uptodate") + (st.checked_at ? " · " + mic.t("updates.checked_at", { t: shortTime(st.checked_at) }) : "");
      else text.textContent = st.checked_at ? mic.t("updates.checked_at", { t: shortTime(st.checked_at) }) : "";
      dot.dataset.s = s === "error" ? "warn" : st.available || busy ? "warn" : s === "uptodate" ? "ok" : "off";
      if (st.error && s !== "error" && st.available) dot.dataset.s = "warn";
      notes.hidden = !(st.notes && (st.available || s === "skipped" || busy));
      notesTitle.textContent = mic.t("updates.notes_title", { version: st.latest });
      notesBody.textContent = st.notes || "";
      if (busy) timer = setTimeout(async () => paint(await mic.call("update_status")), 700);
    }
    check.addEventListener("click", async () => { check.disabled = true; text.textContent = mic.t("updates.checking"); paint(await mic.call("update_check")); });
    skip.addEventListener("click", async () => paint(await mic.call("update_skip")));
    install.addEventListener("click", async () => { install.disabled = true; paint(await mic.call("update_install")); });
    return { card: c.parentElement, paint };
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
    const ver = el("div", "about-ver tnum");
    hero.append(ver);
    const by = el("div", "by");
    const brand = el("span", "brand");
    brand.innerHTML = '<img class="on-dark" src="assets/bitnaga-on-dark.svg" alt="Bitnaga"><img class="on-light" src="assets/bitnaga-on-light.svg" alt="Bitnaga">';
    by.append(el("span", "", mic.t("settings.about.by")), brand);
    hero.append(by);
    pane.append(hero);
    // «Novedades de la X»: once, right after an update (the notes of the version now running)
    const news = group("");
    const newsTitle = el("h4", "caps");
    const newsBody = el("p", "upd-text");
    news.classList.add("upd-news");
    news.append(newsTitle, newsBody);
    news.parentElement.hidden = true;
    pane.append(news.parentElement);
    const upd = updatesCard((st) => {
      const b = st.build || {};
      const bits = [mic.t("updates.version", { version: st.current })];
      if (b.commit || b.date) bits.push(mic.t("updates.build", { commit: b.commit || "—", date: b.date || "—" }));
      ver.textContent = bits.join(" · ");
      const wn = st.whats_new;
      news.parentElement.hidden = !(wn && wn.notes);
      if (wn && wn.notes) {
        newsTitle.textContent = mic.t("updates.whats_new_title", { version: wn.version });
        newsBody.textContent = wn.notes;
      }
    });
    pane.append(upd.card);
    S.updates = upd;
    const c = group(mic.t("settings.cards.licenses"));
    c.classList.add("about");
    const ICON = { "settings.about.license": "feather", "settings.about.models": "cpu", "settings.about.privacy": "lock" };
    for (const k of ["settings.about.license", "settings.about.models", "settings.about.privacy"]) {
      const line = el("p", "line");
      line.innerHTML = mic.icon(ICON[k]);
      line.append(el("span", "", mic.t(k)));
      c.append(line);
    }
    const updLine = el("p", "line");
    updLine.innerHTML = mic.icon("refresh");
    updLine.append(el("span", "", mic.t("settings.about.updates")));
    c.append(updLine);
    pane.append(c.parentElement);
    return pane;
  }

  // ------------------------------------------------------------------ tabs
  function select(id) {
    document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.id === id)));
    document.querySelectorAll(".pane").forEach((p) => { p.hidden = p.id !== "pane-" + id; });
    clearInterval(S.diagTimer);
    clearInterval(S.remoteTimer);
    if (id === "diagnostics") { pollDiag(); S.diagTimer = setInterval(pollDiag, 1000); }
    if (id === "privacy" && S.pollRemote) { S.pollRemote(); S.remoteTimer = setInterval(S.pollRemote, 2500); }
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
      document.dispatchEvent(new CustomEvent("mic:saved"));   // the models card re-checks
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
    S.hasRemote = !!data.has_remote_sync;
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
    // version + updates: just updated («Novedades», once) or an update under way -> «Acerca de»
    mic.call("update_status").then((st) => {
      if (!st || !S.updates) return;
      S.updates.paint(st);
      const wn = st.whats_new;
      const active = ["downloading", "verifying", "waiting", "installing"].includes(st.state);
      if ((wn && !wn.seen) || active) {
        select("about");
        if (wn && !wn.seen) mic.call("update_seen");
      }
    });
    $("save").addEventListener("click", save);
    $("discard").addEventListener("click", () => location.reload());
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (!$("save").disabled) save(); }
    });
    refreshDirty();
  }
  init();
})();

// ---- inside the library window: «‹ Biblioteca» and Esc go back ---------------------------
(function () {
  "use strict";
  if (!window.mic || !mic.embedded) return;
  const back = () => window.parent.micnaga && window.parent.micnaga.library.hideSettings();
  const btn = document.getElementById("nav-back");
  if (btn) { btn.hidden = false; btn.addEventListener("click", back); }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !e.defaultPrevented && !document.querySelector("dialog[open]")) back();
  });
})();
