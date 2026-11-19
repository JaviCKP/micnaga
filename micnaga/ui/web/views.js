/* Micnaga — one meeting, four quiet views instead of one long scroll.

   Resumen (what you see first): two or three sentences, what matters to you at a glance and one
   clear «Revisar N propuestas». Acta: the full structured minutes («Acta completa» inside).
   Propuestas: only the review list. Transcripción: the text with the player.

   It only switches what is visible: secretaria.js and library.js keep rendering as before
   (#sec holds .sec-acta + .sec-props, #tr the phrases). Tabs appear once there is an acta. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const sec = () => window.micnaga && window.micnaga.secretaria && window.micnaga.secretaria.state();
  const VIEWS = ["resumen", "acta", "propuestas", "transcripcion"];
  const LABEL = {
    resumen: "views.resumen", acta: "views.acta", propuestas: "views.propuestas", transcripcion: "views.transcripcion",
  };
  const V = { view: "transcripcion", chosen: false, folder: null };

  function fmtDay(iso) {
    const d = new Date(String(iso).slice(0, 10) + "T12:00:00");
    return isNaN(d) ? String(iso) : d.toLocaleDateString("es-ES", { weekday: "short", day: "numeric", month: "short" });
  }
  const pending = (a) => ((a && a.propuestas) || []).filter((p) => p.estado === "pendiente");

  // ---------------------------------------------------------------- tabs
  function tabs() {
    let bar = $("views");
    if (bar) return bar;
    bar = el("nav", "views");
    bar.id = "views";
    bar.setAttribute("role", "tablist");
    bar.hidden = true;
    VIEWS.forEach((v) => {
      const b = el("button", "view-tab");
      b.type = "button";
      b.dataset.view = v;
      b.setAttribute("role", "tab");
      b.append(el("span", "", mic.t(LABEL[v])));
      b.addEventListener("click", () => setView(v, true));
      bar.append(b);
    });
    const head = document.querySelector(".mh");
    head.append(bar);
    return bar;
  }

  function setView(v, byUser) {
    if (!VIEWS.includes(v)) return;
    V.view = v;
    if (byUser) V.chosen = true;
    $("main").dataset.view = v;
    document.querySelectorAll(".view-tab").forEach((b) => {
      const on = b.dataset.view === v;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
    });
    const scroller = v === "resumen" ? $("brief") : $("tr");
    if (scroller && byUser) scroller.scrollTop = 0;
  }

  // ---------------------------------------------------------------- the brief
  function brief() {
    let b = $("brief");
    if (b) return b;
    b = el("div", "transcript brief");
    b.id = "brief";
    $("tr").before(b);
    return b;
  }

  function row(icon, label, text, extra) {
    const r = el("div", "bf-row");
    const k = el("span", "bf-k");
    k.innerHTML = mic.icon(icon, "mini");
    k.append(label);
    const x = el("span", "bf-x");
    x.append(text);
    if (extra) x.append(el("span", "bf-more", extra));
    r.append(k, x);
    return r;
  }

  function renderBrief(a) {
    const b = brief();
    const box = el("section", "bf");
    const sum = (a.resumen || []).slice(0, 3).join(" ");
    if (a.contexto) box.append(el("p", "bf-ctx", a.contexto));
    if (sum) box.append(el("p", "bf-sum", sum));

    const rows = el("div", "bf-rows");
    const mine = (a.tareas || []).filter((t) => t.es_mia);
    if (mine.length) {
      const t = mine[0];
      rows.append(row("task", mic.t("views.b.yours"), t.texto + (t.fecha ? " · " + fmtDay(t.fecha) : ""),
        mine.length > 1 ? "+" + (mine.length - 1) : null));
    }
    const next = a.proximos_pasos || [];
    if (next.length) rows.append(row("arrow", mic.t("views.b.next"), next[0].texto, next.length > 1 ? "+" + (next.length - 1) : null));
    const dec = a.decisiones || [];
    if (dec.length) rows.append(row("check", mic.t("views.b.decided"), dec[0].texto, dec.length > 1 ? "+" + (dec.length - 1) : null));
    const since = a.desde_la_ultima || [];
    if (since.length) {
      const open = since.filter((s) => s.estado === "abierto").length;
      const done = since.filter((s) => s.estado === "hecho").length;
      const parts = [];
      if (done) parts.push(mic.t("views.b.done", { n: done }));
      if (open) parts.push(mic.t("views.b.open", { n: open }));
      if (parts.length) rows.append(row("restart", mic.t("views.b.since"), parts.join(" · ")));
    }
    if (rows.childElementCount) box.append(rows);

    const acts = el("div", "bf-acts");
    const n = pending(a).length;
    if (n) {
      const go = el("button", "btn btn-primary bf-go");
      go.type = "button";
      go.append(n === 1 ? mic.t("views.b.review_one") : mic.t("views.b.review", { n }));
      go.insertAdjacentHTML("beforeend", mic.icon("arrow", "mini"));
      go.addEventListener("click", () => setView("propuestas", true));
      acts.append(go);
    }
    const acta = el("button", "btn btn-ghost bf-acta", mic.t("views.b.see_acta"));
    acta.type = "button";
    acta.addEventListener("click", () => setView("acta", true));
    acts.append(acta);
    box.append(acts);
    b.replaceChildren(box);
  }

  // ---------------------------------------------------------------- sync with what is rendered
  function sync() {
    const S = sec();
    const a = S && S.acta;
    const folder = S && S.folder;
    if (folder !== V.folder) {            // another meeting: start again from its summary
      V.folder = folder;
      V.chosen = false;
    }
    const has = !!(a && (a.resumen || a.titulo));
    tabs().hidden = !has;
    $("sec").classList.toggle("has-acta", has);
    if (!has) {
      if ($("brief")) $("brief").replaceChildren();
      setView("transcripcion");
      return;
    }
    renderBrief(a);
    const n = pending(a).length;
    const all = (a.propuestas || []).length;
    const pt = document.querySelector('.view-tab[data-view="propuestas"]');
    pt.hidden = !all;
    let c = pt.querySelector(".view-n");
    if (!c) { c = el("span", "view-n tnum"); pt.append(c); }
    c.textContent = n ? String(n) : "";
    c.hidden = !n;
    if (!V.chosen) setView("resumen");
    else if (V.view === "propuestas" && !all) setView("resumen");
    else setView(V.view);
  }

  let queued = false;
  const later = () => { if (!queued) { queued = true; setTimeout(() => { queued = false; sync(); }, 30); } };

  function boot() {
    if (!$("sec") || !$("tr")) return;
    tabs();
    brief();
    new MutationObserver(later).observe($("sec"), { childList: true, subtree: true, characterData: true });
    // a search hit or a toast opens a meeting AT a moment: show it in the transcript
    const lib = window.micnaga && window.micnaga.library;
    if (lib && lib.open) {
      const open = lib.open.bind(lib);
      lib.open = (folder, t) => {
        const r = open(folder, t);
        if (typeof t === "number") { V.chosen = true; setView("transcripcion"); }
        return r;
      };
    }
    document.addEventListener("keydown", (e) => {
      const tab = e.target && e.target.closest && e.target.closest(".view-tab");
      if (!tab || (e.key !== "ArrowRight" && e.key !== "ArrowLeft")) return;
      const vis = [...document.querySelectorAll(".view-tab")].filter((b) => !b.hidden);
      const i = vis.indexOf(tab) + (e.key === "ArrowRight" ? 1 : -1);
      const nb = vis[(i + vis.length) % vis.length];
      nb.focus();
      setView(nb.dataset.view, true);
      e.preventDefault();
    });
    sync();
  }

  window.micnaga = window.micnaga || {};
  window.micnaga.views = { set: (v) => setView(v, true), get: () => V.view, sync };
  const start = () => (mic.i18nReady ? mic.i18nReady.then(boot) : boot());
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
