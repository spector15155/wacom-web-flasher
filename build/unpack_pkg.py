"""Unpack a PTH-660 firmware package (.pkg) into its slot images (.bin) and check their sha256.

Package layout (docs/PROTOCOL.md): "PTH660PKG\\0" (10 bytes) | u32 LE manifest length n | n bytes manifest JSON |
image bytes. manifest.images[i].offset counts from the end of the manifest.

Usage: python build/unpack_pkg.py firmware/pth660_v378_1500hz.pkg [--out DIR]
Writes DIR/<package name>_slot_a.bin and _slot_b.bin (DIR defaults to the package's folder) and prints the manifest.
"""
import argparse
import hashlib
import json
import os
import struct
import sys

MAGIC = b"PTH660PKG\0"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pkg")
    ap.add_argument("--out", default=None, help="output folder (default: next to the package)")
    a = ap.parse_args()
    raw = open(a.pkg, "rb").read()
    if raw[:10] != MAGIC:
        sys.exit("not a PTH-660 package (bad magic)")
    n = struct.unpack_from("<I", raw, 10)[0]
    man = json.loads(raw[14:14 + n])
    data0 = 14 + n
    out = a.out or os.path.dirname(os.path.abspath(a.pkg))
    os.makedirs(out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(a.pkg))[0]
    print(f"{a.pkg}: {man.get('name', '')}  (created {man.get('created', '?')})")
    ok = True
    for im in man["images"]:
        blob = raw[data0 + im["offset"]:data0 + im["offset"] + im["size"]]
        sha = hashlib.sha256(blob).hexdigest()
        good = len(blob) == im["size"] and sha == im["sha256"]
        ok &= good
        path = os.path.join(out, f"{stem}_slot_{im['slot']}.bin")
        open(path, "wb").write(blob)
        print(f"  slot {im['slot'].upper()}  base {im['base']}  version {im['version']}  {im['size']} B  "
              f"sha256 {'OK' if good else 'MISMATCH'}  -> {path}")
        if im.get("desc"):
            print(f"           {im['desc']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
