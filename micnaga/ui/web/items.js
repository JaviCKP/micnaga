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
    row.append(more, el("span", "sec-inv-n", mic.t("secretaria.invite_note")));
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
    google_unknown_outcome: () => mic.t("items.err.unknown_outcome"),
  };
  const errorText = (key) => (ERR[key] ? ERR[key]() : null);

  window.micnaga = window.micnaga || {};
  window.micnaga.items = { text, notice, control, result, actaExtras, errorText };
})();
