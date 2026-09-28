"""
PTH-660 firmware flasher: reference implementation for porting to WebHID.

Self-contained merge of the tools used for every flash of this project
(full_flash.py, flash_universal.py, reboot_tablet.py, probe_scan.py). Same byte
layouts, same order, same checks. See ../docs/PROTOCOL.md.

*** Nothing is written unless --arm is given. ***

  pth660_flash.py info    PKG                 package manifest
  pth660_flash.py status                      version, running slot, flash status
  pth660_flash.py flash   PKG [--arm] [--both] [--no-commit]
  pth660_flash.py reboot  [--arm]

Requires: pip install hidapi
"""
import argparse
import hashlib
import json
import struct
import sys
import time

import hid

VID, PID = 0x056A, 0x0357
USAGE_PAGE = 0xFF0D                      # vendor interface carrying all flash reports

# feature reports: id -> payload length (without the report-id byte)
R_REBOOT = 0x35    # 10  SET twice with the same bytes -> MCU reset
R_STATUS2 = 0xD0   # 8   GET
R_READ = 0xD1      # 260 GET: [u32 addr][256 data] at the read pointer, then pointer += 256
R_PROG = 0xD2      # 260 SET: [u32 addr][256 data] program one block
R_ERASE = 0xD3     # 4   SET: [u16 sector index][u16 0]
R_POINTER = 0xD4   # 4   SET: [u32 addr] set the 0xD1 read pointer (used for RAM reads)
R_STATUS = 0xD5    # 4   GET: [0] status, 0x00/0x20 = ok, 1 = bad sector index, 4 = out of window
R_COMMIT = 0xD6    # 4   SET: [u32 1] write the slot table -> the written slot boots next
R_MAP = 0xD9       # 2560 GET: 256 x [u32 addr][u32 size][u16 flags]
R_SUM = 0xDB       # 6   SET: [u16 sector index][u32 expected sum] (needed before commit)

BLOCK = 0x100
SLOTS = {"a": (0x08040000, 0x0809FFFF), "b": (0x080A0000, 0x080FFFFF)}
FORBIDDEN = [(0x08000000, 0x08004000, "bootloader"), (0x08004000, 0x08004100, "slot table"),
             (0x0800C000, 0x0800C200, "key block"), (0x08010000, 0x08010100, "config sector")]
STATUS_OK = (0x00, 0x20)
PKG_MAGIC = b"PTH660PKG\0"
BOOT_SLOT_RAM = 0x2003FF80               # bootloader's published slot: 1 = A, 2 = B
REBOOT_MAGIC = bytes([0x52, 0x42, 0x54, 0, 0, 0, 0, 0, 0, 0])


# ---------------------------------------------------------------- package

def load_pkg(path):
    """PTH660PKG\\0 | u32 LE manifest length | manifest JSON | image bytes."""
    raw = open(path, "rb").read()
    if not raw.startswith(PKG_MAGIC):
        raise SystemExit("not a PTH-660 package (bad magic)")
    n = struct.unpack_from("<I", raw, len(PKG_MAGIC))[0]
    start = len(PKG_MAGIC) + 4
    man = json.loads(raw[start:start + n])
    payload = raw[start + n:]
    images = {}
    for e in man["images"]:
        data = payload[e["offset"]:e["offset"] + e["size"]]
        if len(data) != e["size"] or hashlib.sha256(data).hexdigest() != e["sha256"]:
            raise SystemExit(f"slot {e['slot'].upper()} image corrupt (sha256 mismatch)")
        err = check_image(data, e["slot"])
        if err:
            raise SystemExit(f"slot {e['slot'].upper()} image invalid: {err}")
        images[e["slot"]] = (e, data)
    return man, images


def check_image(data, slot):
    """Each image is linked for one slot (absolute addresses, no relocation)."""
    lo, hi = SLOTS[slot]
    sp, rv = struct.unpack_from("<II", data, 0)
    if not 0x20000000 <= sp <= 0x20040000:
        return f"initial SP 0x{sp:08X} not in SRAM"
    if not (rv & 1 and lo <= (rv & ~1) <= hi):
        return f"reset vector 0x{rv:08X} not a Thumb address in slot {slot.upper()}"
    if lo + len(data) - 1 > hi:
        return "image larger than the slot"
    if data.count(0xFF) > len(data) // 2:
        return "image looks erased or truncated"
    return None


def sector_sum(buf):
    """Device checksum of one 4096-byte sector: wrapping u32 sum of its words."""
    return sum(struct.unpack("<1024I", buf)) & 0xFFFFFFFF


# ---------------------------------------------------------------- device

class Tablet:
    def __init__(self):
        path, self.version = None, 0
        for d in hid.enumerate(VID, PID):
            if d.get("usage_page") == USAGE_PAGE:
                path, self.version = d["path"], d.get("release_number", 0)
        if not path:
            raise SystemExit("tablet not found (056A:0357, usage page 0xFF0D)")
        self.h = hid.device()
        self.h.open_path(path)

    def close(self):
        self.h.close()

    def get(self, rid, length):
        for _ in range(3):
            try:
                return bytes(self.h.get_feature_report(rid, length + 1))[1:]
            except OSError:
                time.sleep(0.05)
        raise SystemExit(f"GET 0x{rid:02X} failed")

    def set(self, rid, payload):
        if self.h.send_feature_report(bytes([rid]) + payload) < 0:
            raise SystemExit(f"SET 0x{rid:02X} failed")

    def status(self):
        return self.get(R_STATUS, 4)[0], self.get(R_STATUS2, 8)[0]

    def peek_ram(self, addr):
        """256 bytes of SRAM: set the read pointer, read one window. SRAM only,
        and never walk 0xD1 past the end of flash (it wedges the interface)."""
        assert 0x20000000 <= addr and addr + 256 <= 0x20040000
        for _ in range(5):
            self.set(R_POINTER, struct.pack("<I", addr))
            r = self.get(R_READ, 260)
            if struct.unpack_from("<I", r, 0)[0] == addr:
                return r[4:]
        raise SystemExit("RAM read pointer never settled")

    def running_slot(self):
        idx = self.peek_ram(BOOT_SLOT_RAM - 0x80)[0x80]
        return {1: "a", 2: "b"}.get(idx)

    def sector_map(self):
        body, out = self.get(R_MAP, 2560), []
        for i in range(0, len(body) - 9, 10):
            addr, size = struct.unpack_from("<II", body, i)
            if addr == 0 and size == 0:
                continue
            out.append({"index": len(out), "addr": addr, "size": size})
        return out


# ---------------------------------------------------------------- flash

def flash_slot(t, target, data, arm, commit=True):
    running = t.running_slot()
    if running not in SLOTS:
        raise SystemExit("cannot read the running slot")
    if target == running:
        raise SystemExit("refusing to write the running slot")
    lo, hi = SLOTS[target]
    err = check_image(data, target)
    if err:
        raise SystemExit(f"image invalid for slot {target.upper()}: {err}")
    data += b"\xFF" * (-len(data) % BLOCK)
    st, _ = t.status()
    if st not in STATUS_OK:
        raise SystemExit(f"flash controller not idle (status 0x{st:02X})")
    sectors = [s for s in t.sector_map() if s["addr"] < lo + len(data) and s["addr"] + s["size"] > lo]
    for s in sectors:
        for a, b, name in FORBIDDEN:
            if s["addr"] < b and a < s["addr"] + s["size"]:
                raise SystemExit(f"sector {s['index']} overlaps the {name}")
        if s["addr"] < lo or s["addr"] + s["size"] - 1 > hi:
            raise SystemExit(f"sector {s['index']} outside slot {target.upper()}")
    nblocks = len(data) // BLOCK
    print(f"slot {target.upper()} (running {running.upper()}): erase {len(sectors)} sectors, "
          f"program {nblocks} blocks, {'commit' if commit else 'no commit'}")
    if not arm:
        print("DRY RUN: nothing written (add --arm)")
        return True

    def ok(what):
        st, _ = t.status()
        if st not in STATUS_OK:
            raise SystemExit(f"ABORT: status 0x{st:02X} after {what}")

    for s in sectors:                                      # 1. erase
        t.set(R_ERASE, struct.pack("<HH", s["index"], 0))
        ok(f"erase sector {s['index']}")
    print("  erased")
    for i in range(nblocks):                               # 2. program
        addr = lo + i * BLOCK
        t.set(R_PROG, struct.pack("<I", addr) + data[i * BLOCK:(i + 1) * BLOCK])
        if i % 64 == 0:
            ok(f"program 0x{addr:08X}")
    ok("last block")
    print("  programmed")
    span = sectors[-1]["addr"] + sectors[-1]["size"] - lo  # 3. expected sector sums
    padded = data + b"\xFF" * (span - len(data))
    for s in sectors:
        off = s["addr"] - lo
        t.set(R_SUM, struct.pack("<HI", s["index"], sector_sum(padded[off:off + 0x1000])))
    ok("sector sums")
    if commit:                                             # 4. commit
        t.set(R_COMMIT, struct.pack("<I", 1))
        time.sleep(0.05)
        print(f"  committed (status 0x{t.status()[0]:02X}); slot {target.upper()} boots next")
    return True


def reboot(t, wait=120.0):
    """Same 10 bytes twice: the first is stored, the second matches -> reset."""
    t.set(R_REBOOT, REBOOT_MAGIC)
    time.sleep(0.15)
    t.set(R_REBOOT, REBOOT_MAGIC)
    t.close()
    t0, gone = time.time(), False
    while time.time() - t0 < wait:
        present = any(d.get("usage_page") == USAGE_PAGE for d in hid.enumerate(VID, PID))
        if not present:
            gone = True
        elif gone:
            time.sleep(2.0)
            return Tablet()
        time.sleep(0.05)
    raise SystemExit("tablet did not come back after reboot")


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("info", "status", "flash", "reboot"))
    ap.add_argument("pkg", nargs="?")
    ap.add_argument("--arm", action="store_true")
    ap.add_argument("--both", action="store_true")
    ap.add_argument("--no-commit", action="store_true")
    a = ap.parse_args()

    if a.cmd == "info":
        man, images = load_pkg(a.pkg)
        print(f"{man['name']}  ({man['created']})")
        for s, (e, _) in sorted(images.items()):
            print(f"  slot {s.upper()}  v{e['version']}  {e['size']} B  {e['desc']}")
        return 0

    t = Tablet()
    if a.cmd == "status":
        st1, st2 = t.status()
        print(f"version 0x{t.version:04X}, running slot {str(t.running_slot()).upper()}, "
              f"status 0xD5=0x{st1:02X} 0xD0=0x{st2:02X}")
        return 0
    if a.cmd == "reboot":
        if not a.arm:
            print("DRY RUN: would reboot (add --arm)")
            return 0
        t = reboot(t)
        print(f"back: version 0x{t.version:04X}, running slot {str(t.running_slot()).upper()}")
        return 0

    _, images = load_pkg(a.pkg)
    running = t.running_slot()
    target = "a" if running == "b" else "b"
    flash_slot(t, target, images[target][1], a.arm, commit=not a.no_commit)
    if a.both and a.arm and not a.no_commit:
        t = reboot(t)
        if t.running_slot() != target:
            raise SystemExit(f"expected to boot slot {target.upper()}; second slot NOT written")
        flash_slot(t, running, images[running][1], True)
        t = reboot(t)
    elif a.arm and not a.no_commit:
        t = reboot(t)
    print(f"now: version 0x{t.version:04X}, running slot {str(t.running_slot()).upper()}")
    t.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
