"""
Cut the per-loop pressure-action calls (steps 24 + 25 xN) on top of an image,
keeping the action's countdown consistent with the call count.

The pressure action (0x080E9F17 slot B / 0x08089D1B slot A) runs once for
step 24 and `count` times for step 25. Step 23's action seeds a countdown
[0x2001D6C4] = 4 (literal at 0x080E9DB4 / 0x08089BB8). Each call decrements
it; non-zero -> desc7 x3 and sample index [0x2001F202]++, zero -> desc8 x1 and
index = 0. Stock: 4 calls (24 + 25x3), countdown 4.

EXP-7 changed only the step-25 count (3 -> 1), so the countdown never reached 0,
the index was never reset, and the action's pblock[3 + idx] writes ran past
the pressure block into 0x2001EDA8/0x2001EDEC (calc / SPI3 request state):
the observed sensor reset loop. This keeps countdown == calls.

Usage: make_pburst.py --in IMG --slot a|b --calls N [--version 0x0171] --out PATH
  N = 2..4 (stock 4). Each call removed saves one step-25 (~3032 ticks, ~8%).
Writes NOTHING to the tablet.
"""
import argparse
import hashlib
import re
import struct
import sys

PROFILES = {
    "a": dict(base=0x08040000, table=0x0808F4E4, act=0x08089D1B, trivial=0x08062B2D),
    "b": dict(base=0x080A0000, table=0x080EF6E0, act=0x080E9F17, trivial=0x080C2B2D),
}
# e9d90 prologue: four "movs r0,#4" stores into the 0x2001D6C4 block; group 1 is
# the countdown [0x2001D6C4+0].
SEED = re.compile(rb"\x04\x20.\x49\x81\xf8\x2d\x00\x04\x20.\x49\x81\xf8\x95\x00"
                  rb"\x04\x20.\x49\x81\xf8\x94\x00.\x48\x90\xf8\x94\x00.\x49\x81"
                  rb"\xf8\x2c\x00(\x04\x20).\x49\x08\x70", re.S)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--slot", choices=("a", "b"), required=True)
    ap.add_argument("--calls", type=int, required=True)
    ap.add_argument("--version", type=lambda s: int(s, 0), default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    P = PROFILES[a.slot]
    base = P["base"]
    if not 2 <= a.calls <= 4:
        print("--calls must be 2..4")
        return 1
    blob = bytearray(open(a.src, "rb").read())

    def rec(step):
        o = P["table"] - base + 12 * step
        return o, struct.unpack_from("<II", blob, o), blob[o + 8:o + 12]

    seeds = [m.start(1) for m in SEED.finditer(blob)]
    _, (ev24, act24), _ = rec(24)
    o25, (ev25, act25), nb25 = rec(25)
    checks = [
        ("countdown seed found once", len(seeds) == 1),
        ("step 24 action is pressure action", act24 == P["act"]),
        ("step 25 action is pressure action", act25 == P["act"]),
        ("step 25 event is the trivial return-1", ev25 == P["trivial"]),
        ("step 25 next 26, count 3 (stock)", nb25[0] == 26 and nb25[3] == 3),
    ]
    ok = True
    for n, g in checks:
        print(f"  {'PASS' if g else 'FAIL'}  {n}")
        ok &= g
    if not ok:
        return 1

    s = seeds[0]
    blob[s] = a.calls
    blob[o25 + 11] = a.calls - 1
    print(f"countdown seed @ 0x{base + s:08X}: 4 -> {a.calls}")
    print(f"step 25 count @ 0x{base + o25 + 11:08X}: 3 -> {a.calls - 1}")
    if a.version is not None:
        vid = blob.find(struct.pack("<HH", 0x056A, 0x0357))
        struct.pack_into("<H", blob, vid + 4, a.version)
        print(f"bcdDevice -> 0x{a.version:04X}")
    saved = (4 - a.calls) * 3032
    loop = 36000 - saved
    print(f"estimated loop {loop} ticks = {loop * 0.138 / 1000:.2f} ms "
          f"-> ~{3 / (loop * 0.138e-6):.0f} Hz with 3 frames/loop")
    open(a.out, "wb").write(blob)
    print(f"wrote {a.out}\nsha256 {hashlib.sha256(blob).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
