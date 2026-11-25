/* Micnaga — smooth wheel scrolling. Additive: it only listens to the mouse wheel.

   A notch of the wheel no longer jumps ~100 px at once: the scroller glides to its target with an
   exponential ease (each notch adds to the target, so fast spins accelerate naturally).

   It steps aside for everything else:
   - touchpads and precision devices (small or fractional deltas: they are smooth already),
   - Ctrl (zoom) and horizontal / Shift scrolling,
   - inputs, selects and anything marked [data-native-scroll],
   - any other scroll (scrollbar drag, keyboard, a script setting scrollTop): if the position is
     not where the glide left it, the glide stops at once,
   - animations off (Windows «Efectos de animación», reduced motion, data-motion="off"). */
(function () {
  "use strict";
  const TAU = 95;           // ms: time constant of the ease (higher = softer)
  const motion = () => !window.mic || mic.motion;

  const canScroll = (el, dy) => {
    if (!(el instanceof Element)) return false;
    const st = getComputedStyle(el);
    if (!/(auto|scroll|overlay)/.test(st.overflowY)) return false;
    const max = el.scrollHeight - el.clientHeight;
    if (max < 1) return false;
    const pos = glides.has(el) ? glides.get(el).target : el.scrollTop;
    return dy > 0 ? pos < max - 0.5 : pos > 0.5;
  };
  const scrollerFor = (node, dy) => {
    for (let el = node instanceof Element ? node : node && node.parentElement; el && el !== document.documentElement; el = el.parentElement) {
      if (el.matches("input, textarea, select, [data-native-scroll]")) return null;
      if (canScroll(el, dy)) return el;
    }
    const se = document.scrollingElement;
    return se && se.scrollHeight - se.clientHeight > 1 ? se : null;
  };

  const glides = new Map();   // element -> {target, pos, last, raf}

  function step(el, g, now) {
    g.raf = 0;
    if (Math.abs(el.scrollTop - g.set) > 2) { glides.delete(el); return; }   // someone else scrolled
    const dt = Math.min(64, now - (g.last || now - 16));
    g.last = now;
    const k = 1 - Math.exp(-dt / TAU);
    g.pos += (g.target - g.pos) * k;
    if (Math.abs(g.target - g.pos) < 0.5) g.pos = g.target;
    el.scrollTop = g.pos;
    g.set = el.scrollTop;
    // the browser clamps: if it could not move, there is nowhere left to go
    if (g.pos === g.target || Math.abs(el.scrollTop - g.pos) > 1.5) { glides.delete(el); return; }
    g.raf = requestAnimationFrame((t) => step(el, g, t));
  }

  function onWheel(e) {
    if (e.defaultPrevented || e.ctrlKey || e.shiftKey || !motion()) return;
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
    let dy = e.deltaY;
    if (e.deltaMode === 1) dy *= 40;                  // lines
    else if (e.deltaMode === 2) dy *= innerHeight;    // pages
    else if (Math.abs(dy) < 40) return;              // touchpad: many small deltas, already smooth
    const el = scrollerFor(e.target, dy);
    if (!el) return;
    e.preventDefault();
    const max = el.scrollHeight - el.clientHeight;
    let g = glides.get(el);
    if (!g || Math.abs(el.scrollTop - g.set) > 2) {
      if (g && g.raf) cancelAnimationFrame(g.raf);
      g = { target: el.scrollTop, pos: el.scrollTop, set: el.scrollTop, last: 0, raf: 0 };
      glides.set(el, g);
    }
    g.target = Math.max(0, Math.min(max, g.target + dy));
    if (!g.raf) { g.last = 0; g.raf = requestAnimationFrame((t) => step(el, g, t)); }
  }

  window.addEventListener("wheel", onWheel, { passive: false });
})();
