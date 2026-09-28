// Flash one slot: port of flash_slot() in reference/pth660_flash.py (tested on the tablet).
// Same order, same payloads, same checks. docs/PROTOCOL.md "Flashing one slot".

import { SLOTS, BLOCK, SECTOR, checkImage, sectorSum, hex } from "./pkg.js";
import { R, STATUS_OK, sleep } from "./hid.js";

export const W = { ERASE: 0xd3, PROG: 0xd2, SUM: 0xdb, COMMIT: 0xd6, REBOOT: 0x35 };
const FORBIDDEN = [
  [0x08000000, 0x08004000, "bootloader"],
  [0x08004000, 0x08004100, "slot table"],
  [0x0800c000, 0x0800c200, "key block"],
  [0x08010000, 0x08010100, "config sector"],
];
const REBOOT_MAGIC = new Uint8Array([0x52, 0x42, 0x54, 0, 0, 0, 0, 0, 0, 0]);

const u16u16 = (a, b) => le([[a, 2], [b, 2]]);
const u16u32 = (a, b) => le([[a, 2], [b, 4]]);
function le(fields) {
  const out = new Uint8Array(fields.reduce((n, [, w]) => n + w, 0));
  const dv = new DataView(out.buffer);
  let o = 0;
  for (const [v, w] of fields) {
    if (w === 2) dv.setUint16(o, v, true);
    else dv.setUint32(o, v >>> 0, true);
    o += w;
  }
  return out;
}

// Everything is checked before a single write. Throws with a readable reason.
export async function planFlash(tablet, images) {
  const running = await tablet.runningSlot();
  if (!SLOTS[running]) throw new Error("cannot read the running slot");
  const target = running === "a" ? "b" : "a";
  if (!images[target]) throw new Error(`package has no image for slot ${target.toUpperCase()}`);
  const { lo, hi } = SLOTS[target];
  const raw = images[target].data;
  const err = checkImage(raw, target);
  if (err) throw new Error(`image invalid for slot ${target.toUpperCase()}: ${err}`);
  const data = new Uint8Array(Math.ceil(raw.length / BLOCK) * BLOCK).fill(0xff);
  data.set(raw);
  const st = await tablet.status();
  if (!st.ok) throw new Error(`flash controller not idle (status 0x${hex(st.s1, 2)})`);
  const sectors = (await tablet.sectorMap()).filter((s) => s.addr < lo + data.length && s.addr + s.size > lo);
  if (!sectors.length) throw new Error("no sectors planned");
  for (const s of sectors) {
    for (const [a, b, name] of FORBIDDEN) {
      if (s.addr < b && a < s.addr + s.size) throw new Error(`sector ${s.index} overlaps the ${name}`);
    }
    if (s.addr < lo || s.addr + s.size - 1 > hi) throw new Error(`sector ${s.index} outside slot ${target.toUpperCase()}`);
    if (s.size !== SECTOR) throw new Error(`sector ${s.index} has size ${s.size}, expected ${SECTOR}`);
  }
  return {
    running, target, lo, data, sectors,
    blocks: data.length / BLOCK,
    version: images[target].entry.version,
  };
}

// Write the planned slot. dryRun: log every write, send nothing.
// onProgress({phase, done, total}) for erase / program / sums / commit.
export async function runFlash(tablet, plan, { dryRun = true, commit = true, verify = true, onProgress = () => {}, log = () => {} } = {}) {
  const { target, lo, data, sectors, blocks } = plan;
  let writes = 0;
  const send = async (id, payload) => {
    writes++;
    if (!dryRun) await tablet.set(id, payload);
  };
  const ok = async (what) => {
    if (dryRun) return;
    const st = await tablet.status();
    if (!STATUS_OK.includes(st.s1)) throw new Error(`ABORT: status 0x${hex(st.s1, 2)} after ${what}`);
  };

  log(`${dryRun ? "DRY RUN: " : ""}slot ${target.toUpperCase()}: erase ${sectors.length} sectors, ` +
      `program ${blocks} blocks, ${commit ? "commit" : "no commit"}`);

  onProgress({ phase: "erase", done: 0, total: sectors.length });
  for (let i = 0; i < sectors.length; i++) {
    await send(W.ERASE, u16u16(sectors[i].index, 0));
    await ok(`erase sector ${sectors[i].index}`);
    onProgress({ phase: "erase", done: i + 1, total: sectors.length });
  }
  log(`  erased ${sectors.length} sectors`);

  for (let i = 0; i < blocks; i++) {
    const addr = lo + i * BLOCK;
    const payload = new Uint8Array(4 + BLOCK);
    new DataView(payload.buffer).setUint32(0, addr, true);
    payload.set(data.subarray(i * BLOCK, (i + 1) * BLOCK), 4);
    await send(W.PROG, payload);
    if (i % 64 === 0) {
      await ok(`program 0x${hex(addr)}`);
      onProgress({ phase: "program", done: i + 1, total: blocks });
    }
  }
  await ok("last block");
  onProgress({ phase: "program", done: blocks, total: blocks });
  log(`  programmed ${blocks} blocks`);

  // Read the slot back and compare before anything makes it bootable. A mismatch stops here:
  // no commit, so the tablet keeps booting the running slot.
  if (verify && !dryRun) {
    const back = await tablet.readRange(lo, data.length, (done, total) =>
      onProgress({ phase: "verify", done, total }));
    for (let i = 0; i < data.length; i++) {
      if (back[i] !== data[i]) {
        throw new Error(`read-back mismatch at 0x${hex(lo + i)} (wrote 0x${hex(data[i], 2)}, ` +
                        `read 0x${hex(back[i], 2)}); not committed, the running slot still boots`);
      }
    }
    log(`  verified ${data.length} bytes by read-back`);
  } else {
    onProgress({ phase: "verify", done: 1, total: 1 });
  }

  const span = sectors[sectors.length - 1].addr + sectors[sectors.length - 1].size - lo;
  const padded = new Uint8Array(span).fill(0xff);
  padded.set(data);
  for (let i = 0; i < sectors.length; i++) {
    const off = sectors[i].addr - lo;
    await send(W.SUM, u16u32(sectors[i].index, sectorSum(padded.subarray(off, off + SECTOR))));
    onProgress({ phase: "sums", done: i + 1, total: sectors.length });
  }
  await ok("sector sums");
  log(`  declared ${sectors.length} sector checksums`);

  if (commit) {
    await send(W.COMMIT, le([[1, 4]]));
    let st = null;
    if (!dryRun) {
      await sleep(50);
      st = await tablet.status();
    }
    onProgress({ phase: "commit", done: 1, total: 1 });
    log(`  committed${st ? ` (status 0x${hex(st.s1, 2)})` : ""}: slot ${target.toUpperCase()} boots next`);
  }
  return { writes, dryRun };
}

// Same 10 bytes twice: stored, then matched -> MCU reset. The device drops off USB within
// ~0.1 s and comes back after ~9 s; the second send may error because of that.
export async function reboot(tablet, log = () => {}) {
  await tablet.set(W.REBOOT, REBOOT_MAGIC);
  await sleep(150);
  try {
    await tablet.set(W.REBOOT, REBOOT_MAGIC);
  } catch (e) {
    log(`reboot: second send ended with ${e.name} (expected when the tablet resets)`);
  }
}

export { R };
