/* Jarvis orb - canvas 2D, no libraries (works offline).
 *
 *   const orb = new Orb(canvas); orb.setState("thinking"); orb.setLevel(0.6);
 *
 * States: idle, listening, thinking, speaking, mission, asking, error, halted.
 * Each state sets a colour and how the plasma, particles and HUD rings move;
 * the orb eases between them so changes never jump.
 */
(function () {
  "use strict";

  const STATES = {
    //            colour (rgb)        plasma   spin   swirl  pulse  ripple  spinner  label
    idle:      { c: [58, 214, 255],  amp: .05, spin: .25, swirl: .35, pulse: .03, ripple: 0, spinner: 0, label: "STANDING BY" },
    listening: { c: [120, 235, 255], amp: .10, spin: .45, swirl: .8,  pulse: .06, ripple: 1, spinner: 0, label: "LISTENING" },
    thinking:  { c: [255, 176, 64],  amp: .09, spin: 1.6, swirl: 2.4, pulse: .04, ripple: 0, spinner: 1, label: "PROCESSING" },
    speaking:  { c: [80, 225, 255],  amp: .16, spin: .6,  swirl: 1.0, pulse: .10, ripple: 0, spinner: 0, label: "RESPONDING" },
    mission:   { c: [61, 255, 160],  amp: .12, spin: .9,  swirl: 1.2, pulse: .08, ripple: 1, spinner: 0, label: "ACTION COMPLETE" },
    asking:    { c: [255, 200, 74],  amp: .10, spin: .5,  swirl: .8,  pulse: .07, ripple: 1, spinner: 0, label: "AWAITING APPROVAL" },
    error:     { c: [255, 77, 94],   amp: .14, spin: .3,  swirl: .5,  pulse: .09, ripple: 0, spinner: 0, label: "ERROR" },
    halted:    { c: [255, 50, 70],   amp: .03, spin: .05, swirl: .1,  pulse: .14, ripple: 0, spinner: 0, label: "HALTED" },
  };
  const NUM = ["amp", "spin", "swirl", "pulse", "ripple", "spinner"];
  const TAU = Math.PI * 2;
  const lerp = (a, b, t) => a + (b - a) * t;
  const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
  const ease = (v) => (v < 0.5 ? 4 * v * v * v : 1 - Math.pow(-2 * v + 2, 3) / 2);   // easeInOutCubic

  // ---- MORPH: the particles leave the sphere and form a symbol, then flow back.
  // Each shape is drawn once on a 220x220 offscreen canvas and sampled into points.
  const N_PARTICLES = 560;
  const MORPH_DUR = 0.9;        // s per particle flight
  const MORPH_SWEEP = 0.45;     // s spread of start times -> assembles as a sweeping wave
  const SHAPE_SCALE = 1.22;     // shape half-size in orb radii (inside the dashed ring)
  const FONT = "Bahnschrift, 'Segoe UI', Arial, sans-serif";
  const ring = (g, w) => { g.lineWidth = w; g.beginPath(); g.arc(110, 110, 100, 0, TAU); g.stroke(); };
  const SHAPES = {
    mission: { hold: 3.2, draw(g) {           // check mark in a ring
      ring(g, 7);
      g.lineWidth = 24; g.beginPath(); g.moveTo(54, 116); g.lineTo(94, 154); g.lineTo(168, 72); g.stroke();
    } },
    asking: { hold: 3.2, draw(g) {            // question mark in a ring
      ring(g, 7);
      g.font = `bold 170px ${FONT}`; g.textAlign = "center"; g.textBaseline = "middle"; g.fillText("?", 110, 118);
    } },
    error: { hold: 3.6, draw(g) {             // warning triangle
      g.lineWidth = 10; g.beginPath(); g.moveTo(110, 18); g.lineTo(204, 192); g.lineTo(16, 192); g.closePath(); g.stroke();
      g.lineWidth = 22; g.beginPath(); g.moveTo(110, 82); g.lineTo(110, 138); g.stroke();   // bold "!" bar
      g.beginPath(); g.arc(110, 168, 12, 0, TAU); g.fill();                                // and dot
    } },
    halted: { hold: Infinity, draw(g) {       // stop / no-entry sign, held while halted
      g.lineWidth = 9; g.beginPath();
      for (let i = 0; i < 8; i++) { const a = TAU * (i + 0.5) / 8; g[i ? "lineTo" : "moveTo"](110 + Math.cos(a) * 100, 110 + Math.sin(a) * 100); }
      g.closePath(); g.stroke();
      g.lineWidth = 30; g.beginPath(); g.moveTo(56, 110); g.lineTo(164, 110); g.stroke();  // no-entry bar
    } },
  };
  const shapeCache = {};
  function shapePoints(key, n) {
    if (shapeCache[key]) return shapeCache[key];
    const S = 220, cv = document.createElement("canvas");
    cv.width = cv.height = S;
    const g = cv.getContext("2d");
    g.fillStyle = g.strokeStyle = "#fff"; g.lineCap = g.lineJoin = "round";
    SHAPES[key].draw(g);
    const d = g.getImageData(0, 0, S, S).data, pool = [];
    for (let y = 0; y < S; y += 2) for (let x = 0; x < S; x += 2)
      if (d[(y * S + x) * 4 + 3] > 128) pool.push([x / S * 2 - 1, y / S * 2 - 1]);
    for (let i = pool.length - 1; i > 0; i--) { const j = (Math.random() * (i + 1)) | 0; [pool[i], pool[j]] = [pool[j], pool[i]]; }
    const pts = [];
    for (let i = 0; i < n; i++) {            // cycle (with a little jitter) if the shape has few pixels
      const q = pool[i % Math.max(1, pool.length)] || [0, 0];
      pts.push(i < pool.length ? q : [q[0] + (Math.random() - 0.5) * 0.02, q[1] + (Math.random() - 0.5) * 0.02]);
    }
    pts.sort((a, b) => Math.atan2(a[1], a[0]) - Math.atan2(b[1], b[0]));   // neighbours fly together
    return (shapeCache[key] = pts);
  }

  class Orb {
    constructor(canvas) {
      this.cv = canvas;
      this.ctx = canvas.getContext("2d");
      this.state = "idle";
      this.p = { ...STATES.idle, c: [...STATES.idle.c] };    // current (eased) params
      this.level = 0;          // 0..1 audio / activity level (eased)
      this.levelTarget = 0;
      this.t = 0;
      this.ripples = [];
      this.lastRipple = 0;
      this.flash = 0;          // short burst when a state starts
      this.reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.particles = Array.from({ length: N_PARTICLES }, () => ({
        th: Math.random() * TAU,                       // around the y axis
        ph: Math.acos(2 * Math.random() - 1),          // from the pole
        r: 1.05 + Math.random() * 0.55,                // shell radius (x R)
        sp: 0.15 + Math.random() * 0.5,                // own speed
        s: 0.45 + Math.random() * 1.4,                 // size
        x: 0, y: 0, fx: 0, fy: 0, m: 0, fm: 0,         // position + symbol-ness, current and frozen
      }));
      // morph: particles travel from mFrom to mTo ("sphere" | a SHAPES key | "frozen")
      this.morphOn = !this.reduced;
      this.mFrom = "sphere"; this.mTo = "sphere"; this.mStart = -10; this.mHoldUntil = Infinity;
      this.mAmt = 0;           // 0 = orb, 1 = symbol (eased; dims the core while a symbol shows)
      this._resize = this.resize.bind(this);
      window.addEventListener("resize", this._resize);
      this.resize();
      this._last = performance.now();
      requestAnimationFrame(this.frame.bind(this));
    }

    setState(name, instant = false) {
      if (!STATES[name]) return;
      if (instant) { const tg = STATES[name]; this.p = { ...tg, c: [...tg.c] }; }
      if (name === this.state) return;
      this.state = name;
      this.flash = instant ? 0 : 1;
      if (this.morphOn && SHAPES[name]) {
        this._morphTo(name);
        this.mHoldUntil = this.t + SHAPES[name].hold;
      } else {
        this._morphTo("sphere");
      }
    }
    setMorph(on) {
      this.morphOn = !!on;
      if (!this.morphOn) this._morphTo("sphere");
      else if (SHAPES[this.state]) { this._morphTo(this.state); this.mHoldUntil = this.t + SHAPES[this.state].hold; }
    }
    _morphTo(key) {
      if (key === this.mTo) return;
      for (const q of this.particles) { q.fx = q.x; q.fy = q.y; q.fm = q.m; }   // start from where they are now
      this.mFrom = "frozen"; this.mTo = key; this.mStart = this.t;
    }
    get label() { return STATES[this.state].label; }
    get color() { return this.p.c.map(Math.round); }
    setLevel(v) { this.levelTarget = Math.max(0, Math.min(1, v)); }

    resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const r = this.cv.getBoundingClientRect();
      this.w = Math.max(10, r.width); this.h = Math.max(10, r.height);
      this.cv.width = Math.round(this.w * dpr); this.cv.height = Math.round(this.h * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    frame(now) {
      let dt = Math.min(0.05, (now - this._last) / 1000);
      this._last = now;
      if (this.reduced) dt *= 0.35;
      this.t += dt;

      // ease towards the target state
      const tg = STATES[this.state], k = 1 - Math.pow(0.02, dt);
      for (let i = 0; i < 3; i++) this.p.c[i] = lerp(this.p.c[i], tg.c[i], k);
      for (const n of NUM) this.p[n] = lerp(this.p[n], tg[n], k);
      // speaking without real audio: synthesize a voice-like level
      if (this.state === "speaking" && this.levelTarget === 0) {
        const t = this.t;
        this.level = lerp(this.level, 0.35 + 0.3 * Math.abs(Math.sin(t * 9.1) * Math.sin(t * 3.7 + 1)) + 0.15 * Math.sin(t * 23), 0.3);
      } else {
        this.level = lerp(this.level, this.levelTarget, 0.25);
      }
      this.flash = Math.max(0, this.flash - dt * 1.6);
      if (this.mTo !== "sphere" && this.t > this.mHoldUntil) this._morphTo("sphere");
      if (this.t - this.mStart > MORPH_DUR + MORPH_SWEEP) this.mFrom = this.mTo;          // flight finished
      this.mAmt = lerp(this.mAmt, this.mTo === "sphere" ? 0 : 1, 1 - Math.pow(0.004, dt));
      this.draw(dt);
      requestAnimationFrame(this.frame.bind(this));
    }

    rgba(a, c = this.p.c) { return `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`; }

    draw(dt) {
      const { ctx, w, h, t, p } = this;
      ctx.clearRect(0, 0, w, h);
      const cx = w / 2, cy = h / 2;
      const R = Math.max(40, Math.min(w, h) * 0.19);
      const beat = 1 + p.pulse * Math.sin(t * (this.state === "halted" ? 2.2 : 3.2)) + this.level * 0.08 + this.flash * 0.06;

      ctx.save();
      ctx.globalCompositeOperation = "lighter";

      // --- big soft halo
      let g = ctx.createRadialGradient(cx, cy, R * 0.2, cx, cy, R * 2.6);
      g.addColorStop(0, this.rgba(0.28 + this.level * 0.15 + this.flash * 0.2));
      g.addColorStop(0.45, this.rgba(0.07));
      g.addColorStop(1, this.rgba(0));
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);

      this.drawHud(cx, cy, R, dt);
      this.drawRipples(cx, cy, R, dt);
      const dim = 1 - 0.8 * this.mAmt;              // symbol on -> the core steps back
      ctx.globalAlpha = dim;
      this.drawPlasma(cx, cy, R * beat * (1 - 0.25 * this.mAmt));
      ctx.globalAlpha = 1;
      this.drawParticles(cx, cy, R, beat, dt);
      ctx.globalAlpha = dim;

      // --- core sphere
      const cr = R * 0.58 * beat * (1 - 0.45 * this.mAmt);
      g = ctx.createRadialGradient(cx - cr * 0.25, cy - cr * 0.3, cr * 0.05, cx, cy, cr);
      g.addColorStop(0, "rgba(255,255,255,0.95)");
      g.addColorStop(0.25, this.rgba(0.85));
      g.addColorStop(0.7, this.rgba(0.35));
      g.addColorStop(1, this.rgba(0));
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(cx, cy, cr, 0, TAU); ctx.fill();

      // inner bright pinpoint
      g = ctx.createRadialGradient(cx, cy, 0, cx, cy, cr * 0.35);
      g.addColorStop(0, "rgba(255,255,255,0.9)");
      g.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(cx, cy, cr * 0.35, 0, TAU); ctx.fill();
      ctx.globalAlpha = 1;
      ctx.restore();
    }

    drawPlasma(cx, cy, R) {
      const { ctx, t, p } = this;
      const layers = 5, steps = 120;
      ctx.lineWidth = 1.4;
      for (let L = 0; L < layers; L++) {
        const ph = L * 1.7, base = 0.74 + L * 0.055;
        const amp = p.amp * (1 + this.level * 1.6) * (1 - L * 0.08);
        ctx.beginPath();
        for (let i = 0; i <= steps; i++) {
          const a = (i / steps) * TAU;
          const n = Math.sin(a * 3 + t * p.swirl * 1.3 + ph) * 0.5
                  + Math.sin(a * 5 - t * p.swirl * 0.9 + ph * 2) * 0.3
                  + Math.sin(a * 2 + t * p.swirl * 2.1 + ph * 0.5) * 0.2;
          const r = R * (base + amp * n);
          const x = cx + Math.cos(a + t * p.spin * 0.2 * (L % 2 ? -1 : 1)) * r;
          const y = cy + Math.sin(a + t * p.spin * 0.2 * (L % 2 ? -1 : 1)) * r;
          i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        }
        ctx.closePath();
        ctx.strokeStyle = this.rgba(0.55 - L * 0.07);
        ctx.shadowColor = this.rgba(0.9);
        ctx.shadowBlur = 14;
        ctx.stroke();
      }
      ctx.shadowBlur = 0;
    }

    drawParticles(cx, cy, R, beat, dt) {
      const { ctx, p, t } = this;
      const tilt = 0.45, ct = Math.cos(tilt), st = Math.sin(tilt);
      const n = this.particles.length;
      const fromPts = SHAPES[this.mFrom] ? shapePoints(this.mFrom, n) : null;
      const toPts = SHAPES[this.mTo] ? shapePoints(this.mTo, n) : null;
      const moving = this.mFrom !== this.mTo;
      const el = t - this.mStart;
      const breathe = 1 + 0.025 * Math.sin(t * 2.4);
      for (let i = 0; i < n; i++) {
        const q = this.particles[i];
        q.th += dt * q.sp * (0.4 + p.swirl * 0.6);
        // where it would be on the sphere (orb radii)
        const rr = q.r * beat * (1 + this.level * 0.25);
        const sx = Math.sin(q.ph) * Math.cos(q.th), sy = Math.cos(q.ph), sz = Math.sin(q.ph) * Math.sin(q.th);
        const y2 = sy * ct - sz * st, z2 = sy * st + sz * ct;
        const depth = (z2 + 1) / 2;                                   // 0 back .. 1 front
        const sphX = sx * rr, sphY = y2 * rr * 0.92;
        // where the shapes want it (orb radii), with a soft shimmer
        const sh = 0.012 * Math.sin(t * 3 + i);
        const src = (key, pts) => key === "sphere" ? [sphX, sphY]
          : key === "frozen" ? [q.fx, q.fy]
          : [pts[i][0] * SHAPE_SCALE * breathe + sh, pts[i][1] * SHAPE_SCALE * breathe - sh];
        const B = src(this.mTo, toPts);
        let x = B[0], y = B[1], m = this.mTo === "sphere" ? 0 : 1;
        if (moving) {
          const A = src(this.mFrom, fromPts);
          const k = ease(clamp01((el - (i / n) * MORPH_SWEEP) / MORPH_DUR));
          // arc the flight outwards a little so it reads as a swirl, not a straight slide
          const bow = Math.sin(k * Math.PI) * 0.18;
          x = lerp(A[0], B[0], k) - lerp(A[1], B[1], k) * bow;
          y = lerp(A[1], B[1], k) + lerp(A[0], B[0], k) * bow;
          const mA = this.mFrom === "sphere" ? 0 : this.mFrom === "frozen" ? q.fm : 1;
          m = lerp(mA, this.mTo === "sphere" ? 0 : 1, k);
        }
        q.x = x; q.y = y; q.m = m;
        // sphere particles use depth for alpha/size; symbol particles are crisp and bright
        const alpha = lerp(0.15 + depth * 0.75, 0.95, m);
        const size = lerp(q.s * (0.5 + depth), 0.9 + q.s * 0.55, m);
        ctx.fillStyle = this.rgba(alpha);
        ctx.beginPath(); ctx.arc(cx + x * R, cy + y * R, size, 0, TAU); ctx.fill();
      }
    }

    drawRipples(cx, cy, R, dt) {
      const { ctx, p, t } = this;
      if (p.ripple > 0.5 && t - this.lastRipple > (this.state === "listening" ? 0.55 : 1.1)) {
        this.ripples.push({ r: R * 0.9, a: 0.6 });
        this.lastRipple = t;
      }
      ctx.lineWidth = 1.5;
      this.ripples = this.ripples.filter((rp) => {
        rp.r += dt * R * (0.9 + this.level);
        rp.a -= dt * 0.45;
        if (rp.a <= 0) return false;
        ctx.strokeStyle = this.rgba(rp.a);
        ctx.beginPath(); ctx.arc(cx, cy, rp.r, 0, TAU); ctx.stroke();
        return true;
      });
    }

    drawHud(cx, cy, R, dt) {
      const { ctx, t, p } = this;
      const r1 = R * 1.75, r2 = R * 1.95, r3 = R * 2.2;
      ctx.save();
      ctx.translate(cx, cy);

      // thin base circles
      ctx.lineWidth = 1;
      ctx.strokeStyle = this.rgba(0.18);
      ctx.beginPath(); ctx.arc(0, 0, r1, 0, TAU); ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 0, r3, 0, TAU); ctx.stroke();

      // tick ring
      ctx.save();
      ctx.rotate(t * 0.05 * p.spin);
      for (let i = 0; i < 120; i++) {
        const a = (i / 120) * TAU, long = i % 10 === 0;
        ctx.strokeStyle = this.rgba(long ? 0.6 : 0.22);
        ctx.lineWidth = long ? 1.6 : 1;
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * r2, Math.sin(a) * r2);
        ctx.lineTo(Math.cos(a) * (r2 + (long ? 10 : 5)), Math.sin(a) * (r2 + (long ? 10 : 5)));
        ctx.stroke();
      }
      ctx.restore();

      // dashed ring
      ctx.save();
      ctx.rotate(-t * 0.18 * p.spin);
      ctx.setLineDash([R * 0.08, R * 0.05]);
      ctx.strokeStyle = this.rgba(0.35);
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(0, 0, R * 1.4, 0, TAU); ctx.stroke();
      ctx.restore();

      // three bracket arcs
      ctx.save();
      ctx.rotate(t * 0.32 * p.spin);
      ctx.lineWidth = 3;
      ctx.strokeStyle = this.rgba(0.7);
      ctx.shadowColor = this.rgba(1); ctx.shadowBlur = 10;
      for (let i = 0; i < 3; i++) {
        const a = (i / 3) * TAU;
        ctx.beginPath(); ctx.arc(0, 0, r1 + 6, a, a + 0.55); ctx.stroke();
      }
      ctx.restore();

      // counter-rotating thin arcs
      ctx.save();
      ctx.rotate(-t * 0.5 * p.spin);
      ctx.lineWidth = 1.2;
      ctx.strokeStyle = this.rgba(0.45);
      for (let i = 0; i < 4; i++) {
        const a = (i / 4) * TAU + 0.3;
        ctx.beginPath(); ctx.arc(0, 0, r3 - 8, a, a + 0.9); ctx.stroke();
      }
      ctx.restore();

      // processing spinner: bright sweeping comet arc
      if (p.spinner > 0.02) {
        const a0 = t * 4.2;
        const grad = ctx.createConicGradient ? ctx.createConicGradient(a0 - 1.6, 0, 0) : null;
        if (grad) {
          grad.addColorStop(0, this.rgba(0));
          grad.addColorStop(0.25, this.rgba(0.95 * p.spinner));
          grad.addColorStop(0.26, this.rgba(0));
          grad.addColorStop(1, this.rgba(0));
          ctx.strokeStyle = grad;
        } else {
          ctx.strokeStyle = this.rgba(0.9 * p.spinner);
        }
        ctx.lineWidth = 4;
        ctx.shadowColor = this.rgba(1); ctx.shadowBlur = 16;
        ctx.beginPath(); ctx.arc(0, 0, R * 1.55, a0 - 1.6, a0); ctx.stroke();
        ctx.shadowBlur = 0;
      }

      // crosshair notches
      ctx.strokeStyle = this.rgba(0.5);
      ctx.lineWidth = 1.5;
      for (let i = 0; i < 4; i++) {
        const a = (i / 4) * TAU;
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * (r3 + 6), Math.sin(a) * (r3 + 6));
        ctx.lineTo(Math.cos(a) * (r3 + 22), Math.sin(a) * (r3 + 22));
        ctx.stroke();
      }
      ctx.restore();
    }
  }

  window.Orb = Orb;
  window.ORB_STATES = STATES;
})();
