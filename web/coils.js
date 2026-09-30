// Coil Viewer: live coil signals + pen position for the PTH-660, with recording and replay.
// Pen reports: input report 0x10 on the vendor interface (flags, X, Y 24-bit, pressure 16-bit).
// Coil signals: the frame Wacom's position calculation last received (stage buffer 0x2001B1FC, same in every
// build), read through the read-only RAM window (Tablet.peekRam: SET 0xD4 address, GET 0xD1 256 bytes).
// Frame layout: X block at +0x00, Y block at +0xAA; window start coil at block +0x0C; 10 profile entries of 10 bytes
// at block +0x11, each starting with the signed 16-bit signal strength of one coil.
import { hidSupported, findGranted, requestTablet, Tablet } from "./lib/hid.js";

const STAGE = 0x2001b1fc;
const X_MAX = 44800, Y_MAX = 29600, P_MAX = 8191;
const KEEP_MS = 10000;                       // live history kept in memory
const $ = (id) => document.getElementById(id);
const C = { bg: "#14171d", grid: "#262b35", line: "#343a46", fg: "#e8ebf1", muted: "#8b95a7",
  pen: "#35c2ff", est: "#3ecf8e", coil: "#c07a45", x: "#4f8cff", y: "#3ecf8e", p: "#f2b640" };

let tab = null, polling = false;
const live = { pens: [], coils: [] };        // pens: [t, flags, x, y, p]; coils: {t, xs, ys, ax[10], ay[10]}
let rec = null;                              // { t0, pens, coils } while recording
let replay = null;                           // { pens, coils, dur, t, playing, speed, last }
const cal = { x: newFit(1683, -1190), y: newFit(1683, -1190) };

// ---------------------------------------------------------------- coil maths

// strongest coil (not at the window's ends) and the 3-coil parabola offset around it
function fit3(amps) {
  let k = 1;
  for (let i = 2; i < 9; i++) if (amps[i] > amps[k]) k = i;
  const L = amps[k - 1], P = amps[k], R = amps[k + 1];
  if (P < 200) return null;
  const den = 2 * (2 * P - L - R);
  const d = den > 0 ? Math.max(-0.5, Math.min(0.5, (R - L) / den)) : 0;
  return { k, d, L, P, R };
}

// coil coordinate -> tablet counts, least-squares against the pen reports (running sums, starts from the known pitch)
function newFit(a, b) { return { a, b, n: 0, su: 0, sv: 0, suu: 0, suv: 0 }; }
function fitAdd(f, u, v) {
  f.n++; f.su += u; f.sv += v; f.suu += u * u; f.suv += u * v;
  if (f.n >= 40 && f.n % 10 === 0) {
    const den = f.n * f.suu - f.su * f.su;
    if (den > 1e-6) {
      const a = (f.n * f.suv - f.su * f.sv) / den;
      if (a > 1000 && a < 2500) { f.a = a; f.b = (f.sv - a * f.su) / f.n; }
    }
  }
}
const toCounts = (f, u) => f.a * u + f.b;

function coilEstimate(s) {
  const fx = fit3(s.ax), fy = fit3(s.ay);
  return { fx, fy, ux: fx ? s.xs + fx.k + fx.d : null, uy: fy ? s.ys + fy.k + fy.d : null };
}

// ---------------------------------------------------------------- tablet I/O

function parsePen(dv) {
  // WebHID strips the report id: [0] flags, [1..3] X, [4..6] Y, [7..8] pressure
  if (dv.byteLength < 9) return null;
  const u = (i) => dv.getUint8(i);
  return [performance.now(), u(0), u(1) | (u(2) << 8) | (u(3) << 16), u(4) | (u(5) << 8) | (u(6) << 16), dv.getUint16(7, true)];
}

function onInput(e) {
  if (e.reportId !== 0x10) return;
  const r = parsePen(e.data);
  if (!r) return;
  live.pens.push(r);
  if (rec) rec.pens.push(r);
}

// read health, shown in the status line: successful samples, failed reads, reads that never answered
const health = { ok: 0, err: 0, stuck: 0, last: "" };
const sleepMs = (ms) => new Promise((r) => setTimeout(r, ms));

// Now and then (about 1 in 4000 requests) the tablet doesn't answer a read request at all; Windows gives up after
// 5 s and pen reports are held back meanwhile. So: one request at a time (never pile up new ones behind a stuck one),
// a limited read rate, and a visible note while it waits.
let pending = null;                          // start time of the request in flight
async function peekOne(addr) {
  pending = performance.now();
  try {
    return await tab.peekRam(addr);
  } catch (e) {
    if (performance.now() - pending > 1000) e.stuck = true;
    throw e;
  } finally {
    pending = null;
  }
}

async function pollCoils() {
  if (polling) return;
  polling = true;
  let errors = 0;
  while (polling && tab) {
    try {
      const t0 = performance.now();
      const a = await peekOne(STAGE);
      const b = await peekOne(STAGE + 0xaa);
      const t = performance.now();
      const dva = new DataView(a.buffer, a.byteOffset, a.byteLength), dvb = new DataView(b.buffer, b.byteOffset, b.byteLength);
      const ax = [], ay = [];
      for (let k = 0; k < 10; k++) { ax.push(dva.getInt16(0x11 + 10 * k, true)); ay.push(dvb.getInt16(0x11 + 10 * k, true)); }
      const s = { t, xs: a[0x0c], ys: b[0x0c], ax, ay };
      live.coils.push(s);
      if (rec) rec.coils.push(s);
      calibrate(s, live.pens);
      health.ok++;
      errors = 0;
      const every = 1000 / Number($("rate").value || 50);
      const wait = every - (performance.now() - t0);
      if (wait > 1) await sleepMs(wait);
    } catch (err) {
      // never give up: count it, back off a little longer each time (max 1 s), keep going
      if (err.stuck) health.stuck++; else health.err++;
      health.last = err.message;
      errors++;
      await sleepMs(Math.min(1000, 50 * errors));
    }
    trimLive();
  }
  polling = false;
}

// pair a coil sample with the newest in-range pen report to refine the coil -> counts fit
function calibrate(s, pens) {
  const p = pens[pens.length - 1];
  if (!p || !(p[1] & 0x20) || s.t - p[0] > 30) return;
  const e = coilEstimate(s);
  if (e.ux != null && e.fx.P > 1500) fitAdd(cal.x, e.ux, p[2]);
  if (e.uy != null && e.fy.P > 1500) fitAdd(cal.y, e.uy, p[3]);
}

function trimLive() {
  const cut = performance.now() - KEEP_MS;
  while (live.pens.length && live.pens[0][0] < cut) live.pens.shift();
  while (live.coils.length && live.coils[0].t < cut) live.coils.shift();
}

function setConn(on, text) {
  $("conn-pill").className = `pill ${on ? "on" : "off"}`;
  $("conn-text").textContent = text ?? (on ? "Connected" : "Not connected");
  $("connect").hidden = on;
  $("disconnect").hidden = !on;
  $("rec").disabled = !on;
}

async function connect(dev) {
  if (!dev) return;
  tab = new Tablet(dev);
  await tab.open();
  dev.addEventListener("inputreport", onInput);
  setConn(true);
  pollCoils();
}

async function disconnect() {
  polling = false;
  if (tab) {
    tab.dev.removeEventListener("inputreport", onInput);
    try { await tab.close(); } catch { /* already gone */ }
  }
  tab = null;
  setConn(false);
  if (rec) stopRec();
}

// ---------------------------------------------------------------- recording / replay

function startRec() {
  rec = { t0: performance.now(), pens: [], coils: [] };
  $("rec").classList.add("on");
  $("rec").textContent = "■ Stop";
  $("save").disabled = true;
}
function stopRec() {
  $("rec").classList.remove("on");
  $("rec").textContent = "● Record";
  $("save").disabled = !rec || (!rec.pens.length && !rec.coils.length);
  rec && (rec.stopped = performance.now());
}

function saveRec() {
  if (!rec) return;
  const t0 = rec.t0, r = (v) => Math.round(v * 100) / 100;
  const data = {
    format: "pth660-coils", version: 1, created: new Date().toISOString(),
    note: "pens: [ms, flags, x, y, pressure]; coils: [ms, x window start, y window start, 10 x signals, 10 y signals]",
    pens: rec.pens.map((p) => [r(p[0] - t0), p[1], p[2], p[3], p[4]]),
    coils: rec.coils.map((c) => [r(c.t - t0), c.xs, c.ys, ...c.ax, ...c.ay]),
  };
  const blob = new Blob([JSON.stringify(data)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `pth660_coils_${data.created.replace(/[:.]/g, "-").slice(0, 19)}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function loadRec(file) {
  const data = JSON.parse(await file.text());
  if (data.format !== "pth660-coils") throw new Error("not a coil viewer recording");
  const pens = data.pens.map((p) => [p[0], p[1], p[2], p[3], p[4]]);
  const coils = data.coils.map((c) => ({ t: c[0], xs: c[1], ys: c[2], ax: c.slice(3, 13), ay: c.slice(13, 23) }));
  cal.x = newFit(1683, -1190); cal.y = newFit(1683, -1190);
  let j = 0;
  for (const s of coils) {                   // refit the coil -> counts conversion from the recording itself
    while (j + 1 < pens.length && pens[j + 1][0] <= s.t) j++;
    calibrate(s, pens.slice(0, j + 1));
  }
  const dur = Math.max(pens.length ? pens[pens.length - 1][0] : 0, coils.length ? coils[coils.length - 1].t : 0);
  replay = { pens, coils, dur, t: 0, playing: false, speed: 1, last: 0 };
  $("player").hidden = false;
  $("mode").textContent = `replay · ${file.name}`;
  setPlaying(false);
}

function setPlaying(on) {
  if (!replay) return;
  if (on && replay.t >= replay.dur) replay.t = 0;
  replay.playing = on;
  replay.last = performance.now();
  $("play").textContent = on ? "❚❚ Pause" : "▶ Play";
}

// ---------------------------------------------------------------- view state at a time

function lastAtOrBefore(arr, t, key) {
  let lo = 0, hi = arr.length - 1, ans = -1;
  while (lo <= hi) {
    const m = (lo + hi) >> 1;
    if (key(arr[m]) <= t) { ans = m; lo = m + 1; } else hi = m - 1;
  }
  return ans;
}

function viewAt() {
  const src = replay ?? live;
  const T = replay ? replay.t : performance.now();
  const pi = lastAtOrBefore(src.pens, T, (p) => p[0]);
  const ci = lastAtOrBefore(src.coils, T, (c) => c.t);
  return { src, T, pen: pi >= 0 ? src.pens[pi] : null, pi, coil: ci >= 0 ? src.coils[ci] : null, ci };
}

// ---------------------------------------------------------------- drawing

function sizeCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = Math.round(w * Number(cv.dataset.ar));
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); cv.style.height = `${h}px`;
  }
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { g, w, h };
}

function drawMap(v) {
  const { g, w, h } = sizeCanvas($("map"));
  g.fillStyle = C.bg; g.fillRect(0, 0, w, h);
  const pad = 14, sw = w - 2 * pad, sh = h - 2 * pad - 14;
  const sx = (x) => pad + (x / X_MAX) * sw, sy = (y) => pad + (y / Y_MAX) * sh;
  g.fillStyle = "#1b1f27"; g.strokeStyle = C.line; g.lineWidth = 1;
  g.beginPath(); g.roundRect(pad, pad, sw, sh, 8); g.fill(); g.stroke();
  // all coils (from the fitted conversion)
  g.strokeStyle = C.grid;
  for (let i = -2; i < 40; i++) {
    const x = toCounts(cal.x, i);
    if (x > 0 && x < X_MAX) { g.beginPath(); g.moveTo(sx(x), pad); g.lineTo(sx(x), pad + sh); g.stroke(); }
    const y = toCounts(cal.y, i);
    if (y > 0 && y < Y_MAX) { g.beginPath(); g.moveTo(pad, sy(y)); g.lineTo(pad + sw, sy(y)); g.stroke(); }
  }
  // the coils being read, brightness = signal
  const c = v.coil;
  let est = null;
  if (c) {
    const mx = Math.max(1, ...c.ax), my = Math.max(1, ...c.ay);
    g.lineWidth = 2.5;
    for (let k = 0; k < 10; k++) {
      const ax = Math.max(0, c.ax[k]) / mx, ay = Math.max(0, c.ay[k]) / my;
      const x = toCounts(cal.x, c.xs + k), y = toCounts(cal.y, c.ys + k);
      g.strokeStyle = `rgba(192,122,69,${0.12 + 0.88 * ax})`;
      g.beginPath(); g.moveTo(sx(x), pad); g.lineTo(sx(x), pad + sh); g.stroke();
      g.strokeStyle = `rgba(192,122,69,${0.12 + 0.88 * ay})`;
      g.beginPath(); g.moveTo(pad, sy(y)); g.lineTo(pad + sw, sy(y)); g.stroke();
    }
    const e = coilEstimate(c);
    if (e.ux != null && e.uy != null) est = [toCounts(cal.x, e.ux), toCounts(cal.y, e.uy)];
  }
  // trail + pen
  const pens = v.src.pens;
  g.fillStyle = C.pen;
  for (let i = Math.max(0, v.pi - 400); i <= v.pi; i++) {
    const p = pens[i];
    if (v.T - p[0] > 800 || !(p[1] & 0x20)) continue;
    g.globalAlpha = 0.15 + 0.6 * (1 - (v.T - p[0]) / 800);
    g.beginPath(); g.arc(sx(p[2]), sy(p[3]), 1.6, 0, 7); g.fill();
  }
  g.globalAlpha = 1;
  const p = v.pen;
  if (p && p[1] & 0x20 && v.T - p[0] < 60) {
    const r = 4 + (p[4] / P_MAX) * 10;
    g.fillStyle = C.pen; g.beginPath(); g.arc(sx(p[2]), sy(p[3]), r, 0, 7); g.fill();
  }
  if (est) {
    g.strokeStyle = C.est; g.lineWidth = 2;
    g.beginPath(); g.arc(sx(est[0]), sy(est[1]), 9, 0, 7); g.stroke();
  }
  g.fillStyle = C.muted; g.font = "11px system-ui, sans-serif";
  g.fillText(`coil pitch X ${cal.x.a.toFixed(0)}, Y ${cal.y.a.toFixed(0)} counts${cal.x.n < 40 ? " (default, refining)" : ""}`,
    pad, h - 6);
}

function drawProfile(g, x0, y0, w, h, amps, start, col, name) {
  const mx = Math.max(500, ...amps);
  const bw = w / 10;
  g.fillStyle = C.muted; g.font = "12px system-ui, sans-serif";
  g.fillText(name, x0, y0 - 6);
  g.strokeStyle = C.line; g.beginPath(); g.moveTo(x0, y0 + h); g.lineTo(x0 + w, y0 + h); g.stroke();
  const f = fit3(amps);
  for (let k = 0; k < 10; k++) {
    const v = Math.max(0, amps[k]) / mx;
    g.fillStyle = col;
    g.globalAlpha = f && Math.abs(k - f.k) <= 1 ? 0.95 : 0.4;
    g.fillRect(x0 + k * bw + 3, y0 + h - v * h, bw - 6, v * h);
    g.globalAlpha = 1;
    g.fillStyle = C.muted; g.font = "10px system-ui, sans-serif";
    g.fillText(String(start + k), x0 + k * bw + bw / 2 - 5, y0 + h + 12);
  }
  if (f) {
    const a = (f.L + f.R - 2 * f.P) / 2, b = (f.R - f.L) / 2;
    g.strokeStyle = C.fg; g.setLineDash([4, 4]); g.lineWidth = 1.5; g.beginPath();
    for (let i = 0; i <= 40; i++) {
      const t = -1.4 + 2.8 * i / 40, val = a * t * t + b * t + f.P;
      const X = x0 + (f.k + 0.5 + t) * bw, Y = y0 + h - Math.max(0, val) / mx * h;
      i ? g.lineTo(X, Y) : g.moveTo(X, Y);
    }
    g.stroke(); g.setLineDash([]);
    const px = x0 + (f.k + 0.5 + f.d) * bw;
    g.strokeStyle = C.fg; g.beginPath(); g.moveTo(px, y0); g.lineTo(px, y0 + h); g.stroke();
  }
}

function drawBars(v) {
  const { g, w, h } = sizeCanvas($("bars"));
  g.fillStyle = C.bg; g.fillRect(0, 0, w, h);
  const c = v.coil;
  if (!c) {
    g.fillStyle = C.muted; g.font = "13px system-ui, sans-serif";
    g.fillText(tab ? "waiting for coil data..." : "connect the tablet or load a recording", 16, 30);
    $("coilinfo").textContent = "-";
    return;
  }
  const ph = (h - 96) / 2;
  drawProfile(g, 16, 24, w - 32, ph, c.ax, c.xs, C.x, "X coils (columns)");
  drawProfile(g, 16, 24 + ph + 44, w - 32, ph, c.ay, c.ys, C.y, "Y coils (rows)");
  const e = coilEstimate(c);
  $("coilinfo").textContent = e.fx && e.fy
    ? `peak X coil ${c.xs + e.fx.k} (${e.fx.d >= 0 ? "+" : ""}${e.fx.d.toFixed(2)}), Y coil ${c.ys + e.fy.k} (${e.fy.d >= 0 ? "+" : ""}${e.fy.d.toFixed(2)})`
    : "no pen signal";
}

function drawTime(v) {
  const { g, w, h } = sizeCanvas($("time"));
  g.fillStyle = C.bg; g.fillRect(0, 0, w, h);
  const span = 3000, t1 = v.T, t0 = t1 - span;
  const X = (t) => 10 + ((t - t0) / span) * (w - 20), Yv = (f) => h - 10 - f * (h - 20);
  g.strokeStyle = C.grid;
  for (let s = Math.ceil(t0 / 500) * 500; s <= t1; s += 500) { g.beginPath(); g.moveTo(X(s), 6); g.lineTo(X(s), h - 6); g.stroke(); }
  const pens = v.src.pens;
  const i0 = Math.max(0, lastAtOrBefore(pens, t0, (p) => p[0]));
  const line = (col, get) => {
    g.strokeStyle = col; g.lineWidth = 1.6; g.beginPath();
    let on = false;
    for (let i = i0; i <= v.pi; i++) {
      const p = pens[i];
      if (!(p[1] & 0x20)) { on = false; continue; }
      const px = X(p[0]), py = Yv(get(p));
      on ? g.lineTo(px, py) : g.moveTo(px, py);
      on = true;
    }
    g.stroke();
  };
  line(C.x, (p) => p[2] / X_MAX);
  line(C.y, (p) => p[3] / Y_MAX);
  line(C.p, (p) => p[4] / P_MAX);
  // coil-based positions as dots
  const coils = v.src.coils;
  const c0 = Math.max(0, lastAtOrBefore(coils, t0, (c) => c.t));
  for (let i = c0; i <= v.ci; i++) {
    const e = coilEstimate(coils[i]);
    if (e.ux == null || e.uy == null) continue;
    g.fillStyle = C.fg; g.globalAlpha = 0.6;
    g.fillRect(X(coils[i].t) - 1, Yv(toCounts(cal.x, e.ux) / X_MAX) - 1, 2, 2);
    g.fillRect(X(coils[i].t) - 1, Yv(toCounts(cal.y, e.uy) / Y_MAX) - 1, 2, 2);
    g.globalAlpha = 1;
  }
  g.font = "11px system-ui, sans-serif";
  [["X", C.x], ["Y", C.y], ["pressure", C.p], ["from coils", C.fg]].forEach(([n, col], i) => {
    g.fillStyle = col; g.fillText(n, 14 + i * 80, 16);
  });
}

function rate(arr, key, T) {
  let n = 0;
  for (let i = arr.length - 1; i >= 0 && key(arr[i]) > T - 1000; i--) if (key(arr[i]) <= T) n++;
  return n;
}

function drawStats(v) {
  const pr = rate(v.src.pens, (p) => p[0], v.T), cr = rate(v.src.coils, (c) => c.t, v.T);
  const p = v.pen;
  const pen = p && p[1] & 0x20 ? `x ${p[2]} y ${p[3]} p ${p[4]}${(p[1] >> 1) & 3 ? ` btn ${(p[1] >> 1) & 3}` : ""}` : "pen out of range";
  let r = "";
  if (rec) r = ` · rec ${(((rec.stopped ?? performance.now()) - rec.t0) / 1000).toFixed(1)} s, ${rec.pens.length} reports, ${rec.coils.length} coil samples`;
  const waiting = pending && performance.now() - pending > 300
    ? ` · tablet not answering a read (Windows waits up to 5 s), ${((performance.now() - pending) / 1000).toFixed(1)} s` : "";
  const hl = tab ? ` · coil reads ok ${health.ok}, failed ${health.err}, timed out ${health.stuck}${waiting}` : "";
  $("stats").textContent = `${pen} · ${pr} reports/s · ${cr} coil samples/s${r}${hl}`;
}

function frame() {
  if (replay && replay.playing) {
    const now = performance.now();
    replay.t = Math.min(replay.dur, replay.t + (now - replay.last) * replay.speed);
    replay.last = now;
    if (replay.t >= replay.dur) setPlaying(false);
    $("scrub").value = String(Math.round((replay.t / Math.max(1, replay.dur)) * 1000));
  }
  if (replay) $("ptime").textContent = `${(replay.t / 1000).toFixed(2)} / ${(replay.dur / 1000).toFixed(2)} s`;
  if (!replay) trimLive();
  const v = viewAt();
  drawMap(v); drawBars(v); drawTime(v); drawStats(v);
  requestAnimationFrame(frame);
}

// ---------------------------------------------------------------- wiring

function init() {
  if (!hidSupported()) { $("support").hidden = false; $("connect").disabled = true; }
  $("connect").addEventListener("click", async () => {
    try { await connect(await requestTablet()); } catch (e) { setConn(false, e.message); }
  });
  $("disconnect").addEventListener("click", disconnect);
  $("rec").addEventListener("click", () => (rec && !rec.stopped ? stopRec() : startRec()));
  $("save").addEventListener("click", saveRec);
  $("load").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    try { await loadRec(f); } catch (err) { alert(`Could not load: ${err.message}`); }
    e.target.value = "";
  });
  $("play").addEventListener("click", () => setPlaying(!replay?.playing));
  $("scrub").addEventListener("input", (e) => {
    if (!replay) return;
    replay.t = (Number(e.target.value) / 1000) * replay.dur;
    replay.last = performance.now();
  });
  $("speed").addEventListener("change", (e) => { if (replay) replay.speed = Number(e.target.value); });
  $("live").addEventListener("click", () => {
    replay = null; $("player").hidden = true; $("mode").textContent = "live";
    cal.x = newFit(1683, -1190); cal.y = newFit(1683, -1190);
  });
  if (hidSupported()) {
    navigator.hid.addEventListener("disconnect", (e) => { if (tab && e.device === tab.dev) disconnect(); });
    navigator.hid.addEventListener("connect", (e) => {
      if (!tab && e.device.vendorId === 0x056a && e.device.collections.some((c) => c.usagePage === 0xff0d)) connect(e.device).catch(() => {});
    });
    findGranted().then((d) => d && connect(d)).catch(() => {});
  }
  requestAnimationFrame(frame);
}
init();
