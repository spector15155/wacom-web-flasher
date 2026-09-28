// A simulated PTH-660 vendor interface with the WebHID HIDDevice API, behaving like the real
// tablet in Chrome on Windows: receiveFeatureReport returns the report id as byte 0, declared
// feature lengths are inflated (0xDB shows as 2560), writes outside the target slot give
// status 4, a bad sector index gives status 1, a commit leaves status 0x30.
// Flash 0x08040000-0x080FFFFF is simulated: erase fills 0xFF, program writes, 0xD1 reads back.
// The reboot handshake (0x35 twice) boots the committed slot, like the bootloader.
// Reading 0xD1 past the end of flash marks the device wedged (the real one needs a power-down).
// Every SET is recorded as { id, data } so tests can compare it with the Python reference.

const TRUE_LEN = { 0x35: 10, 0xd2: 260, 0xd3: 4, 0xd4: 4, 0xd6: 4, 0xdb: 6 };
const DECLARED = { 0x35: 10, 0xd0: 4, 0xd1: 260, 0xd2: 260, 0xd3: 4, 0xd4: 4, 0xd5: 4, 0xd6: 4, 0xd9: 2560, 0xdb: 2560 };
const VERSION_AT = { a: 0x080917a8, b: 0x080f19a0 };
const MEM_LO = 0x08040000;
const MEM_HI = 0x08100000;
const SLOT_LO = { a: 0x08040000, b: 0x080a0000 };

export class MockTablet {
  constructor({ running = "b", versions = { a: 0x0248, b: 0x0249 }, images = {} } = {}) {
    this.running = running;
    this.opened = false;
    this.vendorId = 0x056a;
    this.productId = 0x0357;
    this.productName = "Mock Intuos Pro M";
    this.collections = [{
      usagePage: 0xff0d, usage: 1, inputReports: [],
      featureReports: Object.entries(DECLARED).map(([id, len]) => ({
        reportId: Number(id), items: [{ reportSize: 8, reportCount: len }],
      })),
    }];
    this.sets = [];
    this.status = 0x20;
    this.pointer = 0;
    this.committed = null;
    this.reboots = 0;
    this.wedged = false;
    this.mem = new Uint8Array(MEM_HI - MEM_LO).fill(0xff);
    for (const slot of ["a", "b"]) {
      if (images[slot]) {
        this.mem.set(images[slot], SLOT_LO[slot] - MEM_LO);
      } else {
        const v = versions[slot];
        this.mem.set([0x6a, 0x05, 0x57, 0x03, v & 0xff, v >> 8], VERSION_AT[slot] - MEM_LO);
      }
    }
    this.last35 = null;
  }

  async open() { this.opened = true; }
  async close() { this.opened = false; }

  slotBytes(slot, len) {
    const o = SLOT_LO[slot] - MEM_LO;
    return this.mem.slice(o, o + len);
  }

  async sendFeatureReport(id, data) {
    const u8 = new Uint8Array(data.buffer ?? data, data.byteOffset ?? 0, data.byteLength ?? data.length);
    this.sets.push({ id, data: u8.slice() });
    const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    const target = this.running === "a" ? "b" : "a";
    const win = target === "a" ? [0x08040000, 0x0809ffff] : [0x080a0000, 0x080fffff];
    switch (id) {
      case 0xd4: this.pointer = dv.getUint32(0, true); break;
      case 0xd3: {
        const idx = dv.getUint16(0, true);
        const a = 0x08000000 + idx * 0x1000;
        if (idx >= 256) this.status = 0x01;
        else {
          if (a >= win[0] && a + 0xfff <= win[1]) this.mem.fill(0xff, a - MEM_LO, a - MEM_LO + 0x1000);
          this.status = 0x00;
        }
        break;
      }
      case 0xd2: {
        const a = dv.getUint32(0, true);
        if (a >= win[0] && a + 255 <= win[1]) {
          const o = a - MEM_LO;
          for (let i = 0; i < 256; i++) this.mem[o + i] &= u8[4 + i]; // NOR flash: program only clears bits
          this.status = 0x00;
        } else this.status = 0x04;
        break;
      }
      case 0xdb: this.status = 0x00; break;
      case 0xd6: this.status = 0x30; this.committed = target; break;
      case 0x35: {
        const key = [...u8.subarray(0, 10)].join(",");
        if (this.last35 === key) {
          this.reboots++;
          if (this.committed) this.running = this.committed;
          this.committed = null;
          this.status = 0x20;
          this.last35 = null;
        } else this.last35 = key;
        break;
      }
      default: break;
    }
  }

  async receiveFeatureReport(id) {
    if (this.wedged) throw new Error("device wedged (read past the end of flash)");
    const len = DECLARED[id];
    const out = new Uint8Array(1 + len);
    out[0] = id; // Windows Chromium: report id as byte 0
    const dv = new DataView(out.buffer);
    if (id === 0xd5 || id === 0xd0) {
      out[1] = this.status;
    } else if (id === 0xd1) {
      const p = this.pointer;
      dv.setUint32(1, p, true);
      const body = out.subarray(5);
      if (p === 0x2003ff00) body[0x80] = this.running === "a" ? 1 : 2;
      else if (p >= MEM_LO && p < MEM_HI) body.set(this.mem.subarray(p - MEM_LO, p - MEM_LO + 256));
      else if (p >= MEM_HI && p < 0x20000000) this.wedged = true;
      this.pointer += 256;
    } else if (id === 0xd9) {
      for (let i = 0; i < 256; i++) {
        dv.setUint32(1 + i * 10, 0x08000000 + i * 0x1000, true);
        dv.setUint32(1 + i * 10 + 4, 0x1000, true);
      }
    }
    return new DataView(out.buffer);
  }

  // the flash writes, cut to their true payload length (the rest must be zero padding)
  writes() {
    return this.sets
      .filter((s) => s.id !== 0xd4 && s.id !== 0x35)
      .map((s) => {
        const n = TRUE_LEN[s.id] ?? s.data.length;
        if (s.data.subarray(n).some((b) => b !== 0)) throw new Error(`SET 0x${s.id.toString(16)} padding not zero`);
        const w = new Uint8Array(1 + n);
        w[0] = s.id;
        w.set(s.data.subarray(0, n), 1);
        return w;
      });
  }
}
