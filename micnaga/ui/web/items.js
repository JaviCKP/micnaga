/* Editable proposals: one uniform layer for every item type.
   Each proposal carries `campos` ([{k, t, v, opts, req}]) from Python (micnaga/brain/items.py);
   this file turns them into an inline editor, and adds the Google bits (invite notice, links,
   «Contrastes»). Saving calls secretaria("edit") — the item keeps its id. */
(function () {
  "use strict";
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const LABEL = {
    titulo: () => mic.t("items.f.titulo"),
    inicio: () => mic.t("items.f.inicio"),
    duracion_min: () => mic.t("items.f.duracion_min"),
    asistentes: () => mic.t("items.f.asistentes"),
    descripcion: () => mic.t("items.f.descripcion"),
    modo: () => mic.t("items.f.modo"),
    para: () => mic.t("items.f.para"),
    asunto: () => mic.t("items.f.asunto"),
    cuerpo: () => mic.t("items.f.cuerpo"),
    a: () => mic.t("items.f.a"),
    de: () => mic.t("items.f.de"),
    nombre: () => mic.t("items.f.nombre"),
    email: () => mic.t("items.f.email"),
    telefono: () => mic.t("items.f.telefono"),
    cargo: () => mic.t("items.f.cargo"),
    empresa_id: () => mic.t("items.f.empresa_id"),
    etapa_id: () => mic.t("items.f.etapa_id"),
    importe: () => mic.t("items.f.importe"),
    fecha_cierre: () => mic.t("items.f.fecha_cierre"),
    resumen: () => mic.t("items.f.resumen"),
    fecha: () => mic.t("items.f.fecha"),
    id: () => mic.t("items.f.id"),
  };
  const OPT = { draft: () => mic.t("items.opt.draft"), send: () => mic.t("items.opt.send") };
  const label = (k) => (LABEL[k] ? LABEL[k]() : k.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()));

  function when(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleDateString("es-ES", { weekday: "short", day: "numeric", month: "short" }) +
      " · " + d.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
  }

  // ------------------------------------------------------------------ one-line text
  function text(p) {
    const d = p.datos || {};
    switch (p.tipo) {
      case "evento_google": return mic.t("items.event", { title: d.titulo || "", when: when(d.inicio) });
      case "correo_google":
        return d.modo === "send" ? mic.t("items.mail_send", { subject: d.asunto || "" }) : mic.t("items.mail_draft", { subject: d.asunto || "" });
      case "acta_drive": return mic.t("items.drive", { title: d.titulo || "" });
      default: return null;
    }
  }

  /** What will leave your account, said plainly (only when something will). */
  function notice(p) {
    const d = p.datos || {};
    if (p.tipo === "evento_google") {
      const on = (d.asistentes || []).filter((a) => a.on).map((a) => a.nombre || a.email);
      return on.length ? mic.t("items.invite", { who: on.join(", ") }) : mic.t("items.no_invite");
    }
    if (p.tipo === "correo_google" && d.modo === "send") return mic.t("items.will_send", { who: (d.para || []).join(", ") });
    return null;
  }

  // ------------------------------------------------------------------ editor
  function input(f) {
    let node;
    const v = f.v;
    if (f.t === "textarea") {
      node = el("textarea", "field-input it-area");
      node.rows = 5;
      node.value = v || "";
    } else if (f.t === "choice") {
      node = el("div", "seg it-seg");
      node.setAttribute("role", "radiogroup");
      (f.opts || []).forEach((o) => {
        const b = el("button", "seg-b", OPT[o] ? OPT[o]() : o);
        b.type = "button";
        b.setAttribute("role", "radio");
        b.setAttribute("aria-checked", String(o === v));
        b.addEventListener("click", () => {
          node.querySelectorAll(".seg-b").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
          node.dataset.value = o;
          node.dispatchEvent(new Event("change", { bubbles: true }));
        });
        node.append(b);
      });
      node.dataset.value = v;
      node.read = () => node.dataset.value;
      return node;
    } else if (f.t === "bool") {
      node = el("input");
      node.type = "checkbox";
      node.checked = !!v;
      node.read = () => node.checked;
      return node;
    } else if (f.t === "people") {
      return people(v || []);
    } else {
      node = el("input", "field-input");
      node.type = { number: "number", date: "date", datetime: "datetime-local" }[f.t] || "text";
      if (f.t === "emails") node.value = (v || []).join(", ");
      else node.value = v === null || v === undefined ? "" : v;
    }
    node.read = () => {
      if (f.t === "number") return node.value === "" ? null : Number(node.value);
      return node.value;
    };
    return node;
  }

  /** Attendees: unchecked by default; checking one means an invitation will be sent. */
  function people(list) {
    const box = el("div", "it-people");
    const rows = [];
    const add = (a) => {
      const lab = el("label", "it-person");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = !!a.on;
      cb.addEventListener("change", () => box.dispatchEvent(new Event("change", { bubbles: true })));
      lab.append(cb, el("span", "it-pn", a.nombre || a.email));
      if (a.nombre) lab.append(el("span", "it-pe", a.email));
      rows.push({ a, cb });
      box.insertBefore(lab, more);
    };
    const more = el("input", "field-input it-add");
    more.type = "email";
    more.placeholder = mic.t("items.add_person");
    more.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const email = more.value.trim();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return;
      add({ nombre: null, email, on: true });
      more.value = "";
      box.dispatchEvent(new Event("change", { bubbles: true }));
    });
    box.append(more);
    list.forEach(add);
    box.read = () => rows.map(({ a, cb }) => ({ nombre: a.nombre, email: a.email, on: cb.checked }));
    return box;
  }

  function editor(p, onSave, onCancel) {
    const form = el("form", "it-form");
    form.noValidate = true;
    const inputs = {};
    for (const f of p.campos || []) {
      const row = el("div", "it-row" + (f.t === "textarea" || f.t === "people" ? " col" : ""));
      const lab = el("label", "it-label", label(f.k));
      const node = input(f);
      node.id = "it-" + p.id + "-" + f.k;
      lab.htmlFor = node.id;
      inputs[f.k] = node;
      row.append(lab, node);
      form.append(row);
    }
    const slots = (p.datos && p.datos.huecos) || [];
    if (p.tipo === "evento_google" && slots.length > 1 && inputs.inicio) {
      const row = el("div", "it-slots");
      slots.forEach((s) => {
        const b = el("button", "chip", when(s));
        b.type = "button";
        b.addEventListener("click", () => { inputs.inicio.value = s; form.dispatchEvent(new Event("change")); });
        row.append(b);
      });
      inputs.inicio.closest(".it-row").after(row);
    }
    const note = el("p", "it-note");
    const live = () => {
      const draft = { ...p, datos: { ...(p.datos || {}), ...read() } };
      const n = notice(draft);
      note.textContent = n || "";
      note.hidden = !n;
      note.dataset.out = String(!!n && (draft.tipo === "correo_google" || (draft.datos.asistentes || []).some((a) => a.on)));
    };
    const read = () => Object.fromEntries(Object.entries(inputs).map(([k, n]) => [k, n.read()]));
    form.addEventListener("change", live);
    form.addEventListener("input", live);
    const bar = el("div", "it-bar");
    const err = el("span", "it-err");
    const cancel = el("button", "btn btn-ghost btn-sm", mic.t("items.cancel"));
    cancel.type = "button";
    cancel.addEventListener("click", onCancel);
    const save = el("button", "btn btn-primary btn-sm", mic.t("items.save"));
    save.type = "submit";
    bar.append(err, cancel, save);
    form.append(note, bar);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      save.disabled = true;
      const r = await onSave(read());
      save.disabled = false;
      if (r && !r.ok) err.textContent = mic.t("items.invalid", { field: label(r.field || "") });
    });
    form.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); onCancel(); } });
    live();
    setTimeout(() => { const first = form.querySelector("input, textarea, button"); if (first) first.focus(); }, 0);
    return form;
  }

  // ------------------------------------------------------------------ links and contrasts
  function link(url, textValue, call) {
    const a = el("button", "it-link", textValue);
    a.type = "button";
    a.insertAdjacentHTML("beforeend", mic.icon("external", "mini"));
    a.addEventListener("click", (e) => { e.stopPropagation(); call("open_link", { url }); });
    return a;
  }

  function result(p, call) {
    const r = p.resultado || {};
    if (p.estado !== "aplicada" || !(r.url || r.meet)) return null;
    const box = el("span", "it-res");
    if (r.url) box.append(link(r.url, p.tipo === "correo_google" ? mic.t("items.open_mail") : mic.t("items.open"), call));
    if (r.meet) box.append(link(r.meet, "Meet", call));
    const m = (p.espejo || {}).estado;
    if (m === "creado" || m === "actualizado") box.append(el("span", "it-mirror", mic.t("items.mirror_ok")));
    else if (m === "nativo") box.append(el("span", "it-mirror", mic.t("items.mirror_native")));
    else if (m === "error") box.append(el("span", "it-mirror warn", mic.t("items.mirror_error")));
    return box;
  }

  function actaExtras(a, call, stamp) {
    const out = [];
    const cs = a.contrastes || [];
    if (cs.length) {
      const box = el("div", "sec-block it-contrasts");
      box.append(el("h4", "caps", mic.t("items.contrasts")));
      const ul = el("ul", "sec-list");
      cs.forEach((c) => {
        const li = el("li", "sec-li");
        li.append(el("span", "sec-g", "≠"));
        const x = el("span", "sec-x");
        x.append(el("b", "", mic.t("items.said") + " "), c.dicho, el("br"), el("b", "", mic.t("items.written") + " "), c.escrito + " ");
        if (c.url) x.append(link(c.url, c.fuente, call)); else x.append(el("span", "it-src", c.fuente));
        li.append(x);
        const s = stamp(c.t);
        if (s) li.append(s);
        ul.append(li);
      });
      box.append(ul);
      out.push(box);
    }
    const ls = a.enlaces || [];
    if (ls.length) {
      const box = el("div", "sec-block");
      box.append(el("h4", "caps", mic.t("items.links")));
      const row = el("div", "it-links");
      ls.forEach((l) => row.append(link(l.url, l.titulo, call)));
      box.append(row);
      out.push(box);
    }
    return out;
  }

  const ERR = {
    google_denied: () => mic.t("items.err.denied"),
    google_guard: () => mic.t("items.err.guard"),
    google_timeout: () => mic.t("items.err.timeout"),
    google_offline: () => mic.t("items.err.offline"),
    google_unguarded: () => mic.t("items.err.unguarded"),
    not_undoable: () => mic.t("items.err.not_undoable"),
  };
  const errorText = (key) => (ERR[key] ? ERR[key]() : null);

  window.micnaga = window.micnaga || {};
  window.micnaga.items = { text, notice, editor, result, actaExtras, errorText };
})();
