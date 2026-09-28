// WebHID transport for the PTH-660 vendor interface (usage page 0xFF0D).
// Phase 1: read-only operations only (status, running slot, sector map).
// Byte layouts: docs/PROTOCOL.md; behaviour mirrors reference/pth660_flash.py.

export const VID = 0x056a;
export const PID = 0x0357;
export const USAGE_PAGE = 0xff0d;

export const R = {
  STATUS2: 0xd0, // GET 8
  READ: 0xd1, // GET 260: u32 addr + 256 bytes
  POINTER: 0xd4, // SET 4: u32 addr (0xD1 read pointer)
  STATUS: 0xd5, // GET 4: [0] 0x00/0x20 ok, 1 bad index, 4 out of window
  MAP: 0xd9, // GET 2560: 256 x {u32 addr, u32 size, u16 flags}
};
export const LEN = { [R.STATUS2]: 8, [R.READ]: 260, [R.POINTER]: 4, [R.STATUS]: 4, [R.MAP]: 2560 };
export const STATUS_OK = [0x00, 0x20];
const BOOT_SLOT_RAM = 0x2003ff80; // bootloader's published slot: 1 = A, 2 = B
// 256-byte flash windows holding the VID/PID + version block (0x080917A8 A, 0x080F19A0 B;
// same offset in all 19 builds of each base, checked against every image in the repo)
const VERSION_BLOCK = { a: 0x08091700, b: 0x080f1900 };
// readable flash: the two firmware slots, stopping 4 KB short of the end of flash (0x08100000)
export const FLASH_READ_LO = 0x08040000;
export const FLASH_READ_HI = 0x08100000 - 0x1000;

export { versionName } from "./pkg.js";

export function hidSupported() {
  return typeof navigator !== "undefined" && "hid" in navigator;
}

// Already-permitted device (no prompt), e.g. after a reload or a tablet reboot.
export async function findGranted() {
  const devs = await navigator.hid.getDevices();
  return devs.find(isVendorInterface) ?? null;
}

export async function requestTablet() {
  const devs = await navigator.hid.requestDevice({
    filters: [{ vendorId: VID, productId: PID, usagePage: USAGE_PAGE }],
  });
  return devs.find(isVendorInterface) ?? null;
}

export function isVendorInterface(d) {
  return d.vendorId === VID && d.productId === PID && d.collections.some((c) => c.usagePage === USAGE_PAGE);
}

// Bytes the code actually uses from each GET (reports may be declared longer or shorter).
const NEED = { [R.STATUS2]: 1, [R.STATUS]: 1, [R.READ]: 260, [R.MAP]: 2560 };

export class Tablet {
  constructor(device, log = () => {}) {
    this.dev = device;
    this.log = log;
    // declared feature-report payload lengths (bytes, without the id) from the HID descriptor
    this.declared = {};
    for (const c of device.collections) {
      for (const r of c.featureReports ?? []) {
        const bits = (r.items ?? []).reduce((n, it) => n + (it.reportSize ?? 0) * (it.reportCount ?? 0), 0);
        this.declared[r.reportId] = bits / 8;
      }
    }
  }

  async open() {
    if (!this.dev.opened) await this.dev.open();
    return this;
  }

  async close() {
    if (this.dev.opened) await this.dev.close();
  }

  // GET a feature report; returns the payload without the report-id byte.
  // On Windows Chromium returns the id as byte 0 (seen: "D5 20 00 00 00" for 0xD5), and
  // the declared lengths are not reliable (0xDB shows as 2560). For every report used here
  // the first payload byte can never equal the id (status values, low byte of an address),
  // so the id is stripped exactly when byte 0 equals it.
  async get(id) {
    let last;
    for (let i = 0; i < 3; i++) {
      try {
        const dv = await this.dev.receiveFeatureReport(id);
        let u8 = new Uint8Array(dv.buffer, dv.byteOffset, dv.byteLength);
        if (u8[0] === id) u8 = u8.subarray(1);
        const need = NEED[id] ?? 1;
        if (u8.length < need) {
          throw new Error(`GET 0x${id.toString(16)}: ${u8.length} payload bytes, need ${need}`);
        }
        return u8.slice();
      } catch (e) {
        last = e;
        await sleep(50);
      }
    }
    throw last;
  }

  // SET a feature report; payload without the report-id byte, zero-padded to the declared length.
  async set(id, payload) {
    const len = this.declared[id] ?? LEN[id];
    if (payload.length > len) throw new Error(`SET 0x${id.toString(16)}: ${payload.length} bytes > ${len}`);
    const buf = new Uint8Array(len);
    buf.set(payload);
    await this.dev.sendFeatureReport(id, buf);
  }

  async status() {
    const s1 = (await this.get(R.STATUS))[0];
    const s2 = (await this.get(R.STATUS2))[0];
    return { s1, s2, ok: STATUS_OK.includes(s1) };
  }

  // 256 bytes of SRAM through the read window. SRAM only: never walk 0xD1 into the
  // end of flash (reading past the last flash block wedges the device until power-off).
  async peekRam(addr) {
    if (addr < 0x20000000 || addr + 256 > 0x20040000) throw new Error("peekRam: SRAM only");
    const a = new Uint8Array(4);
    new DataView(a.buffer).setUint32(0, addr, true);
    let r;
    for (let i = 0; i < 5; i++) {
      await this.set(R.POINTER, a);
      r = await this.get(R.READ);
      if (new DataView(r.buffer, r.byteOffset).getUint32(0, true) === addr) return r.subarray(4);
    }
    const head = [...r.slice(0, 12)].map((b) => b.toString(16).padStart(2, "0")).join(" ");
    throw new Error(`RAM read pointer never settled (asked 0x${addr.toString(16)}, ` +
                    `0xD1 returned ${r.length} bytes: ${head} ...)`);
  }

  // 256 bytes of flash, only inside the two firmware slots. Keeps well away from the end of
  // flash: reading 0xD1 past the last flash block wedges the device until a full power-down.
  async readFlash(addr) {
    if (addr < 0x08040000 || addr + 256 > 0x08100000 - 0x1000) throw new Error("readFlash: slot area only");
    const a = new Uint8Array(4);
    new DataView(a.buffer).setUint32(0, addr, true);
    for (let i = 0; i < 5; i++) {
      await this.set(R.POINTER, a);
      const r = await this.get(R.READ);
      if (new DataView(r.buffer, r.byteOffset).getUint32(0, true) === addr) return r.subarray(4);
    }
    throw new Error(`flash read at 0x${addr.toString(16)} never settled`);
  }

  // Read [addr, addr+len) of flash in 256-byte blocks (backup, read-back verify). Only inside
  // the two firmware slots and never within 4 KB of the end of flash: reading 0xD1 past the
  // last flash block wedges the device until a full power-down.
  async readRange(addr, len, onProgress = () => {}) {
    if (addr % 256 || len % 256) throw new Error("readRange: 256-byte aligned only");
    if (addr < FLASH_READ_LO || addr + len > FLASH_READ_HI) {
      throw new Error(`readRange: 0x${addr.toString(16)}+0x${len.toString(16)} outside the slot area`);
    }
    const out = new Uint8Array(len);
    const n = len / 256;
    const ptr = new Uint8Array(4);
    let expect = -1; // address the device's read pointer is at, -1 = unknown
    for (let i = 0; i < n; i++) {
      const a = addr + i * 256;
      let got = null;
      for (let tries = 0; tries < 5 && got === null; tries++) {
        if (expect !== a) {
          new DataView(ptr.buffer).setUint32(0, a, true);
          await this.set(R.POINTER, ptr);
        }
        const r = await this.get(R.READ);
        const ra = new DataView(r.buffer, r.byteOffset).getUint32(0, true);
        if (ra === a) {
          got = r.subarray(4, 260);
          expect = a + 256; // 0xD1 advances by 256 after each read
        } else {
          expect = -1;
        }
      }
      if (got === null) throw new Error(`flash read at 0x${a.toString(16)} never settled`);
      out.set(got, i * 256);
      if (i % 32 === 0 || i === n - 1) onProgress(i + 1, n);
    }
    return out;
  }

  // Installed firmware version per slot (bcdDevice as stored in the image: VID/PID then u16
  // version). The identity block is at the same place in every build of a base.
  async slotVersions() {
    const out = {};
    for (const [slot, addr] of Object.entries(VERSION_BLOCK)) {
      const d = await this.readFlash(addr);
      let v = null;
      for (let i = 0; i + 6 <= d.length; i++) {
        if (d[i] === 0x6a && d[i + 1] === 0x05 && d[i + 2] === 0x57 && d[i + 3] === 0x03) {
          v = d[i + 4] | (d[i + 5] << 8);
          break;
        }
      }
      out[slot] = v;
    }
    return out;
  }

  async runningSlot() {
    const idx = (await this.peekRam(BOOT_SLOT_RAM - 0x80))[0x80];
    return { 1: "a", 2: "b" }[idx] ?? null;
  }

  async sectorMap() {
    const body = await this.get(R.MAP);
    const dv = new DataView(body.buffer, body.byteOffset, body.byteLength);
    const out = [];
    for (let i = 0; i + 10 <= body.length; i += 10) {
      const addr = dv.getUint32(i, true);
      const size = dv.getUint32(i + 4, true);
      if (addr === 0 && size === 0) continue;
      out.push({ index: out.length, addr, size });
    }
    return out;
  }
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
