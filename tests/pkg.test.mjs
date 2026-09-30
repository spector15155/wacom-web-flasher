// Checks the browser package code against the Python reference (tests/golden.json
// is generated from reference/pth660_flash.py). Run: node --test tests/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { parsePkg, checkImage, sectorSum, padToSectors, sha256Hex, SECTOR } from "../web/lib/pkg.js";

const here = dirname(fileURLToPath(import.meta.url));
const FW = process.env.FIRMWARE_DIR ?? join(here, "..", "firmware");
const golden = JSON.parse(await readFile(join(here, "golden.json"), "utf8"));
const manifest = JSON.parse(await readFile(join(FW, "manifest.json"), "utf8"));

test("manifest: every package matches its sha256 and parses", async () => {
  for (const fw of manifest.firmware) {
    const buf = new Uint8Array(await readFile(join(FW, fw.file)));
    assert.equal(buf.length, fw.size, fw.file);
    assert.equal(await sha256Hex(buf), fw.sha256, fw.file);
    const { images } = await parsePkg(buf);
    for (const im of fw.images) {
      assert.equal(images[im.slot].entry.sha256, im.sha256, `${fw.file} slot ${im.slot}`);
      assert.equal(images[im.slot].data.length, im.size);
    }
  }
});

test("manifest: at least one recommended package, the first one is the default", () => {
  assert.ok(manifest.firmware.filter((f) => f.recommended).length >= 1);
});

for (const [file, g] of Object.entries(golden).filter(([k]) => !k.startsWith("_"))) {
  test(`sector sums match the Python reference: ${file}`, async () => {
    const { images } = await parsePkg(new Uint8Array(await readFile(join(FW, file))));
    for (const [slot, want] of Object.entries(g.images)) {
      const data = images[slot].data;
      assert.equal(data.length, want.size);
      assert.equal(Math.ceil(data.length / 256), want.blocks);
      const padded = padToSectors(data);
      const sums = [];
      for (let i = 0; i < padded.length; i += SECTOR) sums.push(sectorSum(padded.subarray(i, i + SECTOR)));
      assert.deepEqual(sums, want.sectorSums, `${file} slot ${slot}`);
    }
  });
}

test("image check: slot A image is rejected for slot B and vice versa", async () => {
  const { images } = await parsePkg(new Uint8Array(await readFile(join(FW, manifest.firmware[0].file))));
  assert.equal(checkImage(images.a.data, "a"), null);
  assert.equal(checkImage(images.b.data, "b"), null);
  assert.match(checkImage(images.a.data, "b"), /reset vector/);
  assert.match(checkImage(images.b.data, "a"), /reset vector/);
});

test("package: bad magic and corrupted image are rejected", async () => {
  const buf = new Uint8Array(await readFile(join(FW, manifest.firmware[0].file)));
  const bad = buf.slice();
  bad[0] ^= 0xff;
  await assert.rejects(parsePkg(bad), /bad magic/);
  const corrupt = buf.slice();
  corrupt[corrupt.length - 1000] ^= 0x01;
  await assert.rejects(parsePkg(corrupt), /sha256 mismatch/);
});

// When run inside docker compose, also check the web service serves the app.
const WEB = process.env.WEB_URL;
test("web service serves the app, manifest and packages", { skip: !WEB && "WEB_URL not set" }, async () => {
  const page = await fetch(`${WEB}/`);
  assert.equal(page.status, 200);
  assert.match(await page.text(), /PTH-660 Flasher/);
  const man = await (await fetch(`${WEB}/firmware/manifest.json`)).json();
  assert.equal(man.firmware.length, manifest.firmware.length);
  const pkg = await fetch(`${WEB}/firmware/${man.firmware[0].file}`);
  assert.equal(pkg.status, 200);
  const buf = new Uint8Array(await pkg.arrayBuffer());
  assert.equal(await sha256Hex(buf), man.firmware[0].sha256);
  for (const p of ["app.js", "lib/hid.js", "lib/pkg.js"]) {
    const r = await fetch(`${WEB}/${p}`);
    assert.equal(r.status, 200, p);
    assert.match(r.headers.get("content-type") ?? "", /javascript/, p);
  }
});
