// PTH-660 firmware package (.pkg) parsing and image checks.
// Pure functions: runs in the browser and in Node >= 20 (tests). Port of
// reference/pth660_flash.py (load_pkg, check_image, sector_sum).

export const SLOTS = {
  a: { lo: 0x08040000, hi: 0x0809ffff },
  b: { lo: 0x080a0000, hi: 0x080fffff },
};
export const BLOCK = 0x100;
export const SECTOR = 0x1000;
const MAGIC = new TextEncoder().encode("PTH660PKG\0");

export async function sha256Hex(bytes) {
  const d = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// PTH660PKG\0 | u32 LE manifest length | manifest JSON | image bytes
export async function parsePkg(buf) {
  const u8 = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  if (u8.length < MAGIC.length + 4 || !MAGIC.every((b, i) => u8[i] === b)) {
    throw new Error("not a PTH-660 package (bad magic)");
  }
  const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  const n = dv.getUint32(MAGIC.length, true);
  const start = MAGIC.length + 4;
  const manifest = JSON.parse(new TextDecoder().decode(u8.subarray(start, start + n)));
  const payload = u8.subarray(start + n);
  const images = {};
  for (const e of manifest.images) {
    const data = payload.slice(e.offset, e.offset + e.size);
    if (data.length !== e.size || (await sha256Hex(data)) !== e.sha256) {
      throw new Error(`slot ${e.slot.toUpperCase()} image corrupt (sha256 mismatch)`);
    }
    const err = checkImage(data, e.slot);
    if (err) throw new Error(`slot ${e.slot.toUpperCase()} image invalid: ${err}`);
    images[e.slot] = { entry: e, data };
  }
  return { manifest, images };
}

// An image is linked for one slot (absolute addresses): check SP, reset vector, size.
export function checkImage(data, slot) {
  const { lo, hi } = SLOTS[slot];
  const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const sp = dv.getUint32(0, true);
  const rv = dv.getUint32(4, true);
  if (sp < 0x20000000 || sp > 0x20040000) return `initial SP 0x${hex(sp)} not in SRAM`;
  const pc = (rv & ~1) >>> 0;
  if (!(rv & 1) || pc < lo || pc > hi) return `reset vector 0x${hex(rv)} not a Thumb address in slot ${slot.toUpperCase()}`;
  if (lo + data.length - 1 > hi) return "image larger than the slot";
  let ff = 0;
  for (const b of data) if (b === 0xff) ff++;
  if (ff > data.length / 2) return "image looks erased or truncated";
  return null;
}

// Device checksum of one 4096-byte sector: wrapping u32 sum of its little-endian words.
export function sectorSum(sector) {
  const dv = new DataView(sector.buffer, sector.byteOffset, sector.byteLength);
  let s = 0;
  for (let i = 0; i < SECTOR; i += 4) s = (s + dv.getUint32(i, true)) >>> 0;
  return s;
}

// Image padded with 0xFF to a whole number of sectors (what the slot holds after programming).
export function padToSectors(data) {
  const out = new Uint8Array(Math.ceil(data.length / SECTOR) * SECTOR).fill(0xff);
  out.set(data);
  return out;
}

// Which slot a raw image is linked for (reset vector inside the slot window), or null.
export function detectSlot(data) {
  if (data.length < 8) return null;
  for (const s of ["a", "b"]) if (!checkImage(data, s)) return s;
  return null;
}

// Version stored in an image: VID/PID (6A 05 57 03) then u16 version, or null.
export function imageVersion(data) {
  for (let i = 0; i + 6 <= data.length; i++) {
    if (data[i] === 0x6a && data[i + 1] === 0x05 && data[i + 2] === 0x57 && data[i + 3] === 0x03) {
      return data[i + 4] | (data[i + 5] << 8);
    }
  }
  return null;
}

// Slot contents read from flash -> the image: drop the erased (0xFF) tail, keep whole 256-byte blocks.
export function trimImage(slotBytes) {
  let end = slotBytes.length;
  while (end > 0 && slotBytes[end - 1] === 0xff) end--;
  return slotBytes.slice(0, Math.ceil(end / BLOCK) * BLOCK);
}

// Build a .pkg: PTH660PKG\0 | u32 LE manifest length | manifest JSON | images.
// images: [{ slot, data, desc, source }]
export async function buildPkg(images, name) {
  const entries = [];
  let off = 0;
  for (const im of images) {
    const v = imageVersion(im.data);
    entries.push({
      slot: im.slot, base: `0x${hex(SLOTS[im.slot].lo)}`,
      version: v == null ? "unknown" : `0x${hex(v, 4)}`,
      size: im.data.length, offset: off, sha256: await sha256Hex(im.data),
      desc: im.desc ?? "", source: im.source ?? "",
    });
    off += im.data.length;
  }
  const manifest = new TextEncoder().encode(JSON.stringify({
    format: 1, name, device: "Wacom PTH-660 056A:0357",
    created: new Date().toISOString().replace("T", " ").slice(0, 19), images: entries,
  }, null, 1));
  const out = new Uint8Array(MAGIC.length + 4 + manifest.length + off);
  out.set(MAGIC, 0);
  new DataView(out.buffer).setUint32(MAGIC.length, manifest.length, true);
  out.set(manifest, MAGIC.length + 4);
  let p = MAGIC.length + 4 + manifest.length;
  for (const im of images) {
    out.set(im.data, p);
    p += im.data.length;
  }
  return out;
}

// 0x0245 -> "v2.45"
export function versionName(v) {
  return v == null ? "unknown" : `v${(v >> 8).toString(16)}.${(v & 0xff).toString(16).padStart(2, "0")}`;
}

export function hex(v, w = 8) {
  return (v >>> 0).toString(16).toUpperCase().padStart(w, "0");
}
