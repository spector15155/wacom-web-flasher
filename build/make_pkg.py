"""
Bundle one image per slot into a single PTH-660 firmware package.

A raw .bin can only run from the slot it is linked for (absolute pointers
everywhere; no relocation info). The package carries one build per slot, and
the flasher writes whichever one the inactive slot needs.

Format (.pkg):
    b"PTH660PKG\\0"  u32 LE manifest length  manifest JSON  image bytes...
Manifest images[]: slot, base, version, size, offset, sha256, desc.

Usage:
  make_pkg.py --a IMG_A --b IMG_B --name NAME --out PATH [--desc-a D] [--desc-b D]
"""
import argparse
import hashlib
import json
import os
import struct
import sys
import time

MAGIC = b"PTH660PKG\0"
SLOTS = {"a": (0x08040000, 0x0809FFFF), "b": (0x080A0000, 0x080FFFFF)}


def check_image(data, slot):
    lo, hi = SLOTS[slot]
    sp, rv = struct.unpack_from("<II", data, 0)
    vid = data.find(struct.pack("<HH", 0x056A, 0x0357))
    errs = []
    if not 0x20000000 <= sp <= 0x20040000:
        errs.append(f"SP 0x{sp:08X} not in SRAM")
    if not (rv & 1 and lo <= (rv & ~1) <= hi):
        errs.append(f"reset 0x{rv:08X} not a Thumb address in slot {slot.upper()}")
    if len(data) > hi - lo + 1:
        errs.append("image larger than slot window")
    if vid < 0:
        errs.append("VID/PID identity block not found")
    ver = struct.unpack_from("<H", data, vid + 4)[0] if vid >= 0 else 0
    return errs, ver, rv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="image linked for slot A (0x08040000)")
    ap.add_argument("--b", required=True, help="image linked for slot B (0x080A0000)")
    ap.add_argument("--desc-a", default="v1.51 base")
    ap.add_argument("--desc-b", default="v1.52 base")
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    entries, blobs, off = [], [], 0
    for slot, path, desc in (("a", args.a, args.desc_a), ("b", args.b, args.desc_b)):
        data = open(path, "rb").read()
        errs, ver, rv = check_image(data, slot)
        for e in errs:
            print(f"  FAIL slot {slot.upper()}: {e}")
        if errs:
            return 1
        sha = hashlib.sha256(data).hexdigest()
        print(f"  slot {slot.upper()}: {os.path.basename(path)}  {len(data)} B  "
              f"v0x{ver:04X}  reset 0x{rv:08X}  sha256 {sha[:16]}")
        entries.append({"slot": slot, "base": f"0x{SLOTS[slot][0]:08X}",
                        "version": f"0x{ver:04X}", "size": len(data),
                        "offset": off, "sha256": sha, "desc": desc,
                        "source": os.path.basename(path)})
        blobs.append(data)
        off += len(data)

    manifest = json.dumps({"format": 1, "name": args.name,
                           "device": "Wacom PTH-660 056A:0357",
                           "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "images": entries}, indent=1).encode()
    with open(args.out, "wb") as f:
        f.write(MAGIC + struct.pack("<I", len(manifest)) + manifest + b"".join(blobs))
    pkg = open(args.out, "rb").read()
    print(f"wrote {args.out}  {len(pkg)} B  sha256 {hashlib.sha256(pkg).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
