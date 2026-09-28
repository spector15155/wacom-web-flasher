// The browser flasher must send exactly what the tested Python reference sends.
// tests/golden.json "_flashSequence" = the writes recorded from reference/pth660_flash.py
// flash_slot() against a simulated tablet with the real sector layout.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { parsePkg } from "../web/lib/pkg.js";
import { Tablet } from "../web/lib/hid.js";
import { planFlash, runFlash, reboot } from "../web/lib/flash.js";
import { MockTablet } from "./mock-device.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const FW = process.env.FIRMWARE_DIR ?? join(here, "..", "firmware");
const golden = JSON.parse(await readFile(join(here, "golden.json"), "utf8"))._flashSequence;
const pkg = await parsePkg(new Uint8Array(await readFile(join(FW, "pth660_v245_best.pkg"))));

function seqHash(writes) {
  const h = createHash("sha256");
  for (const w of writes) {
    h.update(Buffer.from([w.length & 0xff, w.length >> 8]));
    h.update(w);
  }
  return h.digest("hex");
}

for (const target of ["a", "b"]) {
  test(`flash slot ${target.toUpperCase()}: write sequence identical to the Python reference`, async () => {
    const g = golden[`pth660_v245_best.pkg:${target}`];
    const dev = new MockTablet({ running: g.running });
    const t = await new Tablet(dev).open();
    const plan = await planFlash(t, pkg.images);
    assert.equal(plan.target, target);
    const res = await runFlash(t, plan, { dryRun: false });
    const writes = dev.writes();
    assert.equal(writes.length, g.writes);
    assert.equal(res.writes, g.writes);
    const counts = {};
    for (const w of writes) {
      const k = `0x${w[0].toString(16).toUpperCase()}`;
      counts[k] = (counts[k] ?? 0) + 1;
    }
    assert.deepEqual(counts, g.counts);
    assert.equal(Buffer.from(writes[0]).toString("hex"), g.first);
    assert.equal(Buffer.from(writes.at(-1)).toString("hex"), g.last);
    assert.equal(seqHash(writes), g.sha256);
    assert.equal(dev.committed, target);
  });
}

test("dry run sends no flash writes", async () => {
  const dev = new MockTablet({ running: "b" });
  const t = await new Tablet(dev).open();
  const plan = await planFlash(t, pkg.images);
  const res = await runFlash(t, plan, { dryRun: true });
  assert.equal(res.writes, golden["pth660_v245_best.pkg:a"].writes);
  assert.equal(dev.writes().length, 0); // only 0xD4 pointer sets for the reads, filtered out
  assert.equal(dev.committed, null);
});

test("plan targets the non-running slot and reads versions", async () => {
  for (const running of ["a", "b"]) {
    const t = await new Tablet(new MockTablet({ running })).open();
    const plan = await planFlash(t, pkg.images);
    assert.equal(plan.running, running);
    assert.notEqual(plan.target, running);
    assert.deepEqual(await t.slotVersions(), { a: 0x0248, b: 0x0249 });
  }
});

test("a bad status aborts the flash before the commit", async () => {
  const dev = new MockTablet({ running: "b" });
  const t = await new Tablet(dev).open();
  const plan = await planFlash(t, pkg.images);
  plan.lo = 0x080a0000; // wrong window on purpose: the device answers status 4
  await assert.rejects(runFlash(t, plan, { dryRun: false }), /status 0x04/);
  assert.equal(dev.committed, null);
});

test("an image for the wrong slot is refused in the plan", async () => {
  const t = await new Tablet(new MockTablet({ running: "b" })).open();
  const swapped = { a: pkg.images.b, b: pkg.images.a };
  await assert.rejects(planFlash(t, swapped), /image invalid for slot A/);
});

test("reboot sends the 0x35 handshake twice", async () => {
  const dev = new MockTablet();
  const t = await new Tablet(dev).open();
  await reboot(t);
  const sent = dev.sets.filter((s) => s.id === 0x35);
  assert.equal(sent.length, 2);
  assert.equal(Buffer.from(sent[0].data.subarray(0, 10)).toString("hex"), "524254" + "00".repeat(7));
  assert.equal(dev.reboots, 1);
});
