"""
v1.65 + the missing 4th frame per loop, pushed only when it is consistent.

v1.65 pushes after steps 24 (set 1 done), 28 (set 2 pass a) and 29 (set 2
done). Every coordinate pass moves the reported position one step, so in a fast
stroke the step 24 report carries two passes' worth of motion (23 + 24): one
report in four is missing. v1.62 pushed after step 23 too, but that frame joins
the new set-1 pass a with the previous loop's pass b, and when the coil window
moved in between (pen crossing to other coils) the position jumped.

Step 23's event is wrapped: after the stock frame handler advances, the coil
lists of the sensor register image (0x2001D358, the program of the set-1 pass
a just measured) are compared with the same bytes at the previous loop's step
23. Unchanged window -> push the frame (as make_frame2 does). Changed -> skip
(v1.65 behaviour for that loop). Counters at STATE: +0 pushed, +4 skipped.

Usage: make_frame23.py --slot a|b --in V165.bin --out PATH [--version 0x0245]
Writes NOTHING to the tablet.
"""
import argparse
import hashlib
import struct
import sys

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB

PROFILES = {
    "a": dict(base=0x08040000, table=0x0808F4E4, frame_ev=0x0808B115, pblock=0x2001ECFC,
              memcpy=0x0805B488, ring_push=0x0805D13C, img=0x2001D358),
    "b": dict(base=0x080A0000, table=0x080EF6E0, frame_ev=0x080EB311, pblock=0x2001ED24,
              memcpy=0x080BB488, ring_push=0x080BD13C, img=0x2001D358),
}
STAGE = 0x2001B1FC
STAGE_EXTRA = 0x2001B7E5
STATE = 0x2003FA00          # +0 pushed, +4 skipped, +0x10.. saved coil lists
RANGES = ((0x17, 0x24), (0x4C, 0x59))   # coil-number lists in the register image

HOOK = """
    push {{r4, r5, r6, r7, lr}}
    mov  r4, r2
    bl   #{orig}
    subs r3, r0, #1
    cmp  r3, #1
    bhi  out
    push {{r0, r1}}
    ldr  r5, lit_img
    ldr  r6, lit_st
    movs r7, #0
{compare}
    cmp  r7, #0
    bne  skip
    ldr  r0, lit_stage
    movs r1, #3
    strb.w r1, [r0, #0x709]
    addw r1, r4, #0xa47
    movw r2, #0x605
    bl   #{memcpy}
    ldr  r0, lit_extra
    ldr  r1, lit_pblock
    movs r2, #0x1c
    bl   #{memcpy}
    ldr  r0, lit_stage
    bl   #{push}
    ldr  r0, [r6]
    adds r0, #1
    str  r0, [r6]
    b    done
skip:
    ldr  r0, [r6, #4]
    adds r0, #1
    str  r0, [r6, #4]
done:
    pop  {{r0, r1}}
out:
    pop  {{r4, r5, r6, r7, pc}}
    .align 2
lit_stage:  .word {stage}
lit_extra:  .word {extra}
lit_pblock: .word {pblock}
lit_img:    .word {img}
lit_st:     .word {st}
"""
# per byte: r0 = img[i]; r1 = saved; differ -> r7 = 1; saved = img[i]
BYTE = """    ldrb r0, [r5, #{i}]
    ldrb r1, [r6, #{s}]
    cmp  r0, r1
    it   ne
    movne r7, #1
    strb r0, [r6, #{s}]
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=("a", "b"), required=True)
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--version", type=lambda s: int(s, 0), default=None)
    a = ap.parse_args()
    P = PROFILES[a.slot]
    base = P["base"]
    blob = bytearray(open(a.src, "rb").read())
    o23 = P["table"] - base + 12 * 23
    ev23 = struct.unpack_from("<I", blob, o23)[0]
    if ev23 != P["frame_ev"]:
        raise SystemExit(f"step 23 event is 0x{ev23:08X}, expected the stock frame handler")
    if len(blob) % 0x100:
        raise SystemExit("image tail not 0x100-aligned")
    img = P["img"]          # same literal in both slots (3 references each)
    cmp_src, s = "", 0x10
    for lo, hi in RANGES:
        for i in range(lo, hi):
            cmp_src += BYTE.format(i=i, s=s)
            s += 1
    ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    cave = base + len(blob)
    src = HOOK.format(orig=hex(ev23 & ~1), memcpy=hex(P["memcpy"]), push=hex(P["ring_push"]),
                      stage=hex(STAGE), extra=hex(STAGE_EXTRA), pblock=hex(P["pblock"]),
                      img=hex(img), st=hex(STATE), compare=cmp_src)
    # every label branch 32-bit so keystone never relaxes (and mis-resolves) one
    for lbl in ("out", "skip", "done"):
        src = src.replace(f"    bhi  {lbl}\n", f"    bhi.w {lbl}\n").replace(
            f"    bne  {lbl}\n", f"    bne.w {lbl}\n").replace(f"    b    {lbl}\n", f"    b.w  {lbl}\n")
    code = bytes(ks.asm(src, cave)[0])
    calls = [int(i.op_str.lstrip("#"), 16) for i in md.disasm(code, cave) if i.mnemonic == "bl"]
    assert calls == [ev23 & ~1, P["memcpy"], P["memcpy"], P["ring_push"]], [hex(c) for c in calls]
    lits = struct.unpack_from("<5I", code, len(code) - 20)
    assert lits == (STAGE, STAGE_EXTRA, P["pblock"], img, STATE), [hex(x) for x in lits]
    blob += code + b"\xFF" * ((-len(code)) % 0x100)
    struct.pack_into("<I", blob, o23, cave | 1)
    print(f"step 23: event 0x{ev23:08X} -> hook 0x{cave | 1:08X} ({len(code)} B), "
          f"register image 0x{img:08X}, state 0x{STATE:08X}")
    if a.version is not None:
        vid = blob.find(struct.pack("<HH", 0x056A, 0x0357))
        struct.pack_into("<H", blob, vid + 4, a.version)
        print(f"bcdDevice -> 0x{a.version:04X}")
    open(a.out, "wb").write(blob)
    print(f"wrote {a.out} ({len(blob)} B) sha256 {hashlib.sha256(blob).hexdigest()[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
