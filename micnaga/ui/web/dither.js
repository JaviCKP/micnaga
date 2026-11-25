/*
 * Micnaga — the dithered light. One small module for the surfaces that load it.
 *
 *  1. Ground: the planet limb and its haze from the Bitnaga promo, ordered-dithered with an
 *     8x8 Bayer matrix in 4 px cells. WebGL2 renders ONE pixel per cell into a canvas at 1/4 of
 *     the window and CSS scales it up with image-rendering: pixelated (cells stay exact).
 *     ~12 fps; paused only while the window is hidden or minimized (a window in the WebView2
 *     host often reports no focus while it is in plain view, which froze it); one still frame
 *     with prefers-reduced-motion or data-motion="off". Without WebGL2 the CPU paints the same
 *     light, animated at a lower rate. Mounted into `.ambient` (data-rise / data-cx / data-gain / data-fall
 *     tune the limb per surface). Not mounted inside the embedded Ajustes (the parent paints).
 *  2. Accents: tiny textures generated once per theme and published as CSS custom properties
 *     (--dither-edge: the dissolving edge of a progress fill; --dither-band: the «thinking»
 *     sweep). No per-frame cost.
 *  3. micDither.wipe(mid): the library <-> Ajustes transition. Cells close by Bayer threshold
 *     with a violet edge, `mid()` swaps the view under full cover, cells open again.
 */
(function () {
  "use strict";
  const root = document.documentElement;
  const params = new URLSearchParams(location.search);
  const embedded = params.get("embed") === "1" && window.parent !== window;
  if (embedded) root.classList.add("embedded");      // transparent page: the parent's limb shows
  const CELL = 4;            // px per dither cell (the promo's cell)
  const FPS = 12;
  const reduced = window.matchMedia ? matchMedia("(prefers-reduced-motion: reduce)") : null;
  const darkMq = window.matchMedia ? matchMedia("(prefers-color-scheme: dark)") : null;
  const motionOn = () => root.dataset.motion === "on" || (root.dataset.motion !== "off" && !(reduced && reduced.matches));
  const isDark = () => (root.dataset.theme ? root.dataset.theme === "dark" : !!(darkMq && darkMq.matches));

  // level 1..3 tones; level 0 is the ground (transparent). Dark: the promo's violet ramp.
  const TONES = {
    dark: ["#2b2060", "#5a45b8", "#b29cfb"],
    light: ["#e5e0f5", "#c7bcef", "#8f7ae6"],
  };
  const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));

  // ---- Bayer 8x8 (thresholds in 0..1) -------------------------------------------------------
  const BAY = (() => {
    let m = [[0, 2], [3, 1]];
    while (m.length < 8) {
      const n = m.length;
      m = Array.from({ length: 2 * n }, (_, y) => Array.from({ length: 2 * n }, (_, x) =>
        4 * m[y % n][x % n] + [0, 2, 3, 1][(y >= n) * 2 + (x >= n)]));
    }
    return m.map((r) => r.map((v) => (v + 0.5) / 64));
  })();
  const bayer = (x, y) => BAY[((y % 8) + 8) % 8][((x % 8) + 8) % 8];

  // ---- the light (shared by the shader and the CPU poster) ----------------------------------
  // p: CSS px from the bottom-left corner. Limb = a huge planet whose top sits `rise` px above
  // the bottom edge at `cx` (fraction of the width); its atmosphere falls off over `fall` px.
  function conf() {
    const a = document.querySelector(".ambient");
    const d = (a && a.dataset) || {};
    const num = (v, def) => (v !== undefined && v !== "" && !isNaN(+v) ? +v : def);
    return { rise: num(d.rise, 64), cx: num(d.cx, 0.5), gain: num(d.gain, 0.5), fall: num(d.fall, 70) };
  }

  const GLSL = `#version 300 es
precision highp float;
out vec4 o;
uniform vec2 uSize; uniform float uT, uGain, uRise, uCx, uFall, uCell;
uniform vec3 uC1, uC2, uC3;
float b2(vec2 a){ a = floor(a); return fract(a.x / 2. + a.y * a.y * .75); }
#define b4(a) (b2(.5 * (a)) * .25 + b2(a))
#define b8(a) (b4(.5 * (a)) * .25 + b2(a))
float hs(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float vn(vec2 p){ vec2 i = floor(p), f = fract(p); f = f * f * (3. - 2. * f);
  return mix(mix(hs(i), hs(i + vec2(1, 0)), f.x), mix(hs(i + vec2(0, 1)), hs(i + 1.), f.x), f.y); }
float fbm(vec2 p){ float s = 0., a = .5; for (int i = 0; i < 4; i++) { s += a * vn(p); p *= 2.03; a *= .5; } return s; }
float light(vec2 p){
  float R = max(uSize.x * 1.6, 1400.);
  vec2 c = vec2(uSize.x * uCx, -R + uRise);
  float d = length(p - c) - R;
  float atm = exp(-abs(max(d, 0.)) / uFall) * step(0., d) + .16 * step(d, 0.) * exp(-abs(d) / 40.);
  float haze = pow(fbm(p / 420. + vec2(uT * .03, uT * .011)), 2.2) * smoothstep(uRise + 90., 0., p.y) * .22;
  return (atm + haze) * uGain * (1. + .04 * sin(uT * .37));
}
void main(){
  vec2 q = floor(gl_FragCoord.xy);
  float l = floor(clamp(light((q + .5) * uCell), 0., 1.) * 3. + b8(q));
  o = l < .5 ? vec4(0.) : vec4(l < 1.5 ? uC1 : l < 2.5 ? uC2 : uC3, 1.);
}`;

  // same light on the CPU (poster without WebGL2): a hash/noise pair that matches the shader
  function fract(x) { return x - Math.floor(x); }
  function hs(x, y) { return fract(Math.sin(x * 127.1 + y * 311.7) * 43758.5453); }
  function vn(x, y) {
    const ix = Math.floor(x), iy = Math.floor(y);
    let fx = x - ix, fy = y - iy;
    fx = fx * fx * (3 - 2 * fx); fy = fy * fy * (3 - 2 * fy);
    const a = hs(ix, iy), b = hs(ix + 1, iy), c = hs(ix, iy + 1), d = hs(ix + 1, iy + 1);
    return (a + (b - a) * fx) + ((c + (d - c) * fx) - (a + (b - a) * fx)) * fy;
  }
  function fbm(x, y) { let s = 0, a = 0.5; for (let i = 0; i < 4; i++) { s += a * vn(x, y); x *= 2.03; y *= 2.03; a *= 0.5; } return s; }
  function smooth(e0, e1, x) { const t = Math.min(1, Math.max(0, (x - e0) / (e1 - e0))); return t * t * (3 - 2 * t); }
  function lightCPU(px, py, W, c, t) {
    const R = Math.max(W * 1.6, 1400), cx = W * c.cx, cy = -R + c.rise;
    const d = Math.hypot(px - cx, py - cy) - R;
    const atm = d >= 0 ? Math.exp(-d / c.fall) : 0.16 * Math.exp(d / 40);
    const haze = Math.pow(fbm(px / 420 + t * 0.03, py / 420 + t * 0.011), 2.2) * smooth(c.rise + 90, 0, py) * 0.22;
    return (atm + haze) * c.gain * (1 + 0.04 * Math.sin(t * 0.37));
  }

  // ---- the ground ---------------------------------------------------------------------------
  const ground = { cv: null, gl: null, u: {}, cpu: false, t: 8, last: 0, timer: 0, raf: 0, cw: 0, ch: 0, cfg: null };

  function mountGround() {
    const host = document.querySelector(".ambient");
    if (!host || embedded) return;
    const cv = document.createElement("canvas");
    cv.setAttribute("aria-hidden", "true");
    host.append(cv);
    ground.cv = cv;
    ground.cfg = conf();
    const gl = cv.getContext("webgl2", {
      alpha: true, antialias: false, depth: false, stencil: false, premultipliedAlpha: true,
      preserveDrawingBuffer: false, powerPreference: "low-power",
    });
    if (gl && initGL(gl)) ground.gl = gl;
    else ground.cpu = true;
    size();
    paint();
    requestAnimationFrame(() => cv.classList.add("on"));
    loop();
  }

  function initGL(gl) {
    try {
      const sh = (k, src) => {
        const s = gl.createShader(k);
        gl.shaderSource(s, src); gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || "shader");
        return s;
      };
      const pr = gl.createProgram();
      gl.attachShader(pr, sh(gl.VERTEX_SHADER, "#version 300 es\nin vec2 a; void main(){ gl_Position = vec4(a, 0., 1.); }"));
      gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, GLSL));
      gl.linkProgram(pr);
      if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(pr) || "link");
      gl.useProgram(pr);
      gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      for (const n of ["uSize", "uT", "uGain", "uRise", "uCx", "uFall", "uCell", "uC1", "uC2", "uC3"]) ground.u[n] = gl.getUniformLocation(pr, n);
      ground.cv.addEventListener("webglcontextlost", (e) => { e.preventDefault(); ground.gl = null; ground.cpu = true; paint(); });
      return true;
    } catch (e) {
      console.warn("dither: WebGL2 unavailable, poster instead", e);
      return false;
    }
  }

  function size() {
    if (!ground.cv) return;
    const W = Math.max(1, window.innerWidth), H = Math.max(1, window.innerHeight);
    // only the band where there is light: a smaller layer for the compositor to refresh
    const band = (ground.cfg ? ground.cfg.rise : 64) + 300;
    ground.cw = Math.ceil(W / CELL); ground.ch = Math.ceil(Math.min(H, band) / CELL);
    const cv = ground.cv;
    if (cv.width !== ground.cw || cv.height !== ground.ch) { cv.width = ground.cw; cv.height = ground.ch; }
    cv.style.width = ground.cw * CELL + "px";
    cv.style.height = ground.ch * CELL + "px";
  }

  function paint() {
    if (!ground.cv) return;
    const tones = (isDark() ? TONES.dark : TONES.light).map(rgb);
    // paper needs less light than night for the same presence
    const c = Object.assign({}, ground.cfg, { gain: ground.cfg.gain * (isDark() ? 1 : 0.75) });
    if (ground.gl) {
      const gl = ground.gl, u = ground.u;
      gl.viewport(0, 0, ground.cw, ground.ch);
      gl.uniform2f(u.uSize, ground.cw * CELL, ground.ch * CELL);
      gl.uniform1f(u.uT, ground.t);
      gl.uniform1f(u.uGain, c.gain); gl.uniform1f(u.uRise, c.rise); gl.uniform1f(u.uCx, c.cx); gl.uniform1f(u.uFall, c.fall);
      gl.uniform1f(u.uCell, CELL);
      gl.uniform3f(u.uC1, ...tones[0].map((v) => v / 255));
      gl.uniform3f(u.uC2, ...tones[1].map((v) => v / 255));
      gl.uniform3f(u.uC3, ...tones[2].map((v) => v / 255));
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      return;
    }
    // CPU: the same light, one pixel per cell (animated by the loop at CPU_FPS)
    const g = ground.cv.getContext("2d");
    if (!g) return;
    const w = ground.cw, h = ground.ch, img = g.createImageData(w, h), W = w * CELL;
    for (let y = 0; y < h; y++) {
      const qy = h - 1 - y;                                   // canvas rows go down; the light goes up
      for (let x = 0; x < w; x++) {
        const l = Math.floor(Math.min(1, Math.max(0, lightCPU((x + 0.5) * CELL, (qy + 0.5) * CELL, W, c, ground.t))) * 3 + bayer(x, qy));
        if (l < 1) continue;
        const col = tones[Math.min(3, l) - 1], i = (y * w + x) * 4;
        img.data[i] = col[0]; img.data[i + 1] = col[1]; img.data[i + 2] = col[2]; img.data[i + 3] = 255;
      }
    }
    g.putImageData(img, 0, 0);
  }

  // ---- the loop: ~12 fps (CPU: ~8), only while it can be seen ------------------------------
  const CPU_FPS = 8;
  const visible = () => !document.hidden;
  function loop() {
    if (!(ground.gl || ground.cpu) || ground.timer || ground.raf) return;
    if (!motionOn() || !visible()) { ground.last = 0; return; }
    ground.timer = setTimeout(() => {
      ground.timer = 0;
      ground.raf = requestAnimationFrame((now) => {
        ground.raf = 0;
        if (!motionOn() || !visible()) { ground.last = 0; return; }
        if (ground.last) ground.t += Math.min(0.25, (now - ground.last) / 1000);
        ground.last = now;
        paint();
        loop();
      });
    }, 1000 / (ground.gl ? FPS : CPU_FPS));
  }
  function stop() {
    clearTimeout(ground.timer); ground.timer = 0;
    if (ground.raf) cancelAnimationFrame(ground.raf);
    ground.raf = 0; ground.last = 0;
  }
  function wake() { stop(); if (!motionOn()) { ground.t = 8; paint(); } loop(); }

  // ---- accents: small textures as CSS custom properties -------------------------------------
  function texture(w, h, lit, color) {
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    const g = c.getContext("2d");
    if (!g) return "none";
    g.fillStyle = color;
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) if (lit(x, y)) g.fillRect(x, y, 1, 1);
    return 'url("' + c.toDataURL("image/png") + '")';
  }
  function accents() {
    const accent = getComputedStyle(root).getPropertyValue("--accent").trim() || (isDark() ? "#a78bfa" : "#4d38a8");
    const color = /^#|^rgb/.test(accent) ? accent : (isDark() ? "#a78bfa" : "#4d38a8");
    // progress edge: 12 cells that thin out to nothing (left = solid, right = empty)
    root.style.setProperty("--dither-edge", texture(12, 2, (x, y) => bayer(x, y) < 1 - (x + 0.5) / 12, color));
    // «thinking»: a soft band of cells, densest in the middle
    root.style.setProperty("--dither-band", texture(40, 8, (x, y) => {
      const d = Math.abs((x + 0.5) / 40 - 0.5) * 2;
      return bayer(x, y) < 0.6 * Math.pow(1 - d, 1.6);
    }, color));
  }

  // ---- the wipe ------------------------------------------------------------------------------
  function wipe(mid) {
    if (!motionOn() || typeof requestAnimationFrame !== "function") { mid(); return Promise.resolve(); }
    const C = 8, w = Math.ceil(innerWidth / C), h = Math.ceil(innerHeight / C);
    const cv = document.createElement("canvas");
    cv.className = "dither-wipe";
    cv.width = w; cv.height = h;
    cv.style.width = w * C + "px"; cv.style.height = h * C + "px";
    document.body.append(cv);
    const g = cv.getContext("2d");
    if (!g) { cv.remove(); mid(); return Promise.resolve(); }
    const cs = getComputedStyle(root);
    const bg = rgb(toHex(cs.getPropertyValue("--bg").trim()) || (isDark() ? "#131318" : "#f3f3f0"));
    const ed = rgb(toHex(cs.getPropertyValue("--accent").trim()) || (isDark() ? "#a78bfa" : "#4d38a8"));
    const th = new Float32Array(w * h);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) th[y * w + x] = bayer(x, y) * 0.45 + (x / w) * 0.55;
    const img = g.createImageData(w, h), E = 0.06, D = 190;
    function draw(a, b) {
      const d = img.data;
      for (let i = 0; i < th.length; i++) {
        const t = th[i], k = i * 4;
        if (t < a && t > b) {
          const col = t > a - E || t < b + E ? ed : bg;
          d[k] = col[0]; d[k + 1] = col[1]; d[k + 2] = col[2]; d[k + 3] = 255;
        } else d[k + 3] = 0;
      }
      g.putImageData(img, 0, 0);
    }
    const ease = (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);
    root.classList.add("wiping");
    return new Promise((done) => {
      let t0 = 0, swapped = false;
      const step = (now) => {
        if (!t0) t0 = now;
        const e = now - t0;
        if (e < D) draw(ease(e / D) * (1 + 2 * E) - E, -1);
        else {
          if (!swapped) { swapped = true; try { mid(); } catch (err) { console.warn(err); } }
          const k = Math.min(1, (e - D) / D);
          draw(2, ease(k) * (1 + 2 * E) - E);
          if (k >= 1) { cv.remove(); root.classList.remove("wiping"); done(); return; }
        }
        requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    });
  }
  function toHex(v) {
    if (/^#[0-9a-f]{6}$/i.test(v)) return v;
    const m = v.match(/rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)/i);
    return m ? "#" + [m[1], m[2], m[3]].map((n) => (+n).toString(16).padStart(2, "0")).join("") : "";
  }

  // ---- wiring ---------------------------------------------------------------------------------
  function retheme() { accents(); paint(); }
  new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.attributeName === "data-theme") retheme();
      if (m.attributeName === "data-motion") wake();
    }
  }).observe(root, { attributes: true, attributeFilter: ["data-theme", "data-motion"] });
  document.addEventListener("mic:theme", retheme);
  if (darkMq && darkMq.addEventListener) darkMq.addEventListener("change", retheme);
  if (reduced && reduced.addEventListener) reduced.addEventListener("change", wake);
  document.addEventListener("visibilitychange", wake);
  window.addEventListener("focus", wake);
  // focus moving into the embedded Ajustes blurs this window but the document keeps focus
  window.addEventListener("blur", () => setTimeout(wake, 0));
  let rz = 0;
  window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { size(); paint(); }, 60); });

  /** ms per frame of the ground, GPU included (gl.finish), over n frames: for measuring only */
  function bench(n) {
    if (!ground.gl) return null;
    n = n || 60;
    const t0 = performance.now();
    for (let i = 0; i < n; i++) { ground.t += 1 / FPS; paint(); ground.gl.finish(); }
    return (performance.now() - t0) / n;
  }
  window.micDither = {
    wipe, bench, repaint: retheme,
    get running() { return !!(ground.timer || ground.raf); },
    get mode() { return ground.gl ? "webgl2" : ground.cpu ? "poster" : "none"; },
    get cells() { return [ground.cw, ground.ch]; },
  };

  function boot() { accents(); mountGround(); if (!motionOn()) { ground.t = 8; paint(); } }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
