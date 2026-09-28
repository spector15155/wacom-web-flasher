// Backup / install / restore jobs end to end against the simulated tablet (real flash bytes,
// reboot boots the committed slot).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { parsePkg, trimImage, imageVersion, detectSlot, sha256Hex } from "../web/lib/pkg.js";
import { Tablet } from "../web/lib/hid.js";
import { backupPackage, installPackage, installBin } from "../web/lib/tasks.js";
import { MockTablet } from "./mock-device.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const FW = process.env.FIRMWARE_DIR ?? join(here, "..", "firmware");
const load = async (f) => parsePkg(new Uint8Array(await readFile(join(FW, f))));
const best = await load("pth660_v245_best.pkg");
const stock = await load("pth660_stock_v151_v152.pkg");

async function setup(opts) {
  const dev = new MockTablet(opts);
  let t = await new Tablet(dev).open();
  const steps = [];
  const ctx = {
    tablet: () => t,
    rebootAndReconnect: async () => {
      const { reboot } = await import("../web/lib/flash.js");
      await reboot(t);
      t = await new Tablet(dev).open(); // the same simulated device comes back
      return t;
    },
    step: (title) => steps.push(title),
    progress: () => {},
    log: () => {},
  };
  return { dev, ctx, steps };
}

test("backup: both slots read, trimmed, packaged, and parse back byte-identical", async () => {
  const { dev, ctx, steps } = await setup({ running: "b", images: { a: best.images.a.data, b: stock.images.b.data } });
  const r = await backupPackage(ctx);
  assert.deepEqual(r.included, ["a", "b"]);
  assert.match(r.fileName, /^pth660_backup_A-v2\.45_B-v1\.52_\d{8}-\d{4}\.pkg$/);
  const back = await parsePkg(r.bytes);
  assert.equal(await sha256Hex(back.images.a.data), await sha256Hex(trimImage(best.images.a.data)));
  assert.equal(await sha256Hex(back.images.b.data), await sha256Hex(trimImage(stock.images.b.data)));
  assert.equal(back.images.a.entry.version, "0x0245");
  assert.equal(dev.wedged, false, "never read past the end of flash");
  assert.equal(dev.writes().length, 0, "backup writes nothing");
  assert.deepEqual(steps.slice(0, 2), ["Read slot A", "Read slot B"]);
});

test("backup skips a slot without a valid image", async () => {
  const { ctx } = await setup({ running: "b", images: { b: stock.images.b.data }, versions: { a: 0x0248 } });
  const r = await backupPackage(ctx);
  assert.deepEqual(r.included, ["b"]);
  assert.ok(r.slots.a.error);
});

test("install package on both slots: inactive first, reboot, then the other, both verified", async () => {
  const { dev, ctx } = await setup({ running: "b", images: { a: stock.images.a.data, b: stock.images.b.data } });
  await installPackage(ctx, best, { both: true });
  assert.equal(dev.reboots, 2);
  assert.equal(dev.running, "b"); // A written + booted, then B written + booted
  for (const s of ["a", "b"]) {
    const img = best.images[s].data;
    assert.deepEqual(dev.slotBytes(s, img.length), img, `slot ${s}`);
  }
});

test("restore a .bin for the running slot: switches slots first, then writes it", async () => {
  // running A (stock v1.51), B holds stock v1.52; restore v2.45's slot-A image
  const { dev, ctx, steps } = await setup({ running: "a", images: { a: stock.images.a.data, b: stock.images.b.data } });
  const bin = best.images.a.data;
  assert.equal(detectSlot(bin), "a");
  await installBin(ctx, bin);
  assert.equal(dev.running, "a");
  assert.deepEqual(dev.slotBytes("a", bin.length), bin);
  assert.deepEqual(dev.slotBytes("b", stock.images.b.data.length), stock.images.b.data, "slot B re-installed verbatim");
  assert.equal(dev.reboots, 2);
  assert.ok(steps[0].startsWith("Read slot B"));
});

test("restore a .bin for the inactive slot: one write, one reboot", async () => {
  const { dev, ctx } = await setup({ running: "b", images: { a: stock.images.a.data, b: stock.images.b.data } });
  await installBin(ctx, best.images.a.data);
  assert.equal(dev.running, "a");
  assert.equal(dev.reboots, 1);
  assert.equal(imageVersion(dev.slotBytes("a", best.images.a.data.length)), 0x0245);
});

test("a random file is refused as .bin", async () => {
  const { ctx } = await setup({ running: "b" });
  await assert.rejects(installBin(ctx, new Uint8Array(4096).fill(7)), /not a PTH-660 firmware image/);
});

test("dry-run install sends no flash writes and no reboot", async () => {
  const { dev, ctx } = await setup({ running: "b", images: { a: stock.images.a.data, b: stock.images.b.data } });
  await installPackage(ctx, best, { both: true, dryRun: true });
  assert.equal(dev.writes().length, 0);
  assert.equal(dev.reboots, 0);
});
