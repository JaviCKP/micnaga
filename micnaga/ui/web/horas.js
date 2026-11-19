/* «Partes de horas»: the timesheet review panel inside the library (tray «Rellenar partes de
   horas», the Friday nudge, or the clock button in the sidebar). Python plans the days
   (pattern + recorded meetings + calendar) and validates every line again before writing. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const call = (action, args) => mic.call("secretaria", action, args || {});
  const S = { plan: null, open: false, busy: false, batch: null, collapsed: new Set(), results: {} };

  const hfmt = (h) => (Math.round(h * 100) / 100).toLocaleString("es-ES", { maximumFractionDigits: 2 });
  function dayLabel(iso) {
    const d = new Date(iso + "T12:00:00");
    return d.toLocaleDateString("es-ES", { weekday: "long", day: "numeric", month: "short" });
  }

  // ------------------------------------------------------------------ open / close
  function show(on) {
    S.open = on;
    const host = $("horas");
    if (!host) return;
    host.hidden = !on;
    const btn = $("horas-btn");
    if (btn) btn.setAttribute("aria-pressed", String(on));
    if (on) {
      $("empty").hidden = true;
      $("viewer").hidden = true;
    } else {
      const hasMeeting = !!($("m-title") && $("m-title").textContent);
      $("viewer").hidden = !hasMeeting;
      $("empty").hidden = hasMeeting;
    }
  }

  async function open() {
    show(true);
    S.results = {};
    S.batch = null;
    renderLoading();
    const r = await call("horas_plan");
    if (!S.open) return;
    if (!r || !r.ok) { renderError(mic.t((r && r.error) || "library.err.op_failed")); return; }
    S.plan = r;
    // first days open, the rest folded; a day with pre-checked lines is never folded
    S.collapsed = new Set(r.dias.slice(3).filter((d) => !d.lineas.some((l) => l.checked)).map((d) => d.fecha));
    render();
    const first = document.querySelector(".hx-apply");
    if (first) first.focus({ preventScroll: true });
  }

  // ------------------------------------------------------------------ render
  function head(sub) {
    const h = el("header", "hx-head");
    const titles = el("div", "hx-titles");
    const h2 = el("h2", "display", mic.t("secretaria.horas.title"));
    h2.id = "hx-title";
    titles.append(h2);
    if (sub) titles.append(el("p", "hx-sub", sub));
    h.append(titles);
    const actions = el("div", "hx-actions");
    h.append(actions);
    const close = el("button", "icon-btn hx-close");
    close.type = "button";
    close.innerHTML = mic.icon("x");
    close.setAttribute("aria-label", mic.t("secretaria.horas.close"));
    close.addEventListener("click", () => show(false));
    h.append(close);
    return { h, actions };
  }

  function renderLoading() {
    const host = $("horas");
    host.replaceChildren(head().h);
    const chip = el("div", "sec-chip hx-loading");
    chip.setAttribute("role", "status");
    chip.append(el("span", "sec-spin"), el("span", "", mic.t("secretaria.horas.loading")));
    host.append(chip);
  }

  function renderError(text) {
    const host = $("horas");
    host.replaceChildren(head().h, el("p", "hx-empty", text));
  }

  function checkedLines() {
    const out = [];
    for (const d of S.plan.dias) for (const ln of d.lineas) if (ln.checked && !ln.hecha) out.push(ln);
    return out;
  }

  function render() {
    const host = $("horas");
    const p = S.plan;
    const n = p.dias.length;
    const sub = n === 0 ? "" : n === 1 ? mic.t("secretaria.horas.sub_one") : mic.t("secretaria.horas.sub", { n });
    const { h, actions } = head(sub);
    const undo = el("button", "btn btn-ghost btn-sm hx-undo", mic.t("secretaria.horas.undo"));
    undo.type = "button";
    undo.hidden = !S.batch;
    undo.addEventListener("click", doUndo);
    const apply = el("button", "btn btn-primary btn-sm hx-apply");
    apply.type = "button";
    apply.setAttribute("aria-keyshortcuts", "Enter");
    const count = el("span", "hx-count tnum");
    apply.append(mic.t("secretaria.horas.apply"), count, el("kbd", "", "↵"));
    apply.addEventListener("click", doApply);
    actions.append(undo, apply);
    host.replaceChildren(h);

    if (!n) {
      const ok = el("p", "hx-empty");
      ok.innerHTML = mic.icon("check");
      ok.append(mic.t("secretaria.horas.none"));
      host.append(ok);
      apply.hidden = true;
      return;
    }
    if (p.patron) host.append(el("p", "hx-pattern", p.patron.replace(/^[^:]+:\s*/, "")));
    if (p.posibles_libres && p.posibles_libres.length) {
      host.append(el("p", "hx-note", mic.t("secretaria.horas.maybe_off", { days: p.posibles_libres.map((x) => dayLabel(x)).join(", ") })));
    }
    const list = el("div", "hx-days");
    list.setAttribute("role", "list");
    p.dias.forEach((d) => list.append(renderDay(d)));
    host.append(list, el("p", "hx-keys", mic.t("secretaria.horas.keys")));
    refreshTotals();
  }

  function projectOptions(sel, current) {
    const seen = new Set();
    for (const pr of S.plan.proyectos) {
      const o = el("option", "", pr.nombre);
      o.value = String(pr.id);
      sel.append(o);
      seen.add(pr.id);
    }
    if (current && !seen.has(current.id)) {
      const o = el("option", "", current.nombre || "#" + current.id);
      o.value = String(current.id);
      sel.append(o);
    }
  }

  function renderDay(d) {
    const box = el("section", "hx-day");
    box.dataset.fecha = d.fecha;
    box.setAttribute("role", "listitem");
    const folded = S.collapsed.has(d.fecha);
    box.dataset.folded = String(folded);
    const top = el("div", "hx-dh");
    const all = el("input", "hx-dcheck");
    all.type = "checkbox";
    all.setAttribute("aria-label", mic.t("secretaria.horas.check_day") + " " + dayLabel(d.fecha));
    all.addEventListener("change", () => {
      d.lineas.forEach((ln) => { if (!ln.hecha) ln.checked = all.checked; });
      box.querySelectorAll(".hx-lcheck").forEach((c) => { if (!c.disabled) c.checked = all.checked; });
      refreshTotals();
    });
    const toggle = el("button", "hx-dtoggle");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", String(!folded));
    toggle.innerHTML = mic.icon("chevron");
    const name = el("span", "hx-dname", dayLabel(d.fecha));
    const state = el("span", "hx-state " + d.estado,
      d.estado === "incompleto" ? mic.t("secretaria.horas.partial", { h: hfmt(d.horas) }) : mic.t("secretaria.horas.missing"));
    toggle.append(name, state);
    toggle.addEventListener("click", () => {
      const f = box.dataset.folded !== "true";
      box.dataset.folded = String(f);
      toggle.setAttribute("aria-expanded", String(!f));
      if (f) S.collapsed.add(d.fecha); else S.collapsed.delete(d.fecha);
    });
    toggle.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const heads = [...document.querySelectorAll(".hx-dtoggle")];
      const i = heads.indexOf(toggle);
      if (e.key === "ArrowRight" && box.dataset.folded === "true") { toggle.click(); e.preventDefault(); return; }
      if (e.key === "ArrowLeft" && box.dataset.folded !== "true") { toggle.click(); e.preventDefault(); return; }
      const next = heads[i + (e.key === "ArrowDown" ? 1 : e.key === "ArrowUp" ? -1 : 0)];
      if (next && next !== toggle) { next.focus(); e.preventDefault(); }
    });
    const total = el("span", "hx-total tnum");
    const off = el("button", "btn btn-ghost btn-sm hx-off", mic.t("secretaria.horas.mark_off"));
    off.type = "button";
    off.dataset.tip = mic.t("secretaria.horas.mark_off_tip");
    off.addEventListener("click", async () => {
      const r = await call("horas_off", { fecha: d.fecha, libre: true });
      if (r && r.ok) {
        S.plan.dias = S.plan.dias.filter((x) => x.fecha !== d.fecha);
        render();
      }
    });
    top.append(all, toggle, total, off);
    box.append(top);

    const body = el("div", "hx-lines");
    for (const ex of d.existentes || []) {
      const row = el("div", "hx-line existing");
      row.append(el("span", "hx-lc"), el("span", "hx-h tnum", hfmt(ex.horas)), el("span", "hx-p", ex.proyecto || ""), el("span", "hx-dtext", ex.descripcion || ""));
      body.append(row);
    }
    d.lineas.forEach((ln) => body.append(renderLine(d, ln)));
    box.append(body);
    return box;
  }

  function renderLine(d, ln) {
    const row = el("div", "hx-line");
    row.dataset.key = ln.key;
    const res = S.results[ln.key];
    if (ln.hecha) row.dataset.state = "done";
    else if (res && !res.ok) row.dataset.state = "error";
    const cb = el("input", "hx-lcheck");
    cb.type = "checkbox";
    cb.checked = !!ln.checked;
    cb.disabled = !!ln.hecha;
    cb.setAttribute("aria-label", ln.descripcion);
    cb.addEventListener("change", () => { ln.checked = cb.checked; refreshTotals(); });
    const mark = el("span", "hx-lmark");
    if (ln.hecha) mark.innerHTML = mic.icon("check");
    const hours = el("input", "hx-h field-input num");
    hours.type = "number";
    hours.min = "0.25";
    hours.max = "12";
    hours.step = "0.25";
    hours.value = String(ln.horas);
    hours.disabled = !!ln.hecha;
    hours.setAttribute("aria-label", mic.t("secretaria.horas.hours"));
    hours.addEventListener("input", () => {
      const v = parseFloat(hours.value);
      if (!isNaN(v)) { ln.horas = v; ln.checked = true; cb.checked = true; refreshTotals(); }
    });
    const proj = el("select", "hx-p field-select");
    projectOptions(proj, { id: ln.proyecto_id, nombre: ln.proyecto });
    proj.value = String(ln.proyecto_id);
    proj.disabled = !!ln.hecha;
    proj.setAttribute("aria-label", mic.t("secretaria.horas.project"));
    proj.addEventListener("change", () => {
      ln.proyecto_id = parseInt(proj.value, 10);
      ln.proyecto = proj.selectedOptions[0].textContent;
      ln.tarea_id = null;
      ln.checked = true;
      cb.checked = true;
      refreshTotals();
    });
    const desc = el("input", "hx-dtext field-input");
    desc.type = "text";
    desc.value = ln.descripcion;
    desc.disabled = !!ln.hecha;
    desc.spellcheck = false;
    desc.setAttribute("aria-label", mic.t("secretaria.horas.desc"));
    if (ln.variantes && ln.variantes.length > 1) {
      const dl = el("datalist");
      dl.id = "dl-" + ln.key.replace(/[^\w]/g, "-");
      const rng = (ln.descripcion.match(/\s*\(\d{1,2}:\d{2}-\d{1,2}:\d{2}\)\s*$/) || [""])[0];
      ln.variantes.forEach((v) => { const o = el("option"); o.value = v + rng; dl.append(o); });
      desc.setAttribute("list", dl.id);
      row.append(dl);
    }
    desc.addEventListener("input", () => { ln.descripcion = desc.value; ln.checked = true; cb.checked = true; refreshTotals(); });
    const origin = el("span", "hx-origin " + ln.origen, mic.t("secretaria.horas.o." + ln.origen));
    const lc = el("span", "hx-lc");
    lc.append(cb, mark);
    row.prepend(lc, hours, proj, desc, origin);
    if (res && !res.ok && res.error) row.append(el("span", "hx-err", mic.t("secretaria.horas.err." + res.error)));
    return row;
  }

  function refreshTotals() {
    if (!S.plan) return;
    for (const d of S.plan.dias) {
      const box = document.querySelector(`.hx-day[data-fecha="${d.fecha}"]`);
      if (!box) continue;
      const sum = d.lineas.filter((l) => l.checked || l.hecha).reduce((a, l) => a + (+l.horas || 0), 0) + (d.horas || 0);
      const t = box.querySelector(".hx-total");
      t.textContent = mic.t("secretaria.horas.day_total", { h: hfmt(sum) });
      t.dataset.over = String(sum > 10);
      const pending = d.lineas.filter((l) => !l.hecha);
      const all = box.querySelector(".hx-dcheck");
      all.checked = pending.length > 0 && pending.every((l) => l.checked);
      all.indeterminate = !all.checked && pending.some((l) => l.checked);
      all.disabled = pending.length === 0;
      box.dataset.done = String(pending.length === 0 && d.lineas.length > 0);
    }
    const n = checkedLines().length;
    const count = document.querySelector(".hx-count");
    if (count) count.textContent = n ? String(n) : "";
    const apply = document.querySelector(".hx-apply");
    if (apply) apply.disabled = n === 0 || S.busy;
  }

  // ------------------------------------------------------------------ apply / undo
  function snack(text) {
    const s = $("snack");
    if (!s) return;
    s.textContent = text;
    s.classList.add("show");
    clearTimeout(snack.t);
    snack.t = setTimeout(() => s.classList.remove("show"), 3200);
  }

  async function doApply() {
    const lines = checkedLines();
    if (!lines.length || S.busy) return;
    S.busy = true;
    refreshTotals();
    const payload = lines.map((l) => ({
      key: l.key, fecha: l.fecha, proyecto_id: l.proyecto_id, tarea_id: l.tarea_id || null,
      horas: +l.horas, descripcion: l.descripcion,
    }));
    const r = await call("horas_apply", { lineas: payload });
    S.busy = false;
    if (!r || !r.ok) { snack(mic.t((r && r.error) || "library.err.op_failed")); refreshTotals(); return; }
    let bad = 0;
    for (const res of r.resultados) {
      S.results[res.key] = res;
      const ln = lines.find((l) => l.key === res.key);
      if (!ln) continue;
      if (res.ok) { ln.hecha = true; ln.checked = false; } else bad++;
    }
    if (r.lote) S.batch = r.lote;
    render();
    snack(bad ? mic.t("secretaria.horas.applied_some", { n: r.creadas, bad }) : mic.t("secretaria.horas.applied", { n: r.creadas }));
  }

  async function doUndo() {
    if (!S.batch || S.busy) return;
    S.busy = true;
    const r = await call("horas_undo", { lote: S.batch });
    S.busy = false;
    if (!r || !r.ok) { snack(mic.t((r && r.error) || "library.err.op_failed")); return; }
    S.batch = null;
    snack(mic.t("secretaria.horas.undone"));
    open();
  }

  // ------------------------------------------------------------------ wiring
  document.addEventListener("keydown", (e) => {
    if (!S.open || e.key !== "Enter" || e.ctrlKey || e.altKey || e.metaKey) return;
    const t = e.target;
    if (t.tagName === "SELECT" || t.tagName === "BUTTON" || t.tagName === "TEXTAREA") return;
    if (t.closest && t.closest("dialog, .menu, .search")) return;
    const b = document.querySelector(".hx-apply");
    if (b && !b.disabled && !b.hidden) { e.preventDefault(); b.click(); }
  });
  document.addEventListener("keydown", (e) => {
    if (S.open && e.key === "Escape" && !(e.target.closest && e.target.closest("dialog, .menu"))) show(false);
  });
  document.addEventListener("mic:meeting", () => { if (S.open) show(false); });

  const api = (window.micnaga = window.micnaga || {});
  api.horas = { open, close: () => show(false) };

  (async () => {
    await mic.i18nReady;
    const btn = $("horas-btn");
    const caps = await call("caps");
    if (btn && caps && caps.ok && caps.horas) {
      btn.hidden = false;
      btn.addEventListener("click", () => (S.open ? show(false) : open()));
    }
    if (mic.params.get("view") === "horas") open();
  })();
})();
