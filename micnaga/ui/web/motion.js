/*
 * Micnaga — the few micro-interactions that need a line of JS. Additive: it only observes the
 * DOM the other scripts build (no API, nothing calls in), so it can be dropped without effect.
 *
 *  - List selection: one highlight glides between rows of the library list (.glide).
 *  - Speaker rename: the chip whose name just changed gets `.renamed` for a beat.
 *  - «Aplicar»: rows that turn «aplicada» get their place in the cascade (--i) for the tick.
 */
(function () {
  "use strict";
  const motion = () => !window.mic || mic.motion;

  // ---- list selection ------------------------------------------------------------------------
  const side = document.getElementById("side");
  if (side && window.MutationObserver) {
    const glide = document.createElement("div");
    glide.className = "glide";
    glide.setAttribute("aria-hidden", "true");
    side.classList.add("has-glide");
    let lastRow = null;
    const place = (animate) => {
      if (!glide.isConnected) side.prepend(glide);
      const row = side.querySelector('.row[aria-current]:not([aria-current="false"])');
      if (!row) { glide.classList.remove("on"); lastRow = null; return; }
      const snap = !animate || !lastRow || !motion();
      if (snap) glide.classList.add("snap");
      glide.style.transform = "translateY(" + row.offsetTop + "px)";
      glide.style.height = row.offsetHeight + "px";
      glide.classList.add("on");
      if (snap) { void glide.offsetWidth; glide.classList.remove("snap"); }
      lastRow = row;
    };
    new MutationObserver((muts) => {
      const rebuilt = muts.some((m) => m.type === "childList" && [...m.removedNodes].some((n) => n !== glide));
      if (rebuilt) lastRow = null;
      place(!rebuilt);
    }).observe(side, { childList: true, attributes: true, subtree: true, attributeFilter: ["aria-current"] });
    window.addEventListener("resize", () => place(false));
  }

  // ---- speaker rename ------------------------------------------------------------------------
  const speakers = document.getElementById("speakers");
  if (speakers && window.MutationObserver) {
    let pending = null;
    const note = (e) => {
      const t = e.target;
      if (t && t.tagName === "INPUT" && t.closest(".spk")) pending = { name: t.value.trim(), at: Date.now() };
    };
    speakers.addEventListener("keydown", (e) => { if (e.key === "Enter") note(e); }, true);
    speakers.addEventListener("focusout", note, true);
    new MutationObserver(() => {
      if (!pending || Date.now() - pending.at > 4000) return;
      const name = [...speakers.querySelectorAll(".spk .name")].find((b) => b.textContent === pending.name);
      if (!name) return;
      pending = null;
      const chip = name.closest(".spk");
      chip.classList.add("renamed");
      setTimeout(() => chip.classList.remove("renamed"), 800);
    }).observe(speakers, { childList: true, subtree: true });
  }

  // ---- «Aplicar»: a cascade, top to bottom ----------------------------------------------------
  // Only the rows that were ticked when «Aplicar» was pressed play the tick (a later re-render
  // shows them still), each one --stagger after the one above.
  const sec = document.getElementById("sec");
  if (sec && window.MutationObserver) {
    let applying = null;
    document.addEventListener("click", (e) => {
      const b = e.target && e.target.closest ? e.target.closest(".sec-apply") : null;
      if (!b) return;
      const ids = [...sec.querySelectorAll(".sec-p")].filter((li) => li.querySelector(".sec-cb:checked")).map((li) => li.dataset.id);
      applying = ids.length ? { ids: new Set(ids), at: Date.now() } : null;
    }, true);
    new MutationObserver(() => {
      if (!applying || Date.now() - applying.at > 20000) return;
      const rows = [...sec.querySelectorAll('.sec-p[data-state="aplicada"]')].filter((li) => applying.ids.has(li.dataset.id));
      if (!rows.length) return;
      applying = null;
      rows.forEach((li, i) => { li.style.setProperty("--i", String(i)); li.classList.add("just-applied"); });
    }).observe(sec, { childList: true, subtree: true });
  }
})();
