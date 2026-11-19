/* «Acta completa»: the long minutes, on demand, read like a document inside the acta panel.
   Python writes it with one read-only `claude -p` (streamed); this view shows the text as it
   arrives, then the cached Markdown. Every [mm:ss] seeks the player. Copy / open file / redo. */
(function () {
  "use strict";
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const lib = () => window.micnaga && window.micnaga.library;
  const STAMP = /\[(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\]/g;
  const views = new Map();   // folder -> {node, doc, live, timer}

  /** Inline Markdown (bold, italic, code) + clickable [mm:ss]; text only, never HTML. */
  function inline(parent, text) {
    const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|\[(?:\d{1,2}:)?\d{1,2}:\d{2}\]|\[[^\]]+\]\([^)]+\))/g;
    let pos = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > pos) parent.append(text.slice(pos, m.index));
      const t = m[0];
      if (t.startsWith("**")) inline(parent.appendChild(el("strong")), t.slice(2, -2));
      else if (t.startsWith("`")) parent.append(el("code", "", t.slice(1, -1)));
      else if (t.startsWith("*")) inline(parent.appendChild(el("em")), t.slice(1, -1));
      else if (/^\[[\d:]+\]$/.test(t)) {
        // the stamp keeps the punctuation that follows it on the same line («[00:20].»)
        const tail = /^[.,;:)»]+/.exec(text.slice(m.index + t.length));
        const keep = el("span", "fa-nb");
        keep.append(stampLink(t));
        if (tail) { keep.append(tail[0]); re.lastIndex += tail[0].length; }
        parent.append(keep);
        pos = m.index + t.length + (tail ? tail[0].length : 0);
        continue;
      } else { const lm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(t); parent.append(lm ? lm[1] : t); }
      pos = m.index + t.length;
    }
    if (pos < text.length) parent.append(text.slice(pos));
  }
  function stampLink(t) {
    STAMP.lastIndex = 0;
    const m = STAMP.exec(t);
    const secs = (parseInt(m[1] || "0", 10) * 3600) + parseInt(m[2], 10) * 60 + parseInt(m[3], 10);
    const b = el("button", "fa-t tnum", t.slice(1, -1));
    b.type = "button";
    b.setAttribute("aria-label", mic.t("secretaria.play_at", { t: t.slice(1, -1) }));
    b.addEventListener("click", () => { if (lib()) lib().jumpTo(secs, true); });
    return b;
  }

  /** Block Markdown: headings, lists, paragraphs, rules. */
  function markdown(md) {
    const out = el("div", "fa-doc");
    let list = null, para = [];
    const flushPara = () => { if (para.length) { const p = el("p"); inline(p, para.join(" ")); out.append(p); para = []; } };
    const flushList = () => { list = null; };
    for (const raw of String(md).split(/\r?\n/)) {
      const line = raw.trimEnd();
      let m;
      if (!line.trim()) { flushPara(); flushList(); continue; }
      if ((m = /^(#{1,4})\s+(.*)$/.exec(line))) {
        flushPara(); flushList();
        const h = el("h" + Math.min(4, m[1].length + 1));
        inline(h, m[2]);
        out.append(h);
      } else if (/^(-{3,}|\*{3,})$/.test(line.trim())) {
        flushPara(); flushList(); out.append(el("hr"));
      } else if ((m = /^\s*([-*•]|\d+[.)])\s+(.*)$/.exec(line))) {
        flushPara();
        const ordered = /\d/.test(m[1]);
        if (!list || list.tagName !== (ordered ? "OL" : "UL")) { list = el(ordered ? "ol" : "ul"); out.append(list); }
        const li = el("li");
        inline(li, m[2]);
        list.append(li);
      } else {
        flushList();
        para.push(line.trim());
      }
    }
    flushPara();
    return out;
  }

  function view(folder, call, onClose) {
    let v = views.get(folder);
    if (!v) {
      v = { node: el("section", "fa"), doc: null, live: null, timer: 0, folder, call, onClose, copied: 0 };
      v.node.setAttribute("aria-label", mic.t("secretaria.full"));
      views.set(folder, v);
      load(v);
    }
    v.call = call;
    v.onClose = onClose;
    paint(v);
    return v.node;
  }

  async function load(v) {
    const r = await v.call("full_acta", { folder: v.folder });
    if (views.get(v.folder) !== v) return;   // another meeting is open now
    if (!r || !r.ok) { v.live = { fase: "error", error: "failed" }; paint(v); return; }
    v.doc = r.doc;
    v.live = r.live;
    if (!v.doc && !v.live) { make(v); return; }
    paint(v);
    if (v.live && v.live.fase === "escribiendo") { clearTimeout(v.timer); v.timer = setTimeout(() => load(v), 450); }
  }

  async function make(v) {
    v.live = { fase: "escribiendo", texto: "" };
    paint(v);
    const r = await v.call("full_acta_make", { folder: v.folder });
    if (!r || !r.ok) { v.live = { fase: "error", error: (r && r.error) || "failed" }; paint(v); return; }
    clearTimeout(v.timer);
    v.timer = setTimeout(() => load(v), 450);
  }

  function action(icon, label, fn) {
    const b = el("button", "icon-btn fa-act");
    b.type = "button";
    b.innerHTML = mic.icon(icon);
    b.dataset.tip = label;
    b.setAttribute("aria-label", label);
    b.addEventListener("click", fn);
    return b;
  }

  function paint(v) {
    const node = v.node;
    const writing = v.live && v.live.fase === "escribiendo";
    const head = el("div", "fa-head");
    const back = el("button", "fa-back", mic.t("secretaria.acta"));
    back.type = "button";
    back.insertAdjacentHTML("afterbegin", mic.icon("chevron", "mini"));
    back.addEventListener("click", () => v.onClose && v.onClose());
    head.append(back, el("span", "fa-title caps", mic.t("secretaria.full")));
    const meta = el("span", "fa-meta tnum");
    if (writing) meta.append(el("span", "fa-live", mic.t("secretaria.full_writing")));
    else if (v.doc && v.doc.meta && v.doc.meta.words) meta.append(mic.t("secretaria.full_words", { n: v.doc.meta.words }));
    if (!writing && v.doc && v.doc.stale) meta.append(el("span", "fa-stale", mic.t("secretaria.full_stale")));
    head.append(meta);
    const acts = el("div", "fa-acts");
    if (v.doc && !writing) {
      acts.append(
        action(v.copied && Date.now() - v.copied < 1600 ? "check" : "copy", mic.t("secretaria.full_copy"), async () => {
          try { await navigator.clipboard.writeText(v.doc.md); v.copied = Date.now(); paint(v); setTimeout(() => paint(v), 1700); } catch (e) { /* clipboard denied */ }
        }),
        action("folder", mic.t("secretaria.full_open"), () => v.call("full_acta_open", { folder: v.folder })),
        action("refresh", mic.t("secretaria.full_redo"), () => make(v)),
      );
    }
    head.append(acts);
    let body;
    if (writing) {
      body = v.live.texto ? markdown(v.live.texto) : el("div", "fa-doc fa-wait", mic.t("secretaria.full_writing"));
      body.classList.add("streaming");
    } else if (v.live && v.live.fase === "error") {
      body = el("div", "fa-doc fa-err");
      body.append(el("p", "", mic.t("secretaria.err." + (v.live.error || "failed"))));
      const retry = el("button", "btn btn-sm", mic.t("secretaria.retry"));
      retry.type = "button";
      retry.addEventListener("click", () => make(v));
      body.append(retry);
    } else if (v.doc) {
      body = markdown(v.doc.md);
    } else {
      body = el("div", "fa-doc fa-wait", mic.t("secretaria.full_writing"));
    }
    const scroller = node.querySelector(".fa-body");
    const keep = scroller ? scroller.scrollTop : 0;
    const wrap = el("div", "fa-body");
    wrap.append(body);
    node.replaceChildren(head, wrap);
    wrap.scrollTop = writing ? wrap.scrollHeight : keep;
  }

  function forget(folder) {
    const v = views.get(folder);
    if (v) clearTimeout(v.timer);
    views.delete(folder);
  }

  window.micnaga = window.micnaga || {};
  window.micnaga.fullActa = { view, forget, markdown };
})();
