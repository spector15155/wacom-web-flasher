// Multi-step jobs built from the tested pieces: backup both slots, install a package (one or
// both slots), restore a single .bin (switching slots first when needed).
//
// ctx = {
//   tablet(): the current Tablet,
//   rebootAndReconnect(): reboot the tablet and resolve with the reconnected Tablet,
//   step(title, detail): a new step starts (UI checklist),
//   progress(phase, done, total): progress inside the current step,
//   log(msg),
// }

import { SLOTS, checkImage, trimImage, imageVersion, versionName, buildPkg, detectSlot, sha256Hex } from "./pkg.js";
import { FLASH_READ_HI } from "./hid.js";
import { planFlash, runFlash, reboot } from "./flash.js";

const other = (s) => (s === "a" ? "b" : "a");

// ---------------------------------------------------------------- backup

// Read both slots, trim the erased tail, check each image. Never reads the last 4 KB of flash.
export async function readSlots(ctx) {
  const t = ctx.tablet();
  const running = await t.runningSlot();
  const out = { running, slots: {} };
  for (const slot of ["a", "b"]) {
    const { lo, hi } = SLOTS[slot];
    const end = Math.min(hi + 1, FLASH_READ_HI);
    ctx.step(`Read slot ${slot.toUpperCase()}`, `0x${lo.toString(16)}-0x${(end - 1).toString(16)}`);
    const raw = await t.readRange(lo, end - lo, (d, n) => ctx.progress("read", d, n));
    const data = trimImage(raw);
    const error = data.length === 0 ? "slot is empty" : checkImage(data, slot);
    const version = imageVersion(data);
    out.slots[slot] = { data, version, error, sha256: data.length ? await sha256Hex(data) : null };
    ctx.log(`slot ${slot.toUpperCase()}: ${data.length} bytes, ${versionName(version)}` +
            `${error ? `, NOT usable: ${error}` : ", valid image"}`);
  }
  return out;
}

export async function backupPackage(ctx) {
  const r = await readSlots(ctx);
  const good = ["a", "b"].filter((s) => !r.slots[s].error);
  if (!good.length) throw new Error("neither slot holds a valid image; nothing to back up");
  ctx.step("Save backup file", good.map((s) => `slot ${s.toUpperCase()}`).join(" + "));
  const images = good.map((s) => ({
    slot: s, data: r.slots[s].data, source: "backup from tablet",
    desc: `backup of slot ${s.toUpperCase()} (${versionName(r.slots[s].version)})${s === r.running ? ", was running" : ""}`,
  }));
  const tag = good.map((s) => `${s.toUpperCase()}-${versionName(r.slots[s].version)}`).join("_");
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-:]/g, "").replace("T", "-");
  const bytes = await buildPkg(images, `Backup ${tag} (${stamp})`);
  return { ...r, bytes, fileName: `pth660_backup_${tag}_${stamp}.pkg`, included: good };
}

// ---------------------------------------------------------------- install

// Flash `image` ({ slot, data, version }) into its slot, which must be the inactive one,
// then reboot into it and check the tablet runs it.
async function flashAndBoot(ctx, image, { title, verify = true, dryRun = false } = {}) {
  const t = ctx.tablet();
  const images = { [image.slot]: { data: image.data, entry: { version: `0x${image.version.toString(16)}` } } };
  const plan = await planFlash(t, images);
  if (plan.target !== image.slot) throw new Error(`slot ${image.slot.toUpperCase()} is running; cannot write it`);
  ctx.step(title ?? `Write slot ${plan.target.toUpperCase()} (${versionName(image.version)})`,
           `erase ${plan.sectors.length} sectors, program ${plan.blocks} blocks, verify, commit`);
  const res = await runFlash(t, plan, {
    dryRun, verify, log: ctx.log, onProgress: ({ phase, done, total }) => ctx.progress(phase, done, total),
  });
  if (dryRun) return res;
  ctx.step(`Reboot into slot ${plan.target.toUpperCase()}`, "the tablet disconnects and comes back in ~10 s");
  const t2 = await ctx.rebootAndReconnect();
  const running = await t2.runningSlot();
  const vers = await t2.slotVersions();
  if (running !== plan.target || vers[running] !== image.version) {
    throw new Error(`after reboot the tablet runs slot ${running?.toUpperCase()} ${versionName(vers[running])}, ` +
                    `expected slot ${plan.target.toUpperCase()} ${versionName(image.version)}`);
  }
  ctx.log(`running slot ${running.toUpperCase()} ${versionName(image.version)} as flashed`);
  return res;
}

function imageOf(pkgImage, slot) {
  const data = pkgImage.data;
  return { slot, data, version: imageVersion(data) ?? parseInt(pkgImage.entry.version, 16) };
}

// Install a parsed package: the inactive slot, then (both) the other one too.
export async function installPackage(ctx, pkg, { both = false, dryRun = false } = {}) {
  const running = await ctx.tablet().runningSlot();
  const first = other(running);
  if (!pkg.images[first]) throw new Error(`package has no image for slot ${first.toUpperCase()}`);
  await flashAndBoot(ctx, imageOf(pkg.images[first], first), { dryRun });
  if (!both || dryRun) return;
  if (!pkg.images[running]) throw new Error(`package has no image for slot ${running.toUpperCase()}`);
  await flashAndBoot(ctx, imageOf(pkg.images[running], running));
}

// Restore one raw image. If it's linked for the running slot, switch slots first by
// re-installing (verbatim) what the other slot already holds.
export async function installBin(ctx, bin, { dryRun = false } = {}) {
  const slot = detectSlot(bin);
  if (!slot) throw new Error("this .bin is not a PTH-660 firmware image for slot A or B");
  const version = imageVersion(bin);
  if (version == null) throw new Error("no version block (056A:0357) in this image");
  const image = { slot, data: trimImage(bin), version };
  const running = await ctx.tablet().runningSlot();
  if (slot === running) {
    const o = other(slot);
    ctx.step(`Read slot ${o.toUpperCase()} (to switch to it)`, `the .bin is for slot ${slot.toUpperCase()}, which is running`);
    const { lo, hi } = SLOTS[o];
    const end = Math.min(hi + 1, FLASH_READ_HI);
    const cur = trimImage(await ctx.tablet().readRange(lo, end - lo, (d, n) => ctx.progress("read", d, n)));
    const err = cur.length ? checkImage(cur, o) : "slot is empty";
    if (err) throw new Error(`slot ${o.toUpperCase()} has no valid image to switch to (${err}); install a package first`);
    const curVer = imageVersion(cur);
    if (dryRun) {
      ctx.log(`dry run: would re-install slot ${o.toUpperCase()} (${versionName(curVer)}), reboot, then write the .bin`);
      return;
    }
    await flashAndBoot(ctx, { slot: o, data: cur, version: curVer },
                       { title: `Re-install slot ${o.toUpperCase()} (${versionName(curVer)}) to switch slots` });
  }
  await flashAndBoot(ctx, image, { dryRun });
}

export { reboot };
