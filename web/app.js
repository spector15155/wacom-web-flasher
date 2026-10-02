import { hidSupported, requestTablet, findGranted, isVendorInterface, Tablet, versionName } from "./lib/hid.js";
import { parsePkg, sha256Hex, detectSlot, imageVersion, trimImage, hex } from "./lib/pkg.js";
import { reboot } from "./lib/flash.js";
import { backupPackage, installPackage, installBin } from "./lib/tasks.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const kb = (n) => `${Math.round(n / 1024)} KB`;
const logEl = $("log");
function log(msg) {
  logEl.textContent += `[${new Date().toLocaleTimeString()}] ${msg}\n`;
  logEl.scrollTop = logEl.scrollHeight;
}

let tablet = null;
let info = null; // { running, versions: {a, b} }
let firmwareList = [];
let busy = false;
let backedUp = null; // file name of a backup made in this session
let consent = false; // own-risk acknowledgement, asked once per session
// after a manual disconnect: no auto-connect (page load, replug) until "Connect tablet" is clicked
const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* storage blocked */ } },
};
let manualOff = store.get("pth660.manualDisconnect") === "1";

// ------------------------------------------------------------ names

function fwForVersion(v) {
  if (v == null || Number.isNaN(v)) return null;
  // compare as numbers: the manifest writes "0x0245", and upper-casing it ("0X0245") never matched
  return firmwareList.find((f) => f.images.some((i) => parseInt(i.version, 16) === v)) ?? null;
}
// Firmware this flasher knows: Wacom stock v1.51 / v1.52 and every build in the manifest. A tablet with anything
// else in a slot (e.g. a newer Wacom update) is untested: warn, keep its original firmware as the fallback, and
// require a backup plus an extra acknowledgement before writing.
function knownVersion(v) {
  return v != null && !Number.isNaN(v) && fwForVersion(v) != null;
}
function unknownSlots() {
  return info ? ["a", "b"].filter((s) => !knownVersion(info.versions[s])) : [];
}
function unknownNote() {
  const u = unknownSlots();
  return u.map((s) => `slot ${s.toUpperCase()} (${esc(versionName(info.versions[s]))})`).join(" and ");
}
function verLabel(v) {
  const fw = fwForVersion(v);
  return fw ? `${versionName(v)} · ${fw.title.replace(/^v[\d.]+\s*/, "").replace(/^\((.*)\)$/, "$1")}` : versionName(v);
}

// ------------------------------------------------------------ connection

let attaching = null;
let waiters = []; // resolved with the Tablet on the next successful attach (after a reboot)
function attach(device) {
  if (attaching) return attaching;
  attaching = doAttach(device).finally(() => (attaching = null));
  return attaching;
}

async function doAttach(device) {
  if (!device) return log("no tablet selected");
  if (tablet && tablet.dev === device && device.opened) return;
  const t = new Tablet(device, log);
  try {
    await t.open();
  } catch (e) {
    return log(`open failed: ${e.name}: ${e.message}`);
  }
  tablet = t;
  log(`connected: ${device.productName}`);
  for (let i = 0; i < 6; i++) {
    if (await refresh()) break;
    await new Promise((r) => setTimeout(r, 1000));
  }
  const w = waiters;
  waiters = [];
  w.forEach((f) => f(t));
}

function waitForTablet(ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      waiters = waiters.filter((f) => f !== done);
      reject(new Error("the tablet did not come back within 90 s. Unplug and replug USB; the page reconnects by itself"));
    }, ms);
    const done = (t) => {
      clearTimeout(timer);
      resolve(t);
    };
    waiters.push(done);
  });
}

async function rebootAndReconnect() {
  const back = waitForTablet(90000);
  log("rebooting the tablet");
  await reboot(tablet, log);
  const t = await back;
  await new Promise((r) => setTimeout(r, 500));
  return t;
}

async function refresh() {
  if (!tablet) return false;
  try {
    const st = await tablet.status();
    const running = await tablet.runningSlot();
    const versions = await tablet.slotVersions();
    info = { running, versions, idle: st.ok };
    render();
    log(`running slot ${running?.toUpperCase()}, slot A ${versionName(versions.a)}, slot B ${versionName(versions.b)}, status 0x${hex(st.s1, 2)}`);
    const unk = unknownSlots();
    if (firmwareList.length && unk.length)
      log(`note: ${unk.map((x) => `slot ${x.toUpperCase()} ${versionName(versions[x])}`).join(", ")} is firmware this flasher ` +
          `hasn't been tested with (untested tablet: backup required, both-slot install disabled)`);
    return true;
  } catch (e) {
    log(`status failed: ${e.name}: ${e.message}`);
    return false;
  }
}

function render() {
  const connected = !!(tablet && info);
  const pill = $("conn-pill");
  pill.className = `pill ${busy ? "busy" : connected ? "on" : "off"}`;
  $("conn-text").textContent = busy ? "Working..." : connected ? `Connected · ${versionName(info.versions[info.running])}` : "Not connected";
  $("connect").hidden = connected;
  $("disconnect").hidden = !connected;
  $("disconnect").disabled = busy;
  $("disconnect").title = busy ? "Can't disconnect while the tablet is being written" : "Close the connection to the tablet";
  $("refresh").disabled = !connected || busy;
  for (const s of ["a", "b"]) {
    const el = $(`slot-${s}`);
    el.classList.toggle("running", connected && info.running === s);
    el.querySelector(".slot-ver").textContent = connected ? versionName(info.versions[s]) : "-";
    const fw = connected ? fwForVersion(info.versions[s]) : null;
    el.querySelector(".slot-sub").textContent = !connected ? "" : fw ? fw.title : info.versions[s] == null ? "no firmware found" : "custom or older build";
  }
  $("device-hint").hidden = connected;
  document.querySelectorAll(".action").forEach((b) => (b.disabled = !connected || busy));
}

function clearDevice() {
  tablet = null;
  info = null;
  render();
}

// ------------------------------------------------------------ firmware list

async function loadFirmware() {
  try {
    firmwareList = (await (await fetch("firmware/manifest.json")).json()).firmware;
  } catch (e) {
    log(`manifest failed: ${e.message}`);
  }
}

async function fetchPkg(fw) {
  const buf = new Uint8Array(await (await fetch(`firmware/${fw.file}`)).arrayBuffer());
  if ((await sha256Hex(buf)) !== fw.sha256) throw new Error("the package download is corrupt (sha256 mismatch)");
  return parsePkg(buf);
}

// ------------------------------------------------------------ wizard

let wz = null; // { kind, step, ...state }

function openWizard(kind) {
  if (busy) return;
  wz = { kind, step: "choose" };
  document.querySelectorAll(".action").forEach((b) => b.classList.toggle("active", b.dataset.action === kind));
  $("wizard").hidden = false;
  $("wz-title").textContent = { install: "Install firmware", backup: "Back up tablet", restore: "Restore from file" }[kind];
  showStep();
  $("wizard").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeWizard() {
  if (busy) return;
  wz = null;
  $("wizard").hidden = true;
  document.querySelectorAll(".action").forEach((b) => b.classList.remove("active"));
}

function setStepper(step) {
  const order = ["choose", "review", "run", "done"];
  const cur = order.indexOf(step);
  document.querySelectorAll("#stepper li").forEach((li) => {
    const i = order.indexOf(li.dataset.step);
    li.className = i < cur ? "done" : i === cur ? "cur" : "";
  });
}

function foot(buttons) {
  const f = $("wz-foot");
  f.innerHTML = "";
  for (const b of buttons) {
    if (b === "spacer") {
      f.appendChild(Object.assign(document.createElement("span"), { className: "spacer" }));
      continue;
    }
    const el = document.createElement("button");
    el.className = `btn ${b.cls ?? ""}`;
    el.textContent = b.label;
    el.disabled = !!b.disabled;
    el.onclick = b.onClick;
    if (b.id) el.id = b.id;
    f.appendChild(el);
  }
}

function showStep() {
  setStepper(wz.step);
  $("wz-close").disabled = busy;
  ({ choose: chooseStep, review: reviewStep, run: () => {}, done: () => {} })[wz.step]();
}

// ---- choose

// the three ways the builds get more reports (tag tooltips; the About section explains them too)
const METHOD_INFO = {
  "factory": "Wacom's original firmware.",
  "more Wacom runs": "Same measurements as stock; Wacom's own position calculation runs several times per measurement, so the cursor moves in smaller steps.",
  "even output": "Same measurements as v2.45; our output places reports at exact 1 ms (or 0.5 ms) intervals on the path between measured positions, no prediction.",
  "two measurements": "The pen-data part of each scan also locates the pen: two real positions per scan instead of one.",
};

function chooseStep() {
  const body = $("wz-body");
  if (wz.kind === "install") {
    const running = info.running;
    const option = (fw, i) => {
      const tag = fw.recommended ? '<span class="tag rec">recommended</span>'
        : fw.kind === "experimental" ? '<span class="tag exp">experimental</span>'
        : fw.kind === "stock" ? '<span class="tag">factory</span>' : "";
      const installed = ["a", "b"].filter((s) => fw.images.some((im) => parseInt(im.version, 16) === info.versions[s]));
      const where = installed.length ? `<span class="tag">in slot ${installed.map((s) => s.toUpperCase()).join(" + ")}</span>` : "";
      const checked = (wz.fw ? wz.fw === fw : fw === firmwareList.find((f) => f.recommended)) ? "checked" : "";
      const lag = fw.lag ? `<span class="tag" title="Estimated average delay from pen movement to the USB report (PC not included)">lag ${esc(fw.lag)}</span>` : "";
      const method = fw.method ? `<span class="tag method" title="${esc(METHOD_INFO[fw.method] || "")}">${esc(fw.method)}</span>` : "";
      return `<label class="opt"><input type="radio" name="fw" value="${i}" ${checked}>
        <span class="t">${esc(fw.title)}${tag}${method}${lag}${where}</span>
        <span class="d short">${esc(fw.short || fw.summary)}</span>
        ${fw.short ? `<details class="d how"><summary>How it works</summary>
          ${(fw.howto || []).map((h) => `<img class="how-img" src="howto/${esc(h)}.svg" alt="${esc(HOWTO_ALT[h] || "")}"
            data-howto="${esc(h)}" title="Open in the pictures above" loading="lazy">`).join("")}
          <p>${esc(fw.summary)}</p></details>` : ""}</label>`;
    };
    const current = firmwareList.map((fw, i) => [fw, i]).filter(([fw]) => !fw.older);
    const older = firmwareList.map((fw, i) => [fw, i]).filter(([fw]) => fw.older);
    const olderOpen = older.some(([fw]) => wz.fw === fw) ? "open" : "";
    body.innerHTML = `<div class="choice">${current.map(([fw, i]) => option(fw, i)).join("")}</div>
    ${older.length ? `<details class="older" ${olderOpen}><summary>Older builds (replaced by newer ones, kept for reference)</summary>
      <div class="choice">${older.map(([fw, i]) => option(fw, i)).join("")}</div></details>` : ""}
    <p class="note info">Lag = estimated average delay from pen movement to the tablet's USB report (the PC adds its own).
      v3.78 and v3.85 measure the pen ~2 x 201 times per second, v4.53 ~2 x 240 (shorter scan loop), all other builds
      ~201; higher report rates are
      positions between measured ones (no prediction).<br>
      Three methods: <b>more Wacom runs</b> (Wacom's calculation runs several times per measurement: smaller cursor
      steps), <b>even output</b> (reports at exact 1 ms / 0.5 ms intervals between measured positions) and
      <b>two measurements</b> (the pen is located twice per scan: more real data).</p>
    ${unknownSlots().length ? `<div class="note warn">This tablet has firmware the flasher hasn't been tested with:
      ${unknownNote()}. The builds here were made and tested on one PTH-660 running Wacom v1.51 / v1.52; they should
      work on others (your serial, geometry and calibration live outside the firmware slots and are never written),
      but that isn't confirmed yet. To keep your original firmware as a fallback, only one slot is installed.</div>` : ""}
    <label class="check"><input type="checkbox" id="opt-both" ${wz.both && !unknownSlots().length ? "checked" : ""}
      ${unknownSlots().length ? "disabled" : ""}>
      <span>Install on <b>both slots</b>. Otherwise only slot ${running === "a" ? "B" : "A"} is replaced and your current
      firmware (slot ${running.toUpperCase()}, ${esc(versionName(info.versions[running]))}) stays as a fallback.</span></label>`;
    foot([{ label: "Next", cls: "primary", onClick: () => {
      wz.fw = firmwareList[Number(body.querySelector("input[name=fw]:checked").value)];
      wz.both = $("opt-both").checked && !unknownSlots().length;
      wz.step = "review";
      showStep();
    } }]);
  } else if (wz.kind === "backup") {
    body.innerHTML = `<p>Reads the firmware from <b>both slots</b> and saves it as one <code>.pkg</code> file you can restore
      later with <i>Restore from file</i>. Nothing is written to the tablet.</p>
      <dl class="plan">
        <div><dt>Slot A</dt><dd>${esc(verLabel(info.versions.a))}${info.running === "a" ? " (running)" : ""}</dd></div>
        <div><dt>Slot B</dt><dd>${esc(verLabel(info.versions.b))}${info.running === "b" ? " (running)" : ""}</dd></div>
        <div><dt>Takes</dt><dd>a few seconds</dd></div>
      </dl>`;
    foot([{ label: "Back up now", cls: "primary", onClick: () => run(async (ctx) => {
      const r = await backupPackage(ctx);
      download(r.bytes, r.fileName);
      backedUp = r.fileName;
      return { backup: r };
    }) }]);
  } else {
    body.innerHTML = `<label class="drop" id="drop"><input type="file" id="file" accept=".pkg,.bin" hidden>
      <div><b>Choose a file</b> or drop it here</div><div>A backup or firmware package (.pkg), or a single image (.bin)</div></label>
      <div id="fileinfo"></div>`;
    const drop = $("drop");
    const pick = (f) => f && loadFile(f);
    $("file").onchange = (e) => pick(e.target.files[0]);
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
    drop.ondragleave = () => drop.classList.remove("over");
    drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); pick(e.dataTransfer.files[0]); };
    if (wz.file) showFileInfo();
    else foot([{ label: "Next", cls: "primary", disabled: true }]);
  }
}

async function loadFile(f) {
  const bytes = new Uint8Array(await f.arrayBuffer());
  wz.file = { name: f.name, size: bytes.length };
  wz.fileErr = null;
  wz.pkg = null;
  wz.bin = null;
  try {
    if (new TextDecoder().decode(bytes.subarray(0, 9)) === "PTH660PKG") {
      wz.pkg = await parsePkg(bytes);
    } else {
      const slot = detectSlot(bytes);
      if (!slot) throw new Error("this is not a PTH-660 firmware image (no valid vector table for slot A or B)");
      const version = imageVersion(bytes);
      if (version == null) throw new Error("no version block (056A:0357) in this image");
      wz.bin = { data: bytes, slot, version };
    }
  } catch (e) {
    wz.fileErr = e.message;
  }
  showFileInfo();
}

function showFileInfo() {
  const el = $("fileinfo");
  const f = wz.file;
  let html = `<div class="note info"><b>${esc(f.name)}</b> · ${kb(f.size)}</div>`;
  if (wz.fileErr) {
    html += `<div class="note bad">${esc(wz.fileErr)}</div>`;
  } else if (wz.pkg) {
    const slots = Object.keys(wz.pkg.images).sort();
    html += `<dl class="plan">${slots.map((s) => `<div><dt>Slot ${s.toUpperCase()} image</dt><dd>${esc(verLabel(parseInt(wz.pkg.images[s].entry.version, 16)))}
      <span class="tag">${kb(wz.pkg.images[s].data.length)}</span></dd></div>`).join("")}
      <div><dt>Package</dt><dd>${esc(wz.pkg.manifest.name)}</dd></div></dl>`;
    if (slots.length === 2) {
      html += `<label class="check"><input type="checkbox" id="opt-both" ${wz.both ? "checked" : ""}>
        <span>Restore <b>both slots</b> (a full restore of a backup). Otherwise only the inactive slot is written.</span></label>`;
    }
  } else if (wz.bin) {
    const needSwitch = wz.bin.slot === info.running;
    html += `<dl class="plan"><div><dt>Image for</dt><dd>slot ${wz.bin.slot.toUpperCase()}</dd></div>
      <div><dt>Version</dt><dd>${esc(verLabel(wz.bin.version))}</dd></div></dl>`;
    if (needSwitch) {
      html += `<div class="note warn">Slot ${wz.bin.slot.toUpperCase()} is the one running now, so it can't be written directly.
        The flasher will first switch the tablet to slot ${wz.bin.slot === "a" ? "B" : "A"} by re-installing what's
        already there (${esc(versionName(info.versions[wz.bin.slot === "a" ? "b" : "a"]))}), then write this image.</div>`;
    }
  }
  el.innerHTML = html;
  foot([{ label: "Next", cls: "primary", disabled: !!wz.fileErr || !(wz.pkg || wz.bin), onClick: () => {
    wz.both = !!$("opt-both")?.checked;
    wz.step = "review";
    showStep();
  } }]);
}

// ---- review

function plannedSteps() {
  const pre = wz.backupFirst ? ["Read slot A", "Read slot B", "Save backup file"] : [];
  return [...pre, ...installSteps()];
}

function installSteps() {
  const r = info.running;
  const o = r === "a" ? "b" : "a";
  const U = (s) => s.toUpperCase();
  if (wz.kind === "install" || wz.pkg) {
    const steps = [`Write slot ${U(o)}, verify, commit`, `Reboot into slot ${U(o)}`];
    if (wz.both) steps.push(`Write slot ${U(r)}, verify, commit`, `Reboot into slot ${U(r)}`);
    return steps;
  }
  const s = wz.bin.slot;
  const steps = [];
  if (s === r) steps.push(`Read slot ${U(o)}`, `Re-install slot ${U(o)} to switch slots`, `Reboot into slot ${U(o)}`);
  steps.push(`Write slot ${U(s)}, verify, commit`, `Reboot into slot ${U(s)}`);
  return steps;
}

function newVersions() {
  const v = { ...info.versions };
  const r = info.running;
  const o = r === "a" ? "b" : "a";
  if (wz.kind === "install") {
    const byslot = Object.fromEntries(wz.fw.images.map((im) => [im.slot, parseInt(im.version, 16)]));
    v[o] = byslot[o];
    if (wz.both) v[r] = byslot[r];
  } else if (wz.pkg) {
    v[o] = parseInt(wz.pkg.images[o]?.entry.version, 16);
    if (wz.both) v[r] = parseInt(wz.pkg.images[r]?.entry.version, 16);
  } else {
    v[wz.bin.slot] = wz.bin.version;
  }
  return v;
}

let untestedAck = false; // acknowledgement for a tablet with firmware the flasher doesn't know, once per session

function reviewStep() {
  if (wz.backupFirst === undefined) wz.backupFirst = !backedUp;
  const untested = wz.kind === "install" && unknownSlots().length > 0;
  if (untested && !backedUp) wz.backupFirst = true;             // untested tablet: backup is not optional
  const nv = newVersions();
  const steps = plannedSteps();
  const reboots = steps.filter((s) => s.startsWith("Reboot")).length;
  const what = wz.kind === "install" ? esc(wz.fw.title) : `${esc(wz.file.name)}`;
  const row = (s) => {
    const changed = nv[s] !== info.versions[s];
    return `<div><dt>Slot ${s.toUpperCase()}</dt><dd>${esc(versionName(info.versions[s]))}
      ${changed ? `&rarr; <b>${esc(verLabel(nv[s]))}</b>` : "<span class=\"tag\">unchanged</span>"}</dd></div>`;
  };
  const missing = ["a", "b"].some((s) => nv[s] !== info.versions[s] && Number.isNaN(nv[s]));
  $("wz-body").innerHTML = `<dl class="plan">
      <div><dt>Firmware</dt><dd>${what}</dd></div>${row("a")}${row("b")}
      <div><dt>Steps</dt><dd>${steps.map((s, i) => `${i + 1}. ${esc(s)}`).join("<br>")}</dd></div>
      <div><dt>Takes</dt><dd>about ${15 + 15 * reboots + (wz.backupFirst ? 5 : 0)} s, ${reboots} reboot${reboots === 1 ? "" : "s"} (the tablet disconnects ~10 s each)</dd></div>
    </dl>
    ${missing ? '<div class="note bad">The package has no image for a slot this needs.</div>' : ""}
    <div class="note info">Every write is read back and compared before the slot is made bootable. If anything
      fails, the tablet keeps booting the firmware it has now, and you can simply run this again.</div>
    <div class="backup-opt">
      <label class="check"><input type="checkbox" id="opt-backup" ${wz.backupFirst ? "checked" : ""}
        ${untested && !backedUp ? "disabled" : ""}>
        <span><b>Save a backup of both slots first</b> (recommended). Downloads a <code>.pkg</code> with the firmware that's on
        the tablet now, before anything is written; restore it any time with <i>Restore from file</i>. Adds a few seconds.</span></label>
      ${backedUp ? `<div class="note ok">Backup already saved in this session: <code>${esc(backedUp)}</code></div>` : ""}
    </div>
    <div class="note warn">Keep the tablet plugged in and this tab open until it says Done.</div>
    ${untested ? `<label class="check consent"><input type="checkbox" id="opt-untested" ${untestedAck ? "checked" : ""}>
      <span>This tablet runs firmware the flasher hasn't been tested with (${unknownNote()}). I understand the new
      firmware hasn't been confirmed on it yet, and that the backup saved first is how I get my original firmware
      back.</span></label>` : ""}
    <label class="check consent"><input type="checkbox" id="opt-consent" ${consent ? "checked" : ""}>
      <span>I understand this is <b>unofficial firmware</b> and that I'm flashing <b>at my own risk</b>. The author
      isn't responsible if something goes wrong with my tablet. <a href="#about" class="about-open">Read more</a></span></label>`;
  document.querySelectorAll(".about-open").forEach((a) => (a.onclick = openAbout));
  const goOk = () => consent && !missing && (!untested || untestedAck);
  $("opt-consent").onchange = (e) => {
    consent = e.target.checked;
    $("go").disabled = !goOk();
  };
  if (untested) $("opt-untested").onchange = (e) => {
    untestedAck = e.target.checked;
    $("go").disabled = !goOk();
  };
  $("opt-backup").onchange = (e) => {
    wz.backupFirst = e.target.checked;
    reviewStep();
  };
  const job = (dryRun) => async (ctx) => {
    let backup = null;
    if (wz.backupFirst && !dryRun) {
      backup = await backupPackage(ctx);
      download(backup.bytes, backup.fileName);
      ctx.log(`backup saved as ${backup.fileName}`);
      backedUp = backup.fileName;
      wz.lastBackup = backup; // kept for the Done screen, also when a later step fails
      wz.backupFirst = false;
    }
    if (wz.kind === "install") {
      ctx.step("Download and check package", wz.fw.file);
      const pkg = await fetchPkg(wz.fw);
      await installPackage(ctx, pkg, { both: wz.both, dryRun });
    } else if (wz.pkg) {
      await installPackage(ctx, wz.pkg, { both: wz.both, dryRun });
    } else {
      await installBin(ctx, wz.bin.data, { dryRun });
    }
    return { dryRun, savedBackup: backup };
  };
  foot([
    { label: "Back", cls: "ghost", onClick: () => { wz.step = "choose"; showStep(); } },
    "spacer",
    { label: "Dry run", cls: "ghost", onClick: () => run(job(true)) },
    { label: wz.kind === "install" ? "Install" : "Restore", cls: "danger", id: "go", disabled: !goOk(),
      onClick: () => run(job(false)) },
  ]);
}

// ---- run + done

function guardUnload(e) {
  e.preventDefault();
  e.returnValue = "";
}

const PHASES = {
  read: ["Reading", 0, 100], erase: ["Erasing", 0, 10], program: ["Writing", 10, 80],
  verify: ["Verifying", 80, 97], sums: ["Checksums", 97, 99], commit: ["Committing", 99, 100],
};

async function run(job) {
  if (busy) return;
  busy = true;
  wz.step = "run";
  setStepper("run");
  $("wz-close").disabled = true;
  render();
  $("wz-body").innerHTML = `<ul class="tasks" id="tasks"></ul>
    <div class="bar"><i id="bar"></i></div><div class="phase" id="phase">starting...</div>`;
  foot([]);
  const tasks = $("tasks");
  const t0 = performance.now();
  let cur = null;
  let curStart = 0;
  const timer = setInterval(() => {
    if (cur) cur.querySelector(".tm").textContent = `${((performance.now() - curStart) / 1000).toFixed(0)} s`;
  }, 500);
  const finishCur = (cls) => {
    if (!cur) return;
    cur.className = cls;
    cur.querySelector(".tm").textContent = `${((performance.now() - curStart) / 1000).toFixed(1)} s`;
  };
  const ctx = {
    tablet: () => tablet,
    rebootAndReconnect,
    log,
    step: (title, detail = "") => {
      finishCur("ok");
      cur = document.createElement("li");
      cur.className = "cur";
      cur.innerHTML = `<span class="ic"></span><span><div class="tt">${esc(title)}</div><div class="td">${esc(detail)}</div></span><span class="tm"></span>`;
      tasks.appendChild(cur);
      curStart = performance.now();
      $("bar").style.width = "0%";
      $("phase").textContent = title.startsWith("Reboot") ? "waiting for the tablet to come back..." : "";
      log(`step: ${title}${detail ? ` (${detail})` : ""}`);
    },
    progress: (phase, done, total) => {
      const [name, a, b] = PHASES[phase] ?? [phase, 0, 100];
      $("bar").style.width = `${a + ((b - a) * done) / total}%`;
      $("phase").textContent = `${name} ${done} / ${total}`;
    },
  };
  window.addEventListener("beforeunload", guardUnload);
  let result = null;
  let error = null;
  try {
    const work = () => job(ctx);
    result = navigator.locks ? await navigator.locks.request("pth660-flash", work) : await work();
    finishCur("ok");
  } catch (e) {
    error = e;
    finishCur("fail");
    log(`FAILED: ${e.message}`);
  } finally {
    clearInterval(timer);
    window.removeEventListener("beforeunload", guardUnload);
    busy = false;
    if (tablet) await refresh();
    render();
  }
  doneStep(result, error, ((performance.now() - t0) / 1000).toFixed(0));
}

function doneStep(result, error, secs) {
  wz.step = "done";
  setStepper(error ? "run" : "done");
  $("wz-close").disabled = false;
  const tasksHtml = $("tasks")?.outerHTML ?? "";
  let msg;
  if (error) {
    msg = `<div class="note bad"><b>Stopped:</b> ${esc(error.message)}</div>
      <div class="note info">The slot the tablet was running was not touched. If a write stopped halfway, that slot
      simply isn't bootable yet: run the same action again. If the tablet stopped answering entirely, unplug it and hold
      the power button until the lights go out, then reconnect.</div>`;
  } else if (result?.dryRun) {
    msg = `<div class="note ok"><b>Dry run OK.</b> Every check passed and the full write plan was built. Nothing was written.</div>`;
  } else if (result?.backup) {
    const b = result.backup;
    msg = `<div class="note ok"><b>Backup saved</b> as <code>${esc(b.fileName)}</code> (${kb(b.bytes.length)},
      slot${b.included.length > 1 ? "s" : ""} ${b.included.map((s) => s.toUpperCase()).join(" + ")}).</div>
      ${b.included.length < 2 ? `<div class="note warn">One slot had no valid firmware and was left out.</div>` : ""}`;
  } else {
    msg = `<div class="note ok"><b>Done in ${secs} s.</b> The tablet runs
      ${esc(verLabel(info?.versions[info?.running]))} from slot ${info?.running?.toUpperCase()}.</div>`;
  }
  const saved = result?.savedBackup ?? wz.lastBackup;
  if (saved && !result?.backup) {
    msg += `<div class="note info">Backup of the previous firmware saved as <code>${esc(saved.fileName)}</code>.</div>`;
  }
  $("wz-body").innerHTML = msg + tasksHtml;
  const buttons = [];
  const again = result?.backup ?? saved;
  if (again) {
    buttons.push({ label: "Download backup again", cls: "ghost", onClick: () => download(again.bytes, again.fileName) });
  }
  buttons.push({ label: "Close", cls: "primary", onClick: closeWizard });
  foot(buttons);
}

function download(bytes, name) {
  const url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

// ------------------------------------------------------------ wiring

$("connect").onclick = async () => {
  manualOff = false;
  store.set("pth660.manualDisconnect", null);
  try {
    await attach(await requestTablet());
  } catch (e) {
    log(`connect failed: ${e.name}: ${e.message}`);
  }
};
$("refresh").onclick = refresh;
$("disconnect").onclick = async () => {
  if (busy || !tablet) return;
  manualOff = true;
  store.set("pth660.manualDisconnect", "1");
  const t = tablet;
  clearDevice();
  closeWizard();
  try {
    await t.close();
  } catch (e) {
    log(`close: ${e.message}`);
  }
  log("disconnected by hand; click Connect tablet to connect again");
};
$("wz-close").onclick = closeWizard;
function openAbout(e) {
  e?.preventDefault();
  $("about").open = true;
  $("about").scrollIntoView({ behavior: "smooth", block: "start" });
}
$("about-link").onclick = openAbout;
document.querySelectorAll(".action").forEach((b) => (b.onclick = () => openWizard(b.dataset.action)));

await loadFirmware();
render();
if (!hidSupported()) {
  $("support").hidden = false;
  $("connect").disabled = true;
} else {
  navigator.hid.addEventListener("disconnect", (e) => {
    if (tablet && e.device === tablet.dev) {
      log("tablet disconnected");
      clearDevice();
    }
  });
  navigator.hid.addEventListener("connect", async (e) => {
    if (!isVendorInterface(e.device)) return;
    log("tablet connected");
    if (tablet || attaching) return;
    if (manualOff && !waiters.length) return log("not connecting automatically (disconnected by hand)");
    await new Promise((r) => setTimeout(r, 2000)); // the firmware needs a moment after boot
    if (!tablet) await attach(e.device);
  });
  if (manualOff) log("not connecting automatically (disconnected by hand last time)");
  else findGranted().then((d) => d && attach(d)).catch((e) => log(e.message));
}

export { trimImage }; // keep the import used for debugging from the console

// ---- "How the pen works" slides (web/howto/*.svg, generated by build/make_howto.py): click the picture for the next.
// Chapter 1: the stock scan cycle; chapter 2: what each firmware method changes, drawn against stock.
const CHAPTERS = {
  stock: [
    ["1", "Inside the tablet: a grid of wires. Inside the pen: a coil and a capacitor, no battery."],
    ["2", "The tablet powers the pen by radio; the pen rings back with a signal the wires can hear."],
    ["3", "Step 1 - locate: the wires near the pen listen. The one closest to the pen hears it loudest."],
    ["4", "Comparing the three signals gives the pen's exact position, between the wires."],
    ["5", "Step 2 - power: the tablet sends a longer burst that recharges the pen."],
    ["6", "Step 3 - read data: the pen sends how hard it is pressed and which button is held, as strong and weak pulses."],
    ["7", "The three steps repeat in a loop of about 5 ms: roughly 200 times every second."],
    ["8", "Each cycle ends with one report to the computer: position, pressure and buttons."],
  ],
  methods: [
    ["m1", "Stock: one measurement, one report per cycle. The cursor jumps and trails the pen."],
    ["m2", "More Wacom runs (v2.45): the same measurements, the cursor moves in smaller steps, closer to the pen."],
    ["m3", "Even output (v3.29, v3.28): a report every 1 ms (0.5 ms), placed between measurements, never ahead."],
    ["m4", "Two measurements (v4.53, v3.78, v3.85): while the pen sends its data, the neighbouring wires listen too."],
    ["m4a", "The pen-data step burst by burst: one axis stays on the pen's wire to read the bit, the other listens on a neighbour."],
    ["m4b", "Why divide: a neighbour reading divided by the other axis's reading of the same burst cancels the pen's changing strength."],
    ["m4c", "The ratios replace the neighbour values in Wacom's frame; Wacom's own calculation turns them into the second position."],
    ["m5", "v4.53: twice the real measurements, one report for each (~480 per second)."],
    ["m6", "v3.78: twice the measurements and ~7 Wacom runs per cycle: ~1500 reports per second in fine steps."],
  ],
};
const HOWTO_ALT = Object.fromEntries(Object.values(CHAPTERS).flat());
let chapter = "stock", slide = 0;
function showSlide(n, ch = chapter) {
  if (ch !== chapter) {
    chapter = ch;
    $("slide-dots").innerHTML = CHAPTERS[chapter].map((_, i) => `<button aria-label="Picture ${i + 1}"></button>`).join("");
    $("slide-dots").querySelectorAll("button").forEach((b, i) => b.addEventListener("click", () => showSlide(i)));
    $("tab-stock").setAttribute("aria-selected", chapter === "stock" ? "true" : "false");
    $("tab-methods").setAttribute("aria-selected", chapter === "methods" ? "true" : "false");
  }
  const list = CHAPTERS[chapter];
  slide = (n + list.length) % list.length;
  const [file, text] = list[slide];
  const img = $("slide-img");
  img.src = `howto/${file}.svg`;
  img.alt = `${slide + 1} / ${list.length}: ${text}`;
  $("slide-dots").querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-current", i === slide ? "true" : "false"));
}
function openHowto(ch, n = 0) {
  $("howto").open = true;
  showSlide(n, ch);
  $("howto").scrollIntoView({ behavior: "smooth", block: "start" });
}
function initSlides() {
  if (!$("slides")) return;
  chapter = "";
  $("tab-stock").addEventListener("click", () => showSlide(0, "stock"));
  $("tab-methods").addEventListener("click", () => showSlide(0, "methods"));
  $("slide-img").addEventListener("click", () => showSlide(slide + 1));
  $("slide-prev").addEventListener("click", () => showSlide(slide - 1));
  $("slide-next").addEventListener("click", () => showSlide(slide + 1));
  $("slides").addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight") { showSlide(slide + 1); e.preventDefault(); }
    if (e.key === "ArrowLeft") { showSlide(slide - 1); e.preventDefault(); }
  });
  $("slides").tabIndex = 0;
  for (const [file] of Object.values(CHAPTERS).flat()) new Image().src = `howto/${file}.svg`;   // preload
  showSlide(0, "stock");
  // pictures inside a firmware's "How it works" open the slides at that picture
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-howto]");
    if (!el) return;
    e.preventDefault();
    const i = CHAPTERS.methods.findIndex(([f]) => f === el.dataset.howto);
    openHowto("methods", Math.max(0, i));
  });
}
initSlides();
