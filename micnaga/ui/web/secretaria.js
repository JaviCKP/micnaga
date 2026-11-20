/* Secretaria in the library viewer: the acta above the transcript, then the proposals.
   Every proposal is a small card (kind glyph, one clear line, one quiet line) that can be ticked,
   edited in place or corrected with a message («mejor el jueves»): Python runs a short read-only
   `claude -p`, re-validates the answer and replaces the item with the same id; the card morphs.
   Talks to Python through pywebview.api.secretaria(action, args). */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const S = {
    folder: null, acta: null, run: null, enabled: false, poll: 0, busy: false,
    open: new Set(),          // items with the field editor open
    ask: new Map(),           // id -> {text, busy, error, note}: the «tell it» line of an item
    undoable: new Set(),      // items revised by a message in this view (one-step «Deshacer»)
    flash: new Map(),         // id -> changed field keys (highlighted once after a revision)
    notes: new Map(),         // id -> short note the model left («No puedo asignar…»)
    plan: { text: "", busy: false, error: null, note: null },
    last: null,               // what «Deshacer» in the bar undoes: "apply" | "plan"
    trail: [],                // research steps seen while the acta was being prepared
    full: false,              // «Acta completa» reading view open (fullacta.js)
  };
  const FA = () => window.micnaga && window.micnaga.fullActa;
  const call = (action, args) => mic.call("secretaria", action, args || {});
  const lib = () => window.micnaga && window.micnaga.library;
  const IT = () => window.micnaga && window.micnaga.items;   // Google items + research (items.js)
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));  // never rAF: occluded WebView2
  /** A response for another meeting (the user moved on while it was in flight): ignore it, so
   *  meeting A's answer never paints over (or re-labels) meeting B's proposals. */
  const stale = (r, folder) => folder !== S.folder || !!(r && r.folder && r.folder !== folder);

  /** A small «01:17 ▸» stamp that plays that moment. */
  function stamp(t) {
    if (typeof t !== "number") return null;
    const b = el("button", "sec-t tnum");
    b.type = "button";
    b.append(mic.fmtClock(t));
    b.insertAdjacentHTML("beforeend", mic.icon("play", "mini"));
    b.setAttribute("aria-label", mic.t("secretaria.play_at", { t: mic.fmtClock(t) }));
    b.addEventListener("click", (e) => { e.stopPropagation(); if (lib()) lib().jumpTo(t, true); });
    return b;
  }
  function fmtDay(iso) {
    if (!iso) return "";
    const d = new Date(String(iso).slice(0, 10) + "T12:00:00");
    return isNaN(d) ? iso : d.toLocaleDateString("es-ES", { weekday: "short", day: "numeric", month: "short" });
  }

  // ------------------------------------------------------------------ status
  const TOOL_KEY = {
    buscar_contactos: "secretaria.st.contacts",
    buscar_empresas: "secretaria.st.companies",
    buscar_oportunidades: "secretaria.st.leads",
    leer_oportunidad: "secretaria.st.lead",
    listar_etapas: "secretaria.st.stages",
    historial_reciente: "secretaria.st.history",
    actividades_pendientes: "secretaria.st.activities",
    buscar_en_reuniones: "secretaria.st.meetings",
    reuniones_con: "secretaria.st.prev",
    leer_acta: "secretaria.st.prev",
    mis_partes_de_horas: "secretaria.st.hours",
    proyectos: "secretaria.st.projects",
    tareas: "secretaria.st.tasks",
    calendario: "secretaria.st.calendar",
    mis_actividades: "secretaria.st.my_activities",
    mis_gastos: "secretaria.st.expenses",
  };
  const SERVER_KEY = {   // Google connectors: one calm line per service
    claude_ai_Gmail: "secretaria.st.gmail",
    claude_ai_Google_Calendar: "secretaria.st.calendar",
    claude_ai_Google_Drive: "secretaria.st.drive",
  };
  // where each step looked (the small trail under the live line)
  const SOURCE_OF = {
    reuniones_con: "acta", leer_acta: "acta", buscar_en_reuniones: "acta",
    claude_ai_Gmail: "gmail", claude_ai_Google_Calendar: "calendar", claude_ai_Google_Drive: "drive",
  };
  function statusText(st) {
    if (!st) return mic.t("secretaria.st.reading");
    if (st.fase === "herramienta") {
      const key = TOOL_KEY[st.tool] || SERVER_KEY[st.servidor];
      if (key) return mic.t(key, { arg: st.arg || "" }).replace("  ", " ");
      return mic.t("secretaria.st.other", { server: st.servidor || "" });
    }
    if (st.fase === "comprobando") return mic.t("secretaria.st.checking");
    if (st.fase === "corrigiendo") return mic.t("secretaria.st.cleaning");
    if (st.fase === "en_cola") return mic.t("secretaria.st.queued");
    if (st.fase === "pensando") return mic.t("secretaria.st.thinking");
    return mic.t("secretaria.st.reading");
  }
  function sourceOf(st) {
    if (!st || st.fase !== "herramienta") return null;
    return SOURCE_OF[st.tool] || SOURCE_OF[st.servidor] || (st.servidor === "micnaga" ? "odoo" : null);
  }

  // ------------------------------------------------------------------ acta
  function heading(title, icon, n) {
    const h = el("h4", "caps sec-h");
    if (icon) h.insertAdjacentHTML("afterbegin", mic.icon(icon, "mini"));
    h.append(title);
    if (n) h.append(el("span", "sec-n tnum", String(n)));
    return h;
  }
  function section(title, items, render, cls, icon) {
    if (!items || !items.length) return null;
    const box = el("div", "sec-block" + (cls ? " " + cls : ""));
    box.append(heading(title, icon, items.length));
    const ul = el("ul", "sec-list");
    items.forEach((it) => ul.append(render(it)));
    box.append(ul);
    return box;
  }
  function line(it, glyph) {
    const li = el("li", "sec-li");
    li.append(el("span", "sec-g", glyph), el("span", "sec-x", it.texto));
    const s = stamp(it.t);
    if (s) li.append(s);
    return li;
  }
  function renderActa(a) {
    const box = el("section", "sec-acta");
    box.setAttribute("aria-label", mic.t("secretaria.acta"));
    const head = el("div", "sec-head");
    head.append(el("span", "sec-badge caps", mic.t("secretaria.acta")));
    const redo = el("button", "icon-btn sec-redo");
    redo.type = "button";
    redo.innerHTML = mic.icon("refresh");
    redo.dataset.tip = mic.t("secretaria.redo");
    redo.dataset.tipPos = "left";
    redo.setAttribute("aria-label", mic.t("secretaria.redo"));
    redo.addEventListener("click", analyze);
    if (FA()) {
      const full = el("button", "btn btn-sm sec-full", mic.t("secretaria.full"));
      full.type = "button";
      full.insertAdjacentHTML("afterbegin", mic.icon("doc", "mini"));
      full.addEventListener("click", () => { S.full = true; render(); });
      head.append(full);
    }
    head.append(redo);
    box.append(head);
    if (a.contexto) {
      const ctx = el("p", "sec-ctx");
      ctx.innerHTML = mic.icon("restart", "mini");
      ctx.append(a.contexto);
      box.append(ctx);
    }
    if (a.resumen && a.resumen.length) box.append(el("p", "sec-sum", a.resumen.join(" ")));
    const since = IT() ? IT().since(a, call, stamp) : null;
    if (since) box.append(since);
    const blocks = [
      section(mic.t("secretaria.decisions"), a.decisiones, (it) => line(it, "·"), "k-dec", "check"),
      section(mic.t("secretaria.tasks"), a.tareas, (t) => {
        const li = el("li", "sec-li task" + (t.es_mia ? " mine" : ""));
        li.append(el("span", "sec-g", "○"));
        const x = el("span", "sec-x");
        x.append(el("b", "", (t.es_mia ? mic.t("secretaria.me") : t.responsable || "—") + " — "), t.texto);
        li.append(x);
        if (t.fecha) li.append(el("span", "sec-date tnum", fmtDay(t.fecha)));
        const s = stamp(t.t);
        if (s) li.append(s);
        return li;
      }, "k-task", "task"),
      section(mic.t("secretaria.open"), a.preguntas_abiertas, (it) => line(it, "?"), "k-open", "help"),
      section(mic.t("secretaria.next"), a.proximos_pasos, (it) => line(it, "→"), "k-next", "arrow"),
    ].filter(Boolean);
    if (IT()) blocks.push(...IT().actaExtras(a, call, stamp));
    if (blocks.length) {
      const grid = el("div", "sec-grid");
      blocks.forEach((b) => grid.append(b));
      box.append(grid);
    }
    const src = IT() && IT().sources(a, call);
    if (src) box.append(src);
    return box;
  }

  // ------------------------------------------------------------------ one proposal, two lines
  function money(n) { return typeof n === "number" ? n.toLocaleString("es-ES", { maximumFractionDigits: 2 }) + " €" : "—"; }
  const hfmt = (h) => (Math.round(h * 100) / 100).toLocaleString("es-ES", { maximumFractionDigits: 2 });
  const KIND_ICON = {
    registrar_horas: "clock", crear_tarea: "task", programar_actividad: "bell", crear_evento: "calendar",
    evento_google: "calendar", crear_gasto: "receipt", nota_en_historial: "note", vincular_a_oportunidad: "note",
    crear_contacto: "user", actualizar_oportunidad: "trend", correo_google: "mail", acta_drive: "doc",
    hablante: "user", palabra: "text",
  };
  /** [main line, quiet facts] — the main line says what will exist; the facts say where/when. */
  function lines(p) {
    const d = p.datos || {};
    const name = (x) => "«" + (x || "#" + d.id) + "»";
    switch (p.tipo) {
      case "hablante": return [mic.t("secretaria.p.speaker", { de: d.de || d.hablante, a: d.a }), [mic.t("secretaria.k.speaker")]];
      case "palabra": return [mic.t("secretaria.p.word", { de: d.de, a: d.a, n: d.n || 1 }), [mic.t("secretaria.k.word")]];
      case "vincular_a_oportunidad": return [mic.t("secretaria.p.link", { name: d.nombre || "#" + d.id }), [mic.t("secretaria.k.note")]];
      case "nota_en_historial": return [mic.t("secretaria.k.note_in", { name: name(d.nombre) }), [mic.t("secretaria.k.note")]];
      case "crear_tarea": return [d.nombre, [d.proyecto, d.fecha ? fmtDay(d.fecha) : null]];
      case "crear_evento": return [d.nombre, [fmtDay(d.fecha) + (d.hora_inicio ? " · " + d.hora_inicio : ""), d.duracion_h ? hfmt(d.duracion_h) + " h" : null]];
      case "crear_gasto": return [d.nombre, [money(d.importe), fmtDay(d.fecha)]];
      case "registrar_horas": return [hfmt(d.horas || 0) + " h · " + (d.proyecto || ""), [d.descripcion]];
      case "crear_contacto": return [d.nombre, [d.cargo, d.empresa, d.email, d.telefono]];
      case "actualizar_oportunidad": {
        const prev = d.anterior || {};
        const parts = [];
        if (d.importe != null) parts.push(money(prev.importe) + " → " + money(d.importe));
        if (d.etapa_id != null) parts.push((prev.etapa || "—") + " → " + (d.etapa || "#" + d.etapa_id));
        if (d.fecha_cierre) parts.push(mic.t("secretaria.p.close", { d: fmtDay(d.fecha_cierre) }));
        return [parts.join(" · ") || mic.t("secretaria.k.lead"), [d.nombre ? name(d.nombre) : null]];
      }
      case "programar_actividad": return [d.resumen, [fmtDay(d.fecha), d.nombre ? name(d.nombre) : null]];
      default: {
        const own = IT() && IT().lines(p);
        return own || [p.titulo || p.tipo, []];
      }
    }
  }
  const WARN = {
    parecido_existe: "secretaria.w.similar",
    cambio_en_odoo: "secretaria.w.changed",
    parecida_pendiente: "secretaria.w.dup_activity",
    sin_comprobar: "secretaria.w.unchecked",
    tarea_parecida: "secretaria.w.task_similar",
    evento_parecido: "secretaria.w.event_similar",
  };
  // which editable fields the main line shows, per kind (the rest live in the quiet line):
  // after a revision, the part that changed lights up once
  const MAIN_FIELDS = {
    crear_tarea: ["nombre"], crear_evento: ["nombre"], crear_gasto: ["nombre"], crear_contacto: ["nombre"],
    programar_actividad: ["resumen"], registrar_horas: ["horas", "proyecto_id"], nota_en_historial: ["destino"],
    actualizar_oportunidad: ["importe", "etapa_id", "fecha_cierre"], evento_google: ["titulo"], correo_google: ["asunto"],
    acta_drive: ["titulo"], hablante: ["a"], palabra: ["a"],
  };
  const inMain = (p, k) => (MAIN_FIELDS[p.tipo] || []).includes(k);

  // ------------------------------------------------------------------ item editor
  // Every item is data-driven: Python sends `campos` (key, type, options list, current value)
  // and re-validates each change with the same rules as the original proposal; the item comes
  // back replaced with the same id. One editor for every kind of item.
  function control(p, f) {
    const own = IT() && IT().control(p, f, (v) => edit(p, { [f.clave]: v }));
    if (own) return own;
    const opts = (S.acta.opciones || {})[f.opciones] || [];
    let c;
    if (f.tipo === "textarea") {
      c = el("textarea", "field-input sec-ta");
      c.rows = f.clave === "cuerpo" ? 6 : 3;
      c.value = f.valor || "";
    } else if (f.tipo === "select") {
      c = el("select", "field-select");
      const list = opts.slice();
      if (f.valor != null && !list.some((o) => String(o.id) === String(f.valor))) {
        list.unshift({ id: f.valor, nombre: (p.datos && (p.datos.proyecto || p.datos.nombre || p.datos.etapa)) || String(f.valor) });
      }
      if (f.valor == null) c.append(el("option", "", "—"));
      for (const o of list) { const op = el("option", "", o.nombre); op.value = String(o.id); c.append(op); }
      c.value = f.valor == null ? "" : String(f.valor);
      c.disabled = list.length < 2 && f.valor != null;
    } else {
      c = el("input", "field-input" + (f.tipo === "number" ? " num" : ""));
      c.type = f.tipo === "text" ? "text" : f.tipo;
      if (f.min != null) c.min = f.min;
      if (f.max != null) c.max = f.max;
      if (f.paso != null) c.step = f.paso;
      c.value = f.valor == null ? "" : String(f.valor);
    }
    c.spellcheck = false;
    c.addEventListener("change", () => {
      let v = c.value;
      if (f.tipo === "number") v = parseFloat(v);
      else if (f.tipo === "select" && f.clave !== "destino") v = v === "" ? null : parseInt(v, 10);
      else if (v === "" && (f.tipo === "date" || f.tipo === "time")) v = null;
      edit(p, { [f.clave]: v });
    });
    return c;
  }

  function chips(p, f) {
    const row = el("div", "sec-inv");
    for (const g of f.valor || []) {
      const b = el("button", "sec-chipbtn", g.nombre);
      b.type = "button";
      b.setAttribute("aria-pressed", String(!!g.on));
      b.addEventListener("click", () => {
        const on = f.valor.filter((x) => (x.id === g.id ? !x.on : x.on)).map((x) => x.id);
        edit(p, { invitados_on: on });
      });
      row.append(b);
    }
    return row;
  }

  function editor(p) {
    const box = el("div", "sec-edit");
    box.dataset.id = p.id;
    const lit = S.flash.get(p.id) || [];
    for (const f of p.campos || []) {
      if (f.tipo === "chips" && !(f.valor && f.valor.length)) continue;
      // rows of buttons go in a div: a <label> would forward a click on its text to the first one
      const lab = el(["chips", "people", "choice"].includes(f.tipo) ? "div" : "label", "sec-f sec-f-" + f.tipo + " k-" + f.clave);
      if (lit.includes(f.clave)) lab.classList.add("lit");
      lab.append(el("span", "sec-fl", mic.t(f.etiqueta || "secretaria.f." + f.clave)));
      lab.append(f.tipo === "chips" ? chips(p, f) : control(p, f));
      box.append(lab);
    }
    return box;
  }

  async function edit(p, changes) {
    const folder = S.folder;
    const r = await call("edit", { folder, id: p.id, cambios: changes });
    if (stale(r, folder)) return;
    if (!r || !r.ok) { snack(mic.t((r && r.error) || "library.err.op_failed")); render(); return; }
    S.acta = r.acta;
    S.undoable.delete(p.id);   // a hand edit closes the message's one-step undo
    S.notes.delete(p.id);
    swap(p.id, { saved: true });
  }

  // ------------------------------------------------------------------ «corregir con un mensaje»
  /** The inline glass line under a card: type, Enter sends, Esc closes. */
  function askBox(p) {
    const st = S.ask.get(p.id);
    const box = el("div", "sec-ask");
    box.dataset.id = p.id;
    const field = el("div", "sec-askf");
    const glyph = el("span", "sec-askg");
    glyph.innerHTML = mic.icon("spark");
    const inp = el("input", "sec-aski");
    inp.type = "text";
    inp.spellcheck = false;
    inp.autocomplete = "off";
    inp.maxLength = 400;
    inp.placeholder = mic.t("secretaria.ask.item");
    inp.setAttribute("aria-label", mic.t("secretaria.ask.item_label"));
    inp.value = st.text || "";
    inp.disabled = !!st.busy;
    inp.dataset.ask = p.id;
    inp.addEventListener("input", () => { st.text = inp.value; st.error = null; box.querySelector(".sec-askerr")?.remove(); });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); refine(p.id); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeAsk(p.id); }
    });
    const send = el("button", "sec-asks");
    send.type = "button";
    send.innerHTML = mic.icon("send");
    send.setAttribute("aria-label", mic.t("secretaria.ask.send"));
    send.addEventListener("click", () => refine(p.id));
    field.append(glyph, inp, send);
    box.append(field);
    const status = el("p", "sec-askst");
    status.setAttribute("role", "status");
    if (st.busy) { status.append(el("span", "sec-spin"), el("span", "sec-askst-t", statusText(st.status))); }
    box.append(status);
    if (st.error) box.append(el("p", "sec-askerr", st.error));
    return box;
  }

  function openAsk(id) {
    if (!S.ask.has(id)) S.ask.set(id, { text: "", busy: false, error: null });
    swap(id);
    const inp = document.querySelector(`.sec-aski[data-ask="${id}"]`);
    if (inp) inp.focus();
  }
  function closeAsk(id) {
    const st = S.ask.get(id);
    if (st && st.busy) return;
    S.ask.delete(id);
    swap(id);
    const b = document.querySelector(`.sec-p[data-id="${id}"] .sec-pask`);
    if (b) b.focus();
  }

  function errorLine(r) {
    const base = mic.t((r && r.error) || "library.err.op_failed");
    return r && r.nota ? r.nota : base;
  }

  async function refine(id) {
    const st = S.ask.get(id);
    if (!st || st.busy || !st.text.trim()) return;
    st.busy = true;
    st.error = null;
    st.status = { fase: "pensando" };
    const li = document.querySelector(`.sec-p[data-id="${id}"]`);
    if (li) li.classList.add("thinking");
    swap(id, { keepThinking: true });
    const folder = S.folder;
    const stop = pollStatus((s) => {
      if (!s || s.id !== id) return;
      st.status = s;
      const t = document.querySelector(`.sec-ask[data-id="${id}"] .sec-askst-t`);
      if (t) crossText(t, statusText(s));
    });
    const r = await call("refine", { folder, id, texto: st.text });
    stop();
    if (stale(r, folder)) return;
    st.busy = false;
    if (!r || !r.ok) {
      st.error = errorLine(r);
      swap(id);
      const inp = document.querySelector(`.sec-aski[data-ask="${id}"]`);
      if (inp) inp.focus();
      return;
    }
    S.acta = r.acta;
    S.ask.delete(id);
    S.undoable.add(id);
    S.flash.set(id, r.cambiados || []);
    if (r.nota) S.notes.set(id, r.nota); else S.notes.delete(id);
    await morph(id);
  }

  async function undoRefine(id) {
    const folder = S.folder;
    const r = await call("refine_undo", { folder, id });
    if (stale(r, folder)) return;
    if (!r || !r.ok) { snack(mic.t((r && r.error) || "library.err.op_failed")); return; }
    S.acta = r.acta;
    S.undoable.delete(id);
    S.notes.delete(id);
    const p = findP(id);
    S.flash.set(id, (p && p.cambiados) || []);
    await morph(id);
  }

  /** While a refine runs, its progress line follows the read-only lookups. */
  function pollStatus(onStatus) {
    let alive = true;
    const folder = S.folder;
    (async () => {
      while (alive && folder === S.folder) {
        await wait(350);
        if (!alive || folder !== S.folder) break;
        const r = await call("refine_status", { folder });
        if (alive && folder === S.folder && r && r.ok) onStatus(r.status);
      }
    })();
    return () => { alive = false; };
  }

  /** Text swap with a short crossfade (status lines). */
  function crossText(node, text) {
    if (node.textContent === text) return;
    if (!mic.motion) { node.textContent = text; return; }
    node.classList.add("swap-out");
    setTimeout(() => { node.textContent = text; node.classList.remove("swap-out"); }, 140);
  }

  /** Replace one card in place: the old one dissolves, the new one sharpens, changed parts glow. */
  async function morph(id) {
    const old = document.querySelector(`.sec-p[data-id="${id}"]`);
    if (old && mic.motion) {
      old.classList.remove("thinking");
      old.classList.add("morph-out");
      await wait(160);
    }
    swap(id, { morph: true });
    setTimeout(() => {
      S.flash.delete(id);
      const n = document.querySelector(`.sec-p[data-id="${id}"]`);
      if (n) n.querySelectorAll(".lit").forEach((x) => x.classList.remove("lit"));
    }, 2400);
    refreshBar();
  }

  // ------------------------------------------------------------------ the list
  function findP(id) { return ((S.acta && S.acta.propuestas) || []).find((p) => p.id === id); }

  function renderItem(p) {
    const li = el("li", "sec-p");
    li.dataset.id = p.id;
    li.dataset.state = p.estado;
    li.dataset.kind = p.tipo;
    if (p.nueva && p.estado === "pendiente") li.classList.add("is-new");
    if (p.motivo) li.title = p.motivo;
    const row = el("div", "sec-row");
    const lab = el("label", "sec-pl");
    const cb = el("input", "sec-cb");
    cb.type = "checkbox";
    cb.checked = p.estado === "pendiente" ? !!p.checked : p.estado === "aplicada";
    cb.disabled = p.estado !== "pendiente";
    cb.dataset.id = p.id;
    cb.addEventListener("change", () => { p.checked = cb.checked; refreshBar(); });
    const mark = el("span", "sec-mark");
    mark.innerHTML = p.estado === "aplicada" ? mic.icon("check") : p.estado === "fallida" ? mic.icon("x") : "";
    const kind = el("span", "sec-kind");
    kind.innerHTML = mic.icon(KIND_ICON[p.tipo] || "dots");
    const txt = el("span", "sec-pt");
    const [main, facts] = lines(p);
    const lit = S.flash.get(p.id) || [];
    const mainEl = el("span", "sec-pmain", main || "—");
    if (lit.some((k) => inMain(p, k))) mainEl.classList.add("lit");
    txt.append(mainEl);
    const sub = el("span", "sec-psub");
    const parts = (facts || []).filter((x) => x);
    if (parts.length) {
      const f = el("span", "sec-facts", parts.join(" · "));
      if (lit.some((k) => !inMain(p, k))) f.classList.add("lit");
      sub.append(f);
    }
    if (p.aviso && WARN[p.aviso] && p.estado === "pendiente") sub.append(el("span", "sec-warnf", mic.t(WARN[p.aviso])));
    const note = p.estado === "pendiente" && IT() ? IT().notice(p) : null;
    if (note) {
      const n = el("span", "sec-pnote", note);
      n.dataset.out = String((p.invita && p.invita.length > 0) || !!p.envia);
      sub.append(n);
    }
    if (p.estado === "fallida") sub.append(el("span", "sec-err", (IT() && IT().errorText(p.error)) || errText(p.error)));
    if (p.estado === "aplicada") {
      if (p.enlace) sub.append(linkBtn(p.enlace, mic.t("secretaria.open_odoo")));
      const res = IT() ? IT().result(p, call) : null;
      if (res) sub.append(res);
    }
    if (p.estado === "pendiente" && S.undoable.has(p.id) && p.previa) {
      const u = el("button", "sec-tlink sec-runddo", mic.t("secretaria.undo"));
      u.type = "button";
      u.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); undoRefine(p.id); });
      sub.append(u);
    }
    if (sub.childElementCount) txt.append(sub);
    const nt = S.notes.get(p.id);
    if (nt && p.estado === "pendiente") txt.append(el("span", "sec-pnotemsg", nt));
    lab.append(cb, mark, kind, txt);
    row.append(lab);
    const tools = el("div", "sec-ptools");
    const s = stamp(p.t);
    if (s) tools.append(s);
    if (p.estado === "fallida") {
      // an uncertain result (it may already be done) is never retried blindly: the user checks
      // Odoo/Google first and confirms explicitly
      const unsure = !!p.incierta;
      const retry = el("button", "btn btn-ghost btn-sm sec-retry", mic.t(unsure ? "secretaria.confirm_retry" : "secretaria.retry"));
      retry.type = "button";
      if (unsure) { retry.dataset.tip = mic.t("secretaria.err.resultado_incierto"); retry.dataset.tipPos = "left"; }
      retry.addEventListener("click", () => act("apply", [p.id], unsure ? { confirm: true } : undefined));
      tools.append(retry);
    }
    if (p.estado === "aplicada") {
      const u = el("button", "icon-btn sec-pundo");
      u.type = "button";
      u.innerHTML = mic.icon("restart");
      u.setAttribute("aria-label", mic.t("secretaria.undo_one"));
      u.dataset.tip = mic.t("secretaria.undo_one");
      u.dataset.tipPos = "left";
      u.addEventListener("click", () => act("undo", [p.id]));
      tools.append(u);
    }
    if (p.estado === "pendiente") {
      const ask = el("button", "icon-btn sec-pask");
      ask.type = "button";
      ask.innerHTML = mic.icon("spark");
      ask.setAttribute("aria-label", mic.t("secretaria.ask.open"));
      ask.setAttribute("aria-expanded", String(S.ask.has(p.id)));
      ask.dataset.tip = mic.t("secretaria.ask.open");
      ask.dataset.tipPos = "left";
      ask.addEventListener("click", () => (S.ask.has(p.id) ? closeAsk(p.id) : openAsk(p.id)));
      tools.append(ask);
    }
    const editable = p.estado === "pendiente" || (p.grupo === "google" && p.estado === "fallida");
    if (editable && (p.campos || []).length) {
      const pen = el("button", "icon-btn sec-pedit");
      pen.type = "button";
      pen.innerHTML = mic.icon("edit");
      pen.setAttribute("aria-label", mic.t("secretaria.edit"));
      pen.setAttribute("aria-expanded", String(S.open.has(p.id)));
      pen.dataset.tip = mic.t("secretaria.edit");
      pen.dataset.tipPos = "left";
      pen.addEventListener("click", () => {
        if (S.open.has(p.id)) S.open.delete(p.id); else S.open.add(p.id);
        swap(p.id);
        const first = document.querySelector(`.sec-edit[data-id="${p.id}"] input, .sec-edit[data-id="${p.id}"] select, .sec-edit[data-id="${p.id}"] textarea`);
        if (first) first.focus();
      });
      tools.append(pen);
    }
    row.append(tools);
    li.append(row);
    if (editable && S.open.has(p.id) && (p.campos || []).length) { li.classList.add("editing"); li.append(editor(p)); }
    if (S.ask.has(p.id) && p.estado === "pendiente") { li.classList.add("asking"); li.append(askBox(p)); }
    return li;
  }

  /** Re-render a single card (keeps focus, scroll and every other card untouched). */
  function swap(id, opt) {
    opt = opt || {};
    const old = document.querySelector(`.sec-p[data-id="${id}"]`);
    const p = findP(id);
    if (!old || !p) { render(); return; }
    const focused = document.activeElement && old.contains(document.activeElement) ? document.activeElement : null;
    const caret = focused && focused.dataset && focused.dataset.ask ? focused.selectionStart : null;
    const n = renderItem(p);
    if (opt.keepThinking || (S.ask.get(id) || {}).busy) n.classList.add("thinking");
    if (opt.morph) n.classList.add("morph-in");
    if (opt.saved) n.classList.add("saved");
    old.replaceWith(n);
    if (focused && focused.dataset && focused.dataset.ask) {
      const inp = n.querySelector(".sec-aski");
      if (inp && !inp.disabled) { inp.focus(); if (caret != null) inp.setSelectionRange(caret, caret); }
    }
    if (opt.morph) setTimeout(() => n.classList.remove("morph-in"), 700);
    refreshBar();
  }

  function errText(key) {
    return key ? mic.t("secretaria.err." + key) : mic.t("secretaria.failed");
  }

  function linkBtn(url, label) {
    const a = el("button", "it-link", label);
    a.type = "button";
    a.insertAdjacentHTML("beforeend", mic.icon("external", "mini"));
    a.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); call("open_link", { url }); });
    return a;
  }

  const GROUPS = [
    ["google", "secretaria.group.google"],
    ["horas", "secretaria.group.horas"],
    ["agenda", "secretaria.group.agenda"],
    ["crm", "secretaria.group.crm"],
    ["correcciones", "secretaria.group.fix"],
  ];

  function renderProposals(a) {
    const props = (a.propuestas || []).filter((p) => p.estado !== "descartada");
    const box = el("section", "sec-props");
    box.tabIndex = -1;
    box.setAttribute("aria-label", mic.t("secretaria.proposals"));
    box.append(renderBar());   // sticky at the top: the list scrolls under it
    const odooState = a.odoo && a.odoo.estado;
    if (odooState && odooState !== "conectado" && odooState !== "sin_configurar") {
      box.append(el("p", "sec-warn", mic.t("secretaria.odoo." + odooState)));
    }
    if (!props.length) {
      const empty = el("p", "sec-none");
      empty.innerHTML = mic.icon("check");
      empty.append(a.odoo && a.odoo.estado === "conectado" ? mic.t("secretaria.none_odoo") : mic.t("secretaria.none"));
      box.append(empty);
    }
    for (const [g, key] of GROUPS) {
      const items = props.filter((p) => p.grupo === g);
      if (!items.length) continue;
      box.append(el("h4", "caps sec-gh", mic.t(key)));
      const ul = el("ul", "sec-plist");
      let lastDay = null;
      let lastT = null;
      for (const p of items) {
        if (g === "horas" && p.datos && p.datos.fecha !== lastDay) {
          lastDay = p.datos.fecha;
          const total = items.filter((x) => x.datos && x.datos.fecha === lastDay).reduce((acc, x) => acc + (+x.datos.horas || 0), 0);
          const dh = el("li", "sec-day");
          dh.append(el("span", "sec-dayname", fmtDay(lastDay)), el("span", "sec-daytot tnum", hfmt(total) + " h"));
          ul.append(dh);
        }
        const li = renderItem(p);
        if (typeof p.t === "number" && p.t === lastT) li.querySelector(".sec-t")?.classList.add("same");   // one stamp per moment
        lastT = typeof p.t === "number" ? p.t : null;
        ul.append(li);
      }
      box.append(ul);
    }
    return box;
  }

  // ------------------------------------------------------------------ the bar: message · undo · apply
  // While the bar is pinned its capsule also covers the scroller's top fade band (`.stuck`), so
  // no half-faded row peeks out between the header and the bar.
  function syncStuck() {
    const bar = document.querySelector(".sec-bar");
    const tr = bar && bar.closest(".transcript");
    if (!tr) return;
    const stuck = tr.scrollTop > 0 && bar.getBoundingClientRect().top <= tr.getBoundingClientRect().top + 0.5;
    bar.classList.toggle("stuck", stuck);
  }
  document.addEventListener("scroll", syncStuck, { capture: true, passive: true });

  function renderBar() {
    const bar = el("div", "sec-bar");
    bar.append(el("span", "sec-badge caps", mic.t("secretaria.proposals")));
    const field = el("div", "sec-askf sec-planf");
    const glyph = el("span", "sec-askg");
    glyph.innerHTML = mic.icon("spark");
    const inp = el("input", "sec-aski sec-plani");
    inp.type = "text";
    inp.spellcheck = false;
    inp.autocomplete = "off";
    inp.maxLength = 400;
    inp.placeholder = mic.t("secretaria.ask.plan");
    inp.setAttribute("aria-label", mic.t("secretaria.ask.plan_label"));
    inp.value = S.plan.text;
    inp.disabled = S.plan.busy;
    inp.dataset.ask = "plan";
    inp.addEventListener("input", () => { S.plan.text = inp.value; S.plan.error = null; bar.querySelector(".sec-planerr")?.remove(); });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); refinePlan(); }
      else if (e.key === "Escape") { inp.value = ""; S.plan.text = ""; inp.blur(); }
    });
    const status = el("span", "sec-planst");
    status.setAttribute("role", "status");
    if (S.plan.busy) status.append(el("span", "sec-spin"), el("span", "sec-planst-t", statusText(S.plan.status)));
    field.append(glyph, inp, status);
    const undo = el("button", "btn btn-ghost btn-sm sec-undo", mic.t("secretaria.undo"));
    undo.type = "button";
    undo.addEventListener("click", undoLast);
    const apply = el("button", "btn btn-primary btn-sm sec-apply");
    apply.type = "button";
    apply.setAttribute("aria-keyshortcuts", "Enter");
    apply.append(el("span", "", mic.t("secretaria.apply")), el("span", "sec-n tnum"), el("kbd", "", "↵"));
    apply.addEventListener("click", () => act("apply", checkedIds()));
    bar.append(field, undo, apply);
    if (S.plan.error) bar.append(el("p", "sec-planerr", S.plan.error));
    else if (S.plan.note) bar.append(el("p", "sec-planerr quiet", S.plan.note));
    return bar;
  }

  function checkedIds() {
    return [...document.querySelectorAll(".sec-cb")].filter((c) => !c.disabled && c.checked).map((c) => c.dataset.id);
  }

  function refreshBar() {
    const bar = document.querySelector(".sec-bar");
    if (!bar) return;
    const props = ((S.acta && S.acta.propuestas) || []).filter((p) => p.estado !== "descartada");
    const pending = props.filter((p) => p.estado === "pendiente");
    const n = checkedIds().length;
    const apply = bar.querySelector(".sec-apply");
    const count = apply.querySelector(".sec-n");
    count.textContent = n ? String(n) : "";
    apply.disabled = n === 0 || S.busy || S.plan.busy;
    apply.hidden = pending.length === 0;
    const undo = bar.querySelector(".sec-undo");
    const applied = props.some((p) => p.estado === "aplicada");
    undo.hidden = !((S.last === "plan" && S.acta.plan_previo) || (S.last === "apply" && applied));
    bar.querySelector(".sec-planf").hidden = pending.length === 0 && !S.plan.busy;
    bar.classList.toggle("bare", apply.hidden && undo.hidden && bar.querySelector(".sec-planf").hidden);
  }

  async function refinePlan() {
    if (S.plan.busy || !S.plan.text.trim()) return;
    S.plan.busy = true;
    S.plan.error = null;
    S.plan.note = null;
    S.plan.status = { fase: "pensando" };
    const list = document.querySelector(".sec-props");
    if (list) list.classList.add("thinking");
    replaceBar();
    const folder = S.folder;
    const stop = pollStatus((s) => {
      if (!s || s.id) return;
      S.plan.status = s;
      const t = document.querySelector(".sec-planst-t");
      if (t) crossText(t, statusText(s));
    });
    const r = await call("refine_plan", { folder, texto: S.plan.text });
    stop();
    if (stale(r, folder)) return;
    S.plan.busy = false;
    if (!r || !r.ok) {
      S.plan.error = errorLine(r);
      if (list) list.classList.remove("thinking");
      replaceBar();
      const inp = document.querySelector(".sec-plani");
      if (inp) inp.focus();
      return;
    }
    S.acta = r.acta;
    S.plan.text = "";
    S.plan.note = r.nota && r.nota !== "rejected" ? r.nota : r.nota === "rejected" ? mic.t("secretaria.ask.partial") : null;
    S.last = "plan";
    for (const id of r.cambiados || []) {
      const p = findP(id);
      S.flash.set(id, (p && p.cambiados) || []);
      S.undoable.add(id);
    }
    if (list && mic.motion) {
      for (const id of r.quitados || []) {
        const n = document.querySelector(`.sec-p[data-id="${id}"]`);
        if (n) n.classList.add("leaving");
      }
      await wait((r.quitados || []).length ? 220 : 0);
    }
    render();
    for (const id of r.anadidos || []) {
      const n = document.querySelector(`.sec-p[data-id="${id}"]`);
      if (n) n.classList.add("morph-in");
    }
    setTimeout(() => { S.flash.clear(); document.querySelectorAll(".sec-props .lit").forEach((x) => x.classList.remove("lit")); }, 2400);
    const inp = document.querySelector(".sec-plani");
    if (inp) inp.focus();
  }

  function replaceBar() {
    const old = document.querySelector(".sec-bar");
    if (!old) return;
    old.replaceWith(renderBar());
    refreshBar();
  }

  async function undoLast() {
    if (S.last === "plan") {
      const folder = S.folder;
      const r = await call("refine_undo", { folder });
      if (stale(r, folder)) return;
      if (!r || !r.ok) { snack(mic.t((r && r.error) || "library.err.op_failed")); return; }
      S.acta = r.acta;
      S.last = null;
      S.plan.note = null;
      S.undoable.clear();
      render();
      snack(mic.t("secretaria.undone"));
      return;
    }
    const props = ((S.acta && S.acta.propuestas) || []).filter((p) => p.estado === "aplicada");
    act("undo", props.map((p) => p.id));
  }

  /** «Aplicar» approves the exact version on screen (id + huella); Python refuses any other.
   *  opt.confirm: the user checked an uncertain result by hand and wants it run again. */
  async function act(action, ids, opt) {
    if (!ids.length || S.busy) return;
    const folder = S.folder;
    S.busy = true;
    const box = document.querySelector(".sec-props");
    if (box) box.dataset.busy = "true";
    refreshBar();
    const args = action === "apply"
      ? {
          folder,
          items: ids.map((id) => {
            const p = findP(id) || {};
            return { id, huella: p.huella, ...(opt && opt.confirm ? { confirmar: true } : {}) };
          }),
        }
      : { folder, ids };
    const r = await call(action, args);
    if (stale(r, folder)) return;
    S.busy = false;
    if (!r || !r.ok) {
      snack(mic.t((r && r.error) || "library.err.op_failed"));
      if (r && r.error === "secretaria.err.stale") load(); else render();  // show what is there now
      return;
    }
    S.acta = r.acta;
    S.last = action === "apply" ? "apply" : null;
    const fixes = (r.acta.propuestas || []).some((p) => ids.includes(p.id) && p.grupo === "correcciones");
    render();
    if (fixes && lib()) lib().reload();
    const failed = (r.acta.propuestas || []).filter((p) => ids.includes(p.id) && p.estado === "fallida").length;
    if (action !== "apply") snack(mic.t("secretaria.undone"));
    else snack(failed ? mic.t("secretaria.applied_some", { n: failed }) : mic.t("secretaria.applied"));
  }

  function snack(text) {
    const s = $("snack");
    if (!s) return;
    s.textContent = text;
    s.classList.add("show");
    clearTimeout(snack.t);
    snack.t = setTimeout(() => s.classList.remove("show"), 3200);
  }

  async function analyze() {
    const folder = S.folder;
    const r = await call("analyze", { folder });
    if (stale(r, folder)) return;
    if (!r || !r.ok) { snack(mic.t((r && r.error) || "library.err.op_failed")); return; }
    S.run = { state: "pending" };
    S.acta = null;
    S.trail = [];
    render();
    schedule();
  }

  // ------------------------------------------------------------------ live research line
  function liveChip(status) {
    const chip = el("div", "sec-chip sec-live");
    chip.setAttribute("role", "status");
    chip.append(el("span", "sec-spin"), el("span", "sec-live-t", statusText(status)));
    const src = sourceOf(status);
    if (src && !S.trail.includes(src)) S.trail.push(src);
    if (S.trail.length) {
      const trail = el("span", "sec-trail");
      for (const s of S.trail) trail.append(el("span", "sec-trail-i", mic.t("items.src." + s)));
      chip.append(trail);
    }
    return chip;
  }

  // ------------------------------------------------------------------ render
  function render(status) {
    const host = $("sec");
    if (!host) return;
    const active = document.activeElement;
    const focusAsk = active && active.dataset ? active.dataset.ask : null;
    const caret = focusAsk ? active.selectionStart : null;
    host.replaceChildren();
    const working = S.run && (S.run.state === "pending" || S.run.state === "running");
    if (S.acta && S.full && FA()) {
      host.append(FA().view(S.folder, call, () => { S.full = false; render(); }), renderProposals(S.acta));
    } else if (S.acta) host.append(renderActa(S.acta), renderProposals(S.acta));
    if (working) {
      host.prepend(liveChip(status));
    } else if (!S.acta && S.run && S.run.state === "failed") {
      const row = el("div", "sec-chip err");
      row.append(el("span", "", mic.t("secretaria.err." + (S.run.error || "failed"))));
      const b = el("button", "btn btn-sm", mic.t("secretaria.retry"));
      b.type = "button";
      b.addEventListener("click", analyze);
      row.append(b);
      host.append(row);
    } else if (!S.acta && S.enabled) {
      const row = el("div", "sec-chip idle");
      const b = el("button", "btn btn-sm", mic.t("secretaria.analyze"));
      b.type = "button";
      b.addEventListener("click", analyze);
      row.append(el("span", "", mic.t("secretaria.no_acta")), b);
      host.append(row);
    }
    host.hidden = !host.childElementCount;
    refreshBar();
    if (focusAsk) {
      const inp = document.querySelector(`.sec-aski[data-ask="${focusAsk}"]`);
      if (inp && !inp.disabled) { inp.focus(); if (caret != null) inp.setSelectionRange(caret, caret); }
    }
  }

  function schedule() {
    clearTimeout(S.poll);
    S.poll = setTimeout(load, 1000);
  }

  async function load() {
    const folder = S.folder;
    if (!folder) return;
    const r = await call("acta", { folder });
    if (folder !== S.folder) return;
    if (!r || !r.ok) {
      if (r && r.moved && lib()) { lib().refreshList(); lib().open(r.moved); return; }  // retitled
      render();
      return;
    }
    S.enabled = !!r.enabled;
    S.run = r.run;
    const wasWorking = !S.acta && r.acta;
    S.acta = r.acta;
    render(r.status);
    const working = S.run && (S.run.state === "pending" || S.run.state === "running");
    if (working) schedule();
    else if (wasWorking && lib()) lib().refreshList();
  }

  document.addEventListener("mic:meeting", (e) => {
    const folder = e.detail && e.detail.folder;
    if (folder === S.folder) return;
    if (FA() && S.folder) FA().forget(S.folder);
    S.folder = folder;
    S.acta = null;
    S.open = new Set();
    S.ask = new Map();
    S.undoable = new Set();
    S.flash = new Map();
    S.notes = new Map();
    S.plan = { text: "", busy: false, error: null, note: null };
    S.last = null;
    S.trail = [];
    S.full = false;
    S.run = null;
    S.busy = false;           // a pending «Aplicar» of the previous meeting no longer counts
    clearTimeout(S.poll);
    const host = $("sec");
    if (host) { host.replaceChildren(); host.hidden = true; }
    load();
  });

  // Enter applies the checked proposals (unless typing or on another control).
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.ctrlKey || e.altKey || e.metaKey) return;
    const t = e.target;
    const tag = t.tagName;
    if (tag === "INPUT" && t.type !== "checkbox") return;
    if (tag === "TEXTAREA" || tag === "BUTTON" || tag === "SELECT" || t.isContentEditable) return;
    if (t.closest && t.closest(".ph, dialog, .menu")) return;
    const b = document.querySelector(".sec-apply");
    if (b && !b.disabled && !b.hidden) { e.preventDefault(); b.click(); }
  });

  // test/screenshot hooks (also handy from the console)
  window.micnaga = window.micnaga || {};
  window.micnaga.secretaria = {
    ask: (id, text) => { openAsk(id); const st = S.ask.get(id); if (st) { st.text = text; swap(id); } },
    send: (id) => refine(id),
    plan: (text) => { S.plan.text = text; replaceBar(); },
    sendPlan: () => refinePlan(),
    state: () => S,
  };
})();
