/* Secretaria in the library viewer: the acta above the transcript, then the proposals list.
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
  const S = { folder: null, acta: null, run: null, enabled: false, poll: 0, busy: false, applied: [], open: new Set() };
  const call = (action, args) => mic.call("secretaria", action, args || {});
  const lib = () => window.micnaga && window.micnaga.library;
  const IT = () => window.micnaga && window.micnaga.items;   // Google items (items.js)

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
    const d = new Date(iso + "T12:00:00");
    return isNaN(d) ? iso : d.toLocaleDateString("es-ES", { weekday: "short", day: "numeric", month: "short" });
  }

  // ------------------------------------------------------------------ status chip
  const TOOL_KEY = {
    buscar_contactos: "secretaria.st.contacts",
    buscar_empresas: "secretaria.st.companies",
    buscar_oportunidades: "secretaria.st.leads",
    leer_oportunidad: "secretaria.st.lead",
    listar_etapas: "secretaria.st.stages",
    historial_reciente: "secretaria.st.history",
    actividades_pendientes: "secretaria.st.activities",
    buscar_en_reuniones: "secretaria.st.meetings",
    mis_partes_de_horas: "secretaria.st.hours",
    proyectos: "secretaria.st.projects",
    tareas: "secretaria.st.tasks",
    calendario: "secretaria.st.calendar",
    mis_actividades: "secretaria.st.my_activities",
    mis_gastos: "secretaria.st.expenses",
  };
  function statusText(st) {
    if (!st) return mic.t("secretaria.st.reading");
    if (st.fase === "herramienta") {
      const key = TOOL_KEY[st.tool];
      if (key) return mic.t(key, { arg: st.arg || "" }).replace("  ", " ");
      return mic.t("secretaria.st.other", { server: st.servidor || "" });
    }
    if (st.fase === "comprobando") return mic.t("secretaria.st.checking");
    if (st.fase === "en_cola") return mic.t("secretaria.st.queued");
    return mic.t("secretaria.st.reading");
  }

  // ------------------------------------------------------------------ acta
  function section(title, items, render) {
    if (!items || !items.length) return null;
    const box = el("div", "sec-block");
    box.append(el("h4", "caps", title));
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
    redo.setAttribute("aria-label", mic.t("secretaria.redo"));
    redo.addEventListener("click", analyze);
    head.append(redo);
    box.append(head);
    if (a.resumen && a.resumen.length) box.append(el("p", "sec-sum", a.resumen.join(" ")));
    const blocks = [
      section(mic.t("secretaria.decisions"), a.decisiones, (it) => line(it, "·")),
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
      }),
      section(mic.t("secretaria.open"), a.preguntas_abiertas, (it) => line(it, "?")),
      section(mic.t("secretaria.next"), a.proximos_pasos, (it) => line(it, "→")),
    ].filter(Boolean);
    if (IT()) blocks.push(...IT().actaExtras(a, call, stamp));
    if (blocks.length) {
      const grid = el("div", "sec-grid");
      blocks.forEach((b) => grid.append(b));
      box.append(grid);
    }
    return box;
  }

  // ------------------------------------------------------------------ proposals
  function money(n) { return typeof n === "number" ? n.toLocaleString("es-ES", { maximumFractionDigits: 0 }) + " €" : "—"; }
  function proposalText(p) {
    const d = p.datos || {};
    switch (p.tipo) {
      case "hablante": return mic.t("secretaria.p.speaker", { de: d.de, a: d.a });
      case "palabra": return mic.t("secretaria.p.word", { de: d.de, a: d.a, n: d.n || 1 });
      case "vincular_a_oportunidad": return mic.t("secretaria.p.link", { name: d.nombre || "#" + d.id });
      case "nota_en_historial":
        return d.modelo === "project.task" || d.modelo === "project.project"
          ? mic.t("secretaria.p.note_on", { name: d.nombre || "#" + d.id })
          : mic.t("secretaria.p.note", { name: d.nombre || "#" + d.id });
      case "crear_tarea":
        return mic.t("secretaria.p.task", { what: d.nombre, proj: d.proyecto || "", d: d.fecha ? " · " + fmtDay(d.fecha) : "" });
      case "crear_evento":
        return mic.t("secretaria.p.event", { what: d.nombre, d: fmtDay(d.fecha) + (d.hora_inicio ? " " + d.hora_inicio : "") });
      case "crear_gasto": return mic.t("secretaria.p.expense", { what: d.nombre, amount: money(d.importe) });
      case "registrar_horas":
        return mic.t("secretaria.p.hours", { h: (Math.round(d.horas * 100) / 100).toLocaleString("es-ES"), proj: d.proyecto || "", desc: d.descripcion || "" });
      case "crear_contacto": {
        const bits = [d.nombre, d.cargo, d.empresa].filter(Boolean).join(" · ");
        return mic.t("secretaria.p.contact", { who: bits });
      }
      case "actualizar_oportunidad": {
        const prev = d.anterior || {};
        const parts = [];
        if (d.importe != null) parts.push(money(prev.importe) + " → " + money(d.importe));
        if (d.etapa_id != null) parts.push((prev.etapa || "—") + " → " + (d.etapa || "#" + d.etapa_id));
        if (d.fecha_cierre) parts.push(mic.t("secretaria.p.close", { d: fmtDay(d.fecha_cierre) }));
        return mic.t("secretaria.p.update", { what: parts.join(" · ") });
      }
      case "programar_actividad": return mic.t("secretaria.p.activity", { what: d.resumen, d: fmtDay(d.fecha) });
      default: return (IT() && IT().text(p)) || p.titulo || p.tipo;
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

  // ------------------------------------------------------------------ item editor
  // Every item is data-driven: Python sends `campos` (key, type, options list, current value)
  // and re-validates each change with the same rules as the original proposal; the item comes
  // back replaced with the same id. One editor for every kind of item.
  const hfmt = (h) => (Math.round(h * 100) / 100).toLocaleString("es-ES", { maximumFractionDigits: 2 });

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
    row.append(el("span", "sec-inv-n", mic.t("secretaria.invite_note")));
    return row;
  }

  function editor(p) {
    const box = el("div", "sec-edit");
    box.dataset.id = p.id;
    for (const f of p.campos || []) {
      if (f.tipo === "chips" && !(f.valor && f.valor.length)) continue;
      // rows of buttons go in a div: a <label> would forward a click on its text to the first one
      const lab = el(["chips", "people", "choice"].includes(f.tipo) ? "div" : "label", "sec-f sec-f-" + f.tipo + " k-" + f.clave);
      lab.append(el("span", "sec-fl", mic.t(f.etiqueta || "secretaria.f." + f.clave)));
      lab.append(f.tipo === "chips" ? chips(p, f) : control(p, f));
      box.append(lab);
    }
    return box;
  }

  async function edit(p, changes) {
    const r = await call("edit", { folder: S.folder, id: p.id, cambios: changes });
    if (!r || !r.ok) { snack(mic.t((r && r.error) || "library.err.op_failed")); render(); return; }
    S.acta = r.acta;
    render();
    const box = document.querySelector(`.sec-edit[data-id="${p.id}"]`);
    if (box) box.closest(".sec-p").classList.add("saved");
  }

  function renderProposals(a) {
    const props = (a.propuestas || []).filter((p) => p.estado !== "descartada");
    if (!props.length) {
      const empty = el("p", "sec-none");
      empty.innerHTML = mic.icon("check");
      empty.append(a.odoo && a.odoo.estado === "conectado" ? mic.t("secretaria.none_odoo") : mic.t("secretaria.none"));
      return empty;
    }
    const box = el("section", "sec-props");
    box.tabIndex = -1;
    const head = el("div", "sec-head");
    head.append(el("span", "sec-badge caps", mic.t("secretaria.proposals")));
    const pending = props.filter((p) => p.estado === "pendiente");
    const count = el("span", "sec-count tnum");
    head.append(count);
    const undo = el("button", "btn btn-ghost btn-sm sec-undo", mic.t("secretaria.undo"));
    undo.type = "button";
    undo.hidden = !props.some((p) => p.estado === "aplicada");
    undo.addEventListener("click", () => act("undo", props.filter((p) => p.estado === "aplicada").map((p) => p.id)));
    const apply = el("button", "btn btn-primary btn-sm sec-apply");
    apply.type = "button";
    apply.append(mic.t("secretaria.apply"));
    apply.append(el("kbd", "", "↵"));
    apply.setAttribute("aria-keyshortcuts", "Enter");
    head.append(undo, apply);
    box.append(head);
    const odooState = a.odoo && a.odoo.estado;
    if (odooState && odooState !== "conectado" && odooState !== "sin_configurar") {
      box.append(el("p", "sec-warn", mic.t("secretaria.odoo." + odooState)));
    }
    const groups = [
      ["google", "secretaria.group.google"],
      ["horas", "secretaria.group.horas"],
      ["agenda", "secretaria.group.agenda"],
      ["crm", "secretaria.group.crm"],
      ["correcciones", "secretaria.group.fix"],
    ];
    const checks = [];
    for (const [g, key] of groups) {
      const items = props.filter((p) => p.grupo === g);
      if (!items.length) continue;
      box.append(el("h4", "caps sec-gh", mic.t(key)));
      const ul = el("ul", "sec-plist");
      let lastDay = null;
      for (const p of items) {
        if (g === "horas" && p.datos && p.datos.fecha !== lastDay) {
          lastDay = p.datos.fecha;
          const total = items.filter((x) => x.datos && x.datos.fecha === lastDay).reduce((a, x) => a + (+x.datos.horas || 0), 0);
          const dh = el("li", "sec-day");
          dh.append(el("span", "sec-dayname", fmtDay(lastDay)), el("span", "sec-daytot tnum", hfmt(total) + " h"));
          ul.append(dh);
        }
        const li = el("li", "sec-p");
        li.dataset.state = p.estado;
        const lab = el("label", "sec-pl");
        const cb = el("input");
        cb.type = "checkbox";
        cb.checked = p.estado === "pendiente" ? !!p.checked : p.estado === "aplicada";
        cb.disabled = p.estado !== "pendiente";
        cb.dataset.id = p.id;
        checks.push(cb);
        const mark = el("span", "sec-mark");
        mark.innerHTML = p.estado === "aplicada" ? mic.icon("check") : p.estado === "fallida" ? mic.icon("x") : "";
        const txt = el("span", "sec-pt");
        txt.append(el("span", "sec-pmain", proposalText(p)));
        const sub = [];
        if (p.motivo && g !== "horas") sub.push(p.motivo);
        if (p.aviso && WARN[p.aviso]) sub.push(mic.t(WARN[p.aviso]));
        if (p.estado === "fallida") sub.push((IT() && IT().errorText(p.error)) || mic.t("secretaria.failed"));
        if (sub.length) txt.append(el("span", "sec-psub", sub.join(" · ")));
        const note = p.estado === "pendiente" && IT() ? IT().notice(p) : null;
        if (note) {
          const n = el("span", "sec-pnote", note);
          n.dataset.out = String((p.invita && p.invita.length > 0) || !!p.envia);
          txt.append(n);
        }
        const res = IT() ? IT().result(p, call) : null;
        if (res) txt.append(res);
        lab.append(cb, mark, txt);
        li.append(lab);
        const s = stamp(p.t);
        if (s) li.append(s);
        if (p.estado === "aplicada") {
          const u = el("button", "icon-btn sec-pundo");
          u.type = "button";
          u.innerHTML = mic.icon("restart");
          u.setAttribute("aria-label", mic.t("secretaria.undo_one"));
          u.dataset.tip = mic.t("secretaria.undo_one");
          u.addEventListener("click", () => act("undo", [p.id]));
          li.append(u);
        }
        const editable = p.estado === "pendiente" || (p.grupo === "google" && p.estado === "fallida");
        if (editable && (p.campos || []).length) {
          const pen = el("button", "icon-btn sec-pedit");
          pen.type = "button";
          pen.innerHTML = mic.icon("edit");
          pen.setAttribute("aria-label", mic.t("secretaria.edit"));
          pen.setAttribute("aria-expanded", String(S.open.has(p.id)));
          pen.addEventListener("click", () => {
            if (S.open.has(p.id)) S.open.delete(p.id); else S.open.add(p.id);
            render();
            const first = document.querySelector(`.sec-edit[data-id="${p.id}"] input, .sec-edit[data-id="${p.id}"] select, .sec-edit[data-id="${p.id}"] textarea`);
            if (first) first.focus();
          });
          li.append(pen);
          if (S.open.has(p.id)) { li.classList.add("editing"); li.append(editor(p)); }
        }
        ul.append(li);
      }
      box.append(ul);
    }
    const refresh = () => {
      const n = checks.filter((c) => !c.disabled && c.checked).length;
      count.textContent = mic.t("secretaria.count", { n, total: pending.length });
      apply.disabled = n === 0 || S.busy;
      apply.hidden = pending.length === 0;
      count.hidden = pending.length === 0;
    };
    checks.forEach((c) => c.addEventListener("change", refresh));
    refresh();
    apply.addEventListener("click", () => act("apply", checks.filter((c) => !c.disabled && c.checked).map((c) => c.dataset.id)));
    return box;
  }

  async function act(action, ids) {
    if (!ids.length || S.busy) return;
    S.busy = true;
    const box = document.querySelector(".sec-props");
    if (box) box.dataset.busy = "true";
    const r = await call(action, { folder: S.folder, ids });
    S.busy = false;
    if (!r || !r.ok) { snack(mic.t((r && r.error) || "library.err.op_failed")); render(); return; }
    S.acta = r.acta;
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
    const r = await call("analyze", { folder: S.folder });
    if (!r || !r.ok) { snack(mic.t((r && r.error) || "library.err.op_failed")); return; }
    S.run = { state: "pending" };
    S.acta = null;
    render();
    schedule();
  }

  // ------------------------------------------------------------------ render
  function render(status) {
    const host = $("sec");
    if (!host) return;
    host.replaceChildren();
    const working = S.run && (S.run.state === "pending" || S.run.state === "running");
    if (S.acta) {
      host.append(renderActa(S.acta), renderProposals(S.acta));
    }
    if (working) {
      const chip = el("div", "sec-chip");
      chip.setAttribute("role", "status");
      chip.append(el("span", "sec-spin"), el("span", "", statusText(status)));
      host.prepend(chip);
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
    S.folder = folder;
    S.acta = null;
    S.open = new Set();
    S.run = null;
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
})();
