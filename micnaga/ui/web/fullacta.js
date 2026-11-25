/* «Acta completa»: the long minutes, on demand, read like a document in the «Acta» view.
   Python writes it with one read-only `claude -p` (streamed). While it is being written the view
   shows ONLY a loading line (a dithered light that runs along it), the time it has taken and how
   far it has got; then the cached Markdown, laid out as a document: the header block, an index
   of sections that follows the reading, tables, nested lists and quotes. It scrolls with the
   view (one scroller, the whole height), never in a box of its own. Every [mm:ss] seeks the
   player. Copy / open file / redo. DOM is built from text only, never HTML. */
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
  const views = new Map();   // folder -> {node, doc, live, timer, loader}

  // ------------------------------------------------------------------ inline
  /** Inline Markdown (bold, italic, code, links as text) + clickable [mm:ss]; text only. */
  function inline(parent, text) {
    const re = /(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*\s][^*]*\*|\b_[^_\s][^_]*_\b|\[(?:\d{1,2}:)?\d{1,2}:\d{2}\]|\[[^\]]+\]\([^)]+\))/g;
    let pos = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > pos) parent.append(text.slice(pos, m.index));
      const t = m[0];
      if (t.startsWith("**") || t.startsWith("__")) inline(parent.appendChild(el("strong")), t.slice(2, -2));
      else if (t.startsWith("`")) parent.append(el("code", "", t.slice(1, -1)));
      else if (t.startsWith("*") || t.startsWith("_")) inline(parent.appendChild(el("em")), t.slice(1, -1));
      else if (/^\[[\d:]+\]$/.test(t)) {
        // the stamp keeps the punctuation that follows it on the same line («[00:20].»)
        const tail = /^[.,;:)»–-]+/.exec(text.slice(m.index + t.length));
        const keep = el("span", "fa-nb");
        keep.append(stampLink(t));
        if (tail) { keep.append(tail[0]); re.lastIndex += tail[0].length; }
        parent.append(keep);
        pos = m.index + t.length + (tail ? tail[0].length : 0);
        continue;
      } else {
        const lm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(t);
        if (lm) inline(parent.appendChild(el("span", "fa-link")), lm[1]);
        else parent.append(t);
      }
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

  // ------------------------------------------------------------------ blocks
  const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
  const cells = (line) => {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
  };
  const META = /^\s*\*\*([^*]{1,40}?):?\*\*:?\s+(.+)$/;   // «**Fecha:** jueves…»
  const LIST = /^(\s*)([-*+•]|\d+[.)])\s+(.*)$/;

  /** Block Markdown: headings, paragraphs, nested lists, tables, quotes, code, rules and the
   *  «**Campo:** valor» header block (shown as a small two-column grid). */
  function markdown(md) {
    const out = el("div", "fa-doc");
    const lines = String(md).replace(/\r\n?/g, "\n").split("\n");
    let para = [];
    const flushPara = () => {
      if (!para.length) return;
      const p = el("p");
      para.forEach((ln, i) => { if (i) p.append(ln.hard ? el("br") : " "); inline(p, ln.text); });
      out.append(p);
      para = [];
    };
    let i = 0;
    while (i < lines.length) {
      const raw = lines[i];
      const line = raw.replace(/\s+$/, "");
      let m;
      if (!line.trim()) { flushPara(); i++; continue; }
      // fenced code
      if (/^\s*(```|~~~)/.test(line)) {
        flushPara();
        const fence = line.trim().slice(0, 3);
        const buf = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) buf.push(lines[i++]);
        i++;
        const pre = el("pre", "fa-pre");
        pre.append(el("code", "", buf.join("\n")));
        out.append(pre);
        continue;
      }
      if ((m = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line))) {
        flushPara();
        const level = m[1].length;
        const h = el("h" + Math.min(5, level + 1));   // «#» is the document title (h2)
        inline(h, m[2]);
        out.append(h);
        i++;
        continue;
      }
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flushPara(); out.append(el("hr")); i++; continue; }
      // table: a header row followed by the |---|---| row
      if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
        flushPara();
        const head = cells(line);
        const align = cells(lines[i + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
        out.append(table(head, rows, align));
        continue;
      }
      if (/^\s*>/.test(line)) {
        flushPara();
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
        const q = el("blockquote");
        q.append(...markdown(buf.join("\n")).childNodes);
        out.append(q);
        continue;
      }
      if (LIST.test(line)) {
        flushPara();
        const items = [];
        while (i < lines.length) {
          const ln = lines[i];
          if (!ln.trim()) {
            // a blank line inside a list only continues it if the next line is still a list item
            if (i + 1 < lines.length && LIST.test(lines[i + 1])) { i++; continue; }
            break;
          }
          const lm = LIST.exec(ln);
          if (lm) items.push({ indent: lm[1].replace(/\t/g, "    ").length, ordered: /\d/.test(lm[2]), start: parseInt(lm[2], 10), text: lm[3] });
          else if (/^\s{2,}\S/.test(ln) && items.length) items[items.length - 1].text += " " + ln.trim();
          else break;
          i++;
        }
        out.append(list(items));
        continue;
      }
      // the header block: consecutive «**Campo:** valor» lines
      if (META.test(line) && (i + 1 >= lines.length || META.test(lines[i + 1]) || !lines[i + 1].trim())) {
        flushPara();
        const dl = el("dl", "fa-meta-grid");
        while (i < lines.length && (m = META.exec(lines[i]))) {
          dl.append(el("dt", "", m[1].replace(/:$/, "")));
          const dd = el("dd");
          inline(dd, m[2].trim());
          dl.append(dd);
          i++;
        }
        out.append(dl);
        continue;
      }
      // paragraph line (two trailing spaces or a trailing backslash = hard break)
      para.push({ text: line.trim().replace(/\\$/, ""), hard: para.length > 0 && (/ {2,}$/.test(lines[i - 1] || "") || /\\$/.test((lines[i - 1] || "").trim())) });
      i++;
    }
    flushPara();
    return out;
  }

  function list(items) {
    // nesting by indentation: a deeper item opens a sub-list inside the previous <li>
    const root = el(items[0] && items[0].ordered ? "ol" : "ul");
    if (items[0] && items[0].ordered && items[0].start > 1) root.start = items[0].start;
    const stack = [{ indent: items.length ? items[0].indent : 0, node: root }];
    for (const it of items) {
      while (stack.length > 1 && it.indent < stack[stack.length - 1].indent) stack.pop();
      let top = stack[stack.length - 1];
      if (it.indent > top.indent && top.node.lastElementChild) {
        const sub = el(it.ordered ? "ol" : "ul");
        top.node.lastElementChild.append(sub);
        stack.push({ indent: it.indent, node: sub });
        top = stack[stack.length - 1];
      }
      const li = el("li");
      const task = /^\[([ xX])\]\s+(.*)$/.exec(it.text);
      if (task) {
        li.classList.add("fa-task");
        if (task[1] !== " ") li.classList.add("done");
        inline(li, task[2]);
      } else inline(li, it.text);
      top.node.append(li);
    }
    return root;
  }

  function table(head, rows, align) {
    const wrap = el("div", "fa-table");
    const t = el("table");
    const thead = el("thead");
    const tr = el("tr");
    head.forEach((h, k) => { const th = el("th"); inline(th, h); if (align[k]) th.style.textAlign = align[k]; tr.append(th); });
    thead.append(tr);
    const tbody = el("tbody");
    for (const r of rows) {
      const row = el("tr");
      for (let k = 0; k < head.length; k++) {
        const td = el("td");
        inline(td, r[k] || "");
        if (align[k]) td.style.textAlign = align[k];
        row.append(td);
      }
      tbody.append(row);
    }
    t.append(thead, tbody);
    wrap.append(t);
    return wrap;
  }

  // ------------------------------------------------------------------ the loading line
  /** A row of 3 px cells; a soft light runs along it and dissolves through an 8x8 Bayer matrix
   *  (the app's dither), in the accent. Driven by rAF while connected; one still frame with
   *  animations off. The node survives repaints (it is reused), so it never restarts. */
  const BAYER = (() => {
    let m = [[0, 2], [3, 1]];
    while (m.length < 8) {
      const n = m.length;
      m = Array.from({ length: 2 * n }, (_, y) => Array.from({ length: 2 * n }, (_, x) => 4 * m[y % n][x % n] + [0, 2, 3, 1][(y >= n) * 2 + (x >= n)]));
    }
    return m.map((r) => r.map((v) => (v + 0.5) / 64));
  })();
  function loader(v) {
    const box = el("div", "fa-loading");
    box.setAttribute("role", "status");
    const cv = el("canvas", "fa-line");
    cv.setAttribute("aria-hidden", "true");
    const label = el("div", "fa-load-label", mic.t("secretaria.full_writing_long"));
    const meta = el("div", "fa-load-meta tnum");
    box.append(cv, label, meta);
    const CELL = 4, W = 100, H = 7;   // 400 x 28 px of cells
    cv.width = W; cv.height = H;
    const g = cv.getContext("2d");
    const img = g ? g.createImageData(W, H) : null;
    const t0 = performance.now();
    let colors = null, lastMeta = 0;
    const tone = () => {
      const cs = getComputedStyle(document.documentElement);
      const hex = (s, d) => { const m = /#([0-9a-f]{6})/i.exec(s); return m ? m[1] : d; };
      const rgb = (h) => [0, 2, 4].map((k) => parseInt(h.slice(k, k + 2), 16));
      const dark = document.documentElement.dataset.theme === "dark" ||
        (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
      // the ground's violet ramp (dither.js), dim -> bright, plus the track
      const ramp = dark ? ["2b2060", "5a45b8", "b29cfb"] : ["d9d2f3", "9c8ae6", "4d38a8"];
      return { ramp: ramp.map(rgb), track: rgb(hex(cs.getPropertyValue("--text-3"), "8c8c99")) };
    };
    function frame(now) {
      if (!box.isConnected) { box._raf = 0; return; }
      if (!colors || now - (box._toneAt || 0) > 2000) { colors = tone(); box._toneAt = now; }
      if (img) {
        const t = mic.motion ? (now - t0) / 1000 : 1.1;
        // a bright head with a long dissolving tail, and a slower, softer second light; they
        // travel at different speeds so the pattern never repeats exactly
        const p1 = ((t * 0.42) % 1.5) - 0.25, p2 = ((t * 0.23 + 0.6) % 1.8) - 0.4;
        const mid = (H - 1) / 2;
        const d = img.data;
        for (let y = 0; y < H; y++) {
          const vy = 1 - Math.pow(Math.abs(y - mid) / (mid + 0.6), 1.6);   // brighter in the middle rows
          for (let x = 0; x < W; x++) {
            const u = x / (W - 1);
            const du = u - p1;
            const head = du > 0 ? Math.exp(-Math.pow(du / 0.035, 2)) : Math.exp(du / 0.16);   // tail behind
            const soft = 0.5 * Math.exp(-Math.pow((u - p2) / 0.1, 2));
            const shimmer = 0.06 * (1 + Math.sin(u * 22 - t * 4 + y));
            const light = Math.min(1, (head + soft) * vy + shimmer * vy);
            const k = (y * W + x) * 4, th = BAYER[y % 8][x % 8];
            const lvl = Math.floor(light * 3 + th);   // 0..3, ordered-dithered like the ground
            if (lvl > 0) {
              const c = colors.ramp[Math.min(3, lvl) - 1];
              d[k] = c[0]; d[k + 1] = c[1]; d[k + 2] = c[2]; d[k + 3] = 255;
            } else if (y === Math.round(mid) && x % 2 === 0) {
              const c = colors.track; d[k] = c[0]; d[k + 1] = c[1]; d[k + 2] = c[2]; d[k + 3] = 70;   // the track
            } else d[k + 3] = 0;
          }
        }
        g.putImageData(img, 0, 0);
      }
      if (now - lastMeta > 250) { lastMeta = now; writeMeta(); }
      box._raf = mic.motion ? requestAnimationFrame(frame) : 0;
    }
    function writeMeta() {
      const live = v.live || {};
      const start = live.inicio ? live.inicio * 1000 : v.writingSince || (v.writingSince = Date.now());
      const secs = Math.max(0, Math.round((Date.now() - start) / 1000));
      const words = ((live.texto || "").match(/[\p{L}\p{N}]+/gu) || []).length;
      const parts = [mic.fmtClock(secs)];
      if (words) parts.push(mic.t("secretaria.full_words", { n: words }));
      meta.textContent = parts.join("  ·  ");
    }
    box._kick = () => { writeMeta(); if (!box._raf) box._raf = requestAnimationFrame(frame); };
    cv.style.width = W * CELL + "px";
    cv.style.height = H * CELL + "px";
    return box;
  }

  // ------------------------------------------------------------------ the index
  function slug(s, used) {
    let b = s.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "s";
    let k = b, n = 2;
    while (used.has(k)) k = b + "-" + n++;
    used.add(k);
    return k;
  }
  function index(doc) {
    const hs = [...doc.querySelectorAll("h3, h4")];
    if (hs.length < 3) return null;
    const nav = el("nav", "fa-toc");
    nav.setAttribute("aria-label", mic.t("secretaria.full_index"));
    nav.append(el("div", "fa-toc-h caps", mic.t("secretaria.full_index")));
    const used = new Set();
    const links = [];
    for (const h of hs) {
      h.id = "fa-" + slug(h.textContent, used);
      const a = el("button", "fa-toc-i" + (h.tagName === "H4" ? " sub" : ""), h.textContent.replace(/^\d+\.\s*/, ""));
      a.type = "button";
      const num = /^(\d+)\./.exec(h.textContent);
      if (num) a.prepend(el("span", "fa-toc-n tnum", num[1].padStart(2, "0")));
      a.addEventListener("click", () => {
        const sc = scroller(doc);
        if (!sc) return;
        const top = h.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop - 64;
        sc.scrollTo({ top, behavior: mic.motion ? "smooth" : "auto" });
      });
      links.push([h, a]);
      nav.append(a);
    }
    nav._links = links;
    return nav;
  }
  const scroller = (n) => n && n.closest(".transcript");
  function spy(v) {
    const nav = v.node.querySelector(".fa-toc");
    const sc = scroller(v.node);
    if (!nav || !sc) return;
    if (v.spyOff) v.spyOff();
    const bar = v.node.querySelector(".fa-progress > i");
    const onScroll = () => {
      const top = sc.getBoundingClientRect().top + 90;
      let cur = nav._links[0];
      for (const l of nav._links) { if (l[0].getBoundingClientRect().top <= top) cur = l; else break; }
      for (const l of nav._links) l[1].classList.toggle("on", l === cur);
      const docEl = v.node.querySelector(".fa-doc");
      if (bar && docEl) {
        const r = docEl.getBoundingClientRect(), sr = sc.getBoundingClientRect();
        const p = Math.min(1, Math.max(0, (sr.top - r.top + sr.height * 0.5) / Math.max(1, r.height)));
        bar.style.transform = "scaleX(" + p.toFixed(4) + ")";
      }
    };
    sc.addEventListener("scroll", onScroll, { passive: true });
    v.spyOff = () => sc.removeEventListener("scroll", onScroll);
    requestAnimationFrame(onScroll);
  }

  // ------------------------------------------------------------------ view
  function view(folder, call, onClose) {
    let v = views.get(folder);
    if (!v) {
      v = { node: el("section", "fa"), doc: null, live: null, timer: 0, folder, call, onClose, copied: 0, loader: null, painted: "" };
      v.node.setAttribute("aria-label", mic.t("secretaria.full"));
      views.set(folder, v);
      load(v);
    }
    v.call = call;
    v.onClose = onClose;
    paint(v, true);
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
    if (v.live && v.live.fase === "escribiendo") { clearTimeout(v.timer); v.timer = setTimeout(() => load(v), 700); }
  }

  async function make(v) {
    v.live = { fase: "escribiendo", texto: "" };
    v.writingSince = Date.now();
    paint(v);
    const r = await v.call("full_acta_make", { folder: v.folder });
    if (!r || !r.ok) { v.live = { fase: "error", error: (r && r.error) || "failed" }; paint(v); return; }
    clearTimeout(v.timer);
    v.timer = setTimeout(() => load(v), 700);
  }

  function action(icon, label, fn) {
    const b = el("button", "icon-btn fa-act");
    b.type = "button";
    b.innerHTML = mic.icon(icon);
    b.dataset.tip = label;
    b.dataset.tipPos = "below";
    b.setAttribute("aria-label", label);
    b.addEventListener("click", fn);
    return b;
  }

  function readMinutes(words) { return Math.max(1, Math.round(words / 220)); }

  function paint(v, attach) {
    const node = v.node;
    const writing = v.live && v.live.fase === "escribiendo";
    const state = writing ? "writing" : v.live && v.live.fase === "error" ? "error:" + v.live.error
      : v.doc ? "doc:" + (v.doc.md || "").length + ":" + (v.doc.stale ? 1 : 0) + ":" + (v.copied && Date.now() - v.copied < 1600 ? 1 : 0) : "wait";
    // while it is being written only the clock and word count move: keep the same nodes
    if (state === v.painted && node.childElementCount) {
      if (writing && v.loader && v.loader._kick) v.loader._kick();
      if (attach) spy(v);
      return;
    }
    v.painted = state;
    const head = el("div", "fa-head");
    const back = el("button", "fa-back", mic.t("secretaria.acta"));
    back.type = "button";
    back.insertAdjacentHTML("afterbegin", mic.icon("chevron", "mini"));
    back.addEventListener("click", () => v.onClose && v.onClose());
    head.append(back, el("span", "fa-title caps", mic.t("secretaria.full")));
    const meta = el("span", "fa-meta tnum");
    if (!writing && v.doc && v.doc.meta && v.doc.meta.words) {
      meta.append(mic.t("secretaria.full_words", { n: v.doc.meta.words }) + " · " + mic.t("secretaria.full_read", { n: readMinutes(v.doc.meta.words) }));
    }
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
    const progress = el("div", "fa-progress");
    progress.append(el("i"));
    head.append(progress);

    let body;
    if (writing) {
      if (!v.loader) v.loader = loader(v);
      body = el("div", "fa-wrap writing");
      body.append(v.loader);
    } else {
      v.loader = null;
      body = el("div", "fa-wrap");
      if (v.live && v.live.fase === "error") {
        const err = el("div", "fa-doc fa-err");
        err.append(el("p", "", mic.t("secretaria.err." + (v.live.error || "failed"))));
        const retry = el("button", "btn btn-sm", mic.t("secretaria.retry"));
        retry.type = "button";
        retry.addEventListener("click", () => make(v));
        err.append(retry);
        body.append(err);
      } else if (v.doc) {
        const doc = markdown(v.doc.md);
        const toc = index(doc);
        if (toc) { body.classList.add("has-toc"); body.append(toc); }
        body.append(doc);
      } else {
        if (!v.loader) v.loader = loader(v);
        body.classList.add("writing");
        body.append(v.loader);
      }
    }
    node.classList.toggle("is-writing", !!writing);
    node.replaceChildren(head, body);
    if (v.loader) v.loader._kick();
    spy(v);
  }

  function forget(folder) {
    const v = views.get(folder);
    if (v) { clearTimeout(v.timer); if (v.spyOff) v.spyOff(); }
    views.delete(folder);
  }

  window.micnaga = window.micnaga || {};
  window.micnaga.fullActa = { view, forget, markdown };
})();
