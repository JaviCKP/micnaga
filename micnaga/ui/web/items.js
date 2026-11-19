/* Google items in the review: their one-line text, the «what leaves your account» notice, the
   extra field kinds for the shared editor (micnaga/brain/google/items.py sends `campos`), result
   links and «Contrastes». Editing goes through secretaria("edit"); the item keeps its id. */
(function () {
  "use strict";
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const OPT = { draft: () => mic.t("items.opt.draft"), send: () => mic.t("items.opt.send") };

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

  /** [main line, quiet facts] for the review card (secretaria.js draws the card). */
  function lines(p) {
    const d = p.datos || {};
    switch (p.tipo) {
      case "evento_google": return [d.titulo || "", [when(d.inicio), d.duracion_min ? d.duracion_min + " min" : null, "Meet"]];
      case "correo_google":
        return [d.asunto || "", [d.modo === "send" ? mic.t("items.opt.send") : mic.t("items.opt.draft"), (d.para || []).join(", ")]];
      case "acta_drive": return [d.titulo || "", [mic.t("items.k.drive")]];
      default: return null;
    }
  }

  /** What will leave your account, said plainly (only when something will). */
  function notice(p) {
    const d = p.datos || {};
    if (p.tipo === "evento_google") {
      const on = (d.asistentes || []).filter((a) => a.on).map((a) => a.nombre || a.email);
      return on.length ? mic.t("items.invite", { who: on.join(", ") }) : null;
    }
    if (p.tipo === "correo_google" && d.modo === "send") return mic.t("items.will_send", { who: (d.para || []).join(", ") });
    return null;
  }

  // ------------------------------------------------------------------ controls
  // The review editor is the one in secretaria.js (fields from `campos`, saved on change). These
  // are the extra field kinds Google items use; `save(value)` sends that one change.
  function control(p, f, save) {
    const v = f.valor;
    if (f.tipo === "datetime") {
      const box = el("div", "it-dt");
      const c = el("input", "field-input");
      c.type = "datetime-local";
      c.value = v || "";
      c.addEventListener("change", () => { if (c.value) save(c.value); });
      box.append(c);
      const slots = (p.datos && p.datos.huecos) || [];
      if (slots.length > 1) {
        const row = el("div", "it-slots");
        slots.forEach((s) => {
          const b = el("button", "sec-chipbtn", when(s));
          b.type = "button";
          b.setAttribute("aria-pressed", String(String(s).slice(0, 16) === String(v || "").slice(0, 16)));
          b.addEventListener("click", () => save(String(s).slice(0, 16)));
          row.append(b);
        });
        box.append(row);
      }
      return box;
    }
    if (f.tipo === "choice") {
      const seg = el("div", "seg it-seg");
      seg.setAttribute("role", "radiogroup");
      (f.opciones_fijas || []).forEach((o) => {
        const b = el("button", "seg-b", OPT[o] ? OPT[o]() : o);
        b.type = "button";
        b.setAttribute("role", "radio");
        b.setAttribute("aria-checked", String(o === v));
        b.addEventListener("click", () => { if (o !== v) save(o); });
        seg.append(b);
      });
      return seg;
    }
    if (f.tipo === "emails") {
      const c = el("input", "field-input");
      c.type = "text";
      c.spellcheck = false;
      c.value = (v || []).join(", ");
      c.addEventListener("change", () => save(c.value));
      return c;
    }
    if (f.tipo === "people") return people(v || [], save);
    return null;
  }

  /** Attendees: off by default; switching one on means Google will send them an invitation. */
  function people(list, save) {
    const row = el("div", "sec-inv");
    list.forEach((a, i) => {
      const b = el("button", "sec-chipbtn", a.nombre || a.email);
      b.type = "button";
      b.title = a.email;
      b.setAttribute("aria-pressed", String(!!a.on));
      b.addEventListener("click", () => save(list.map((x, j) => ({ ...x, on: j === i ? !x.on : !!x.on }))));
      row.append(b);
    });
    const more = el("input", "field-input it-add");
    more.type = "email";
    more.spellcheck = false;
    more.placeholder = mic.t("items.add_person");
    more.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const email = more.value.trim();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return;
      save([...list, { nombre: null, email, on: true }]);
    });
    row.append(more);
    return row;
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
        const li = el("li", "sec-li it-contrast");
        li.append(el("span", "sec-g", "≠"));
        const x = el("span", "sec-x");
        const said = el("span", "it-said");
        said.append(el("b", "", mic.t("items.said")), " " + c.dicho);
        const wrote = el("span", "it-wrote");
        wrote.append(el("b", "", mic.t("items.written")), " " + c.escrito + " ");
        if (c.url) wrote.append(link(c.url, c.fuente, call)); else wrote.append(el("span", "it-src", c.fuente));
        x.append(said, wrote);
        li.append(x);
        const s = stamp(c.t);
        if (s) li.append(s);
        ul.append(li);
      });
      box.append(ul);
      out.push(box);
    }
    return out;
  }

  // ------------------------------------------------------------------ research
  const MARK = { hecho: "check", abierto: "clock", desconocido: "help" };
  const SRC_ICON = { acta: "note", gmail: "mail", drive: "doc", calendar: "calendar", odoo: "app" };
  const SRC = { acta: "items.src.acta", gmail: "items.src.gmail", drive: "items.src.drive", calendar: "items.src.calendar", odoo: "items.src.odoo" };
  const fmtDay = (iso) => {
    const d = new Date(String(iso).slice(0, 10) + "T12:00:00");
    return isNaN(d) ? String(iso) : d.toLocaleDateString("es-ES", { day: "numeric", month: "short" });
  };

  function sourceLink(s, call, textValue) {
    if (s.tipo === "acta") {
      const b = el("button", "it-link", textValue);
      b.type = "button";
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        const lib = window.micnaga && window.micnaga.library;
        if (lib) lib.open(s.ref);
      });
      return b;
    }
    return s.url ? link(s.url, textValue, call) : el("span", "it-src", textValue);
  }

  /** «Desde la última vez»: what the previous meeting left open, with its proof. */
  function since(a, call, stamp) {
    const items = a.desde_la_ultima || [];
    if (!items.length) return null;
    const box = el("div", "it-since");
    box.append(el("h4", "caps", mic.t("items.since")));
    const ul = el("ul", "sec-list");
    items.forEach((it) => {
      const li = el("li", "sec-li");
      const state = MARK[it.estado] ? it.estado : "desconocido";
      li.dataset.state = state;
      const chip = el("span", "it-state " + state);
      chip.innerHTML = mic.icon(MARK[state], "mini");
      chip.append(mic.t("items.state." + state));
      const x = el("span", "sec-x", it.texto);
      if (it.evidencia) {
        const ev = sourceLink(it.evidencia, call, it.evidencia.titulo);
        ev.classList.add("it-ev");
        x.append(" ", ev);
      }
      li.append(chip, x);
      const s = stamp(it.t);
      if (s) li.append(s);
      ul.append(li);
    });
    box.append(ul);
    return box;
  }

  /** «Fuentes»: one small row (Acta 12 nov · Gmail · Odoo), each one opens its source. */
  function sources(a, call) {
    const seen = new Set();
    const all = [];
    for (const s of a.fuentes || []) {
      const k = s.url || s.ref;
      if (!seen.has(k)) { seen.add(k); all.push(s); }
    }
    for (const l of a.enlaces || []) {
      if (l.url && !seen.has(l.url)) { seen.add(l.url); all.push({ tipo: l.fuente || "drive", titulo: l.titulo, url: l.url }); }
    }
    if (!all.length) return null;
    const row = el("div", "it-sources");
    row.append(el("span", "it-sources-l caps", mic.t("items.sources")));
    all.forEach((s) => {
      const label = mic.t(SRC[s.tipo] || "items.src.odoo") + (s.tipo === "acta" ? " " + fmtDay(s.ref) : "");
      const b = sourceLink(s, call, label);
      b.classList.add("it-srcchip");
      b.insertAdjacentHTML("afterbegin", mic.icon(SRC_ICON[s.tipo] || "app", "mini"));
      b.title = s.titulo || "";
      row.append(b);
    });
    return row;
  }

  const ERR = {
    google_denied: () => mic.t("items.err.denied"),
    google_guard: () => mic.t("items.err.guard"),
    google_timeout: () => mic.t("items.err.timeout"),
    google_offline: () => mic.t("items.err.offline"),
    google_unguarded: () => mic.t("items.err.unguarded"),
    not_undoable: () => mic.t("items.err.not_undoable"),
    google_unknown_outcome: () => mic.t("items.err.unknown_outcome"),
  };
  const errorText = (key) => (ERR[key] ? ERR[key]() : null);

  window.micnaga = window.micnaga || {};
  window.micnaga.items = { text, lines, notice, control, result, actaExtras, since, sources, errorText };
})();
