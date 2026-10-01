"""
Build the C engines (tools/s2x/) into a v2.45 image.

S2 engine (s2x.c): two real positions per loop for Wacom's own calc.
  step callback  b.w -> stub: bl stock step cb (trampoline), bl s2x_step
  event callback bl dispatch -> stub: bl s2x_event(r0 = unpacked results), b.w dispatch

--output (out.c): one pen report per 1 ms HID tick, X/Y interpolated between Wacom's real per-scan positions
at now - DELAY_US (pressure / buttons / proximity from Wacom's own records). Hooks:
  ring push entry (mov r1,r0; push {r4,r5,lr}) -> stub: out_push, displaced code, b.w push+4
  calc ring pop call, calc mail put call     -> stub: out_pop / out_put, b.w original
  HID pen routine call in the report task    -> bl out_hid (calls the pen routine itself)

The C code is compiled with arm-none-eabi-gcc for Cortex-M4 and linked at the image tail; it must not need
.data/.bss or library calls (checked).

Usage: make_s2c.py --slot a|b --in V245.bin --out PATH [--version 0x0310] [--noinject] [--output --delay-us N]
Readers: tools/s2c_read.py, tools/out_read.py. Writes NOTHING to the tablet.
"""
import argparse
import hashlib
import os
import re
import struct
import subprocess
import sys
import tempfile

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB

BASES = {"a": 0x08040000, "b": 0x080A0000}
GCC_DIR = r"C:\Program Files\Arm\GNU Toolchain mingw-w64-x86_64-arm-none-eabi\bin"
SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "s2x")
FRAME_OFF = 0xA47
STEP_SIG = re.compile(rb"\x00\xb5\x8f\xb0\x04\xa8....\x9d\xf8\x11\x00\x10\xf0\x01\x00", re.S)
EVT_SIG = bytes.fromhex("00b5a5b000216846")
EVT_SIG2 = bytes.fromhex("9df8000010f00100")
PUSH_SIG = bytes.fromhex("014630b5214841f21e52")      # ring push: mov r1,r0; push {r4,r5,lr}; ldr r0,=ring
POP_SIG = bytes.fromhex("4ff4f360dff880834044")       # mov.w r0,#0x798; ldr.w r8,=calc; add r0,r8; bl pop
CALC_SIG = bytes.fromhex("064641464ff4f36200f06df82868314602f02afd")   # memcpy(mail,r8,0x798); ...; bl put
HID_PEN_SIG = re.compile(rb"\x00\x22.\x48\x01\x68\x68\x46....\x00\x98\x20\x28.\xd1\x01\x9d", re.S)

TRAMP = """
    push {{lr}}
    sub  sp, #0x3c
    b.w  #{cont}
"""
STEP_STUB = """
    push {{r4, lr}}
    bl   #{tramp}
    bl   #{fn}
    pop  {{r4, pc}}
"""
EVT_STUB = """
    push {{r0, r1, r2, r3, r4, lr}}
    bl   #{fn}
    pop  {{r0, r1, r2, r3, r4, lr}}
    b.w  #{dispatch}
"""
PUSH_STUB = """
    push {{r0, r1, r2, r3, r4, lr}}
    bl   #{fn}
    pop  {{r0, r1, r2, r3, r4, lr}}
    mov  r1, r0
    push {{r4, r5, lr}}
    b.w  #{cont}
"""
CALL_STUB = """
    push {{r0, r1, r2, r3, r4, lr}}
    bl   #{fn}
    pop  {{r0, r1, r2, r3, r4, lr}}
    b.w  #{orig}
"""


def tool(name):
    return os.path.join(GCC_DIR, f"arm-none-eabi-{name}.exe")


def compile_c(addr, defines, sources):
    d = tempfile.mkdtemp(prefix="s2c_")
    elf, binf, ld = (os.path.join(d, n) for n in ("s2x.elf", "s2x.bin", "s2x.ld"))
    open(ld, "w").write(f"""SECTIONS {{
  . = {hex(addr)};
  .text : {{ *(.text*) *(.rodata*) }}
  .data : {{ *(.data*) }}
  .bss : {{ *(.bss*) *(COMMON) }}
  /DISCARD/ : {{ *(.comment) *(.ARM.attributes) }}
}}""")
    flags = ["-mcpu=cortex-m4", "-mthumb", "-mfloat-abi=soft", "-Os", "-ffreestanding", "-fno-builtin",
             "-fno-tree-loop-distribute-patterns", "-fno-pic", "-ffunction-sections", "-Wall", "-Werror", "-Wno-unused-function"]
    flags += [f"-D{k}={v}" for k, v in defines.items()]
    objs = []
    for src in sources:
        obj = os.path.join(d, src + ".o")
        subprocess.run([tool("gcc"), *flags, "-c", os.path.join(SRC_DIR, src), "-o", obj], check=True)
        objs.append(obj)
    subprocess.run([tool("ld"), "-T", ld, "-nostdlib", "--no-undefined", *objs, "-o", elf], check=True)
    hdrs = subprocess.run([tool("objdump"), "-h", elf], check=True, capture_output=True, text=True).stdout
    for sec in (".data", ".bss"):
        m = re.search(rf"\s{re.escape(sec)}\s+([0-9a-f]+)", hdrs)
        assert not m or int(m.group(1), 16) == 0, f"{sec} not empty:\n{hdrs}"
    subprocess.run([tool("objcopy"), "-O", "binary", "-j", ".text", elf, binf], check=True)
    syms = {}
    for line in subprocess.run([tool("nm"), elf], check=True, capture_output=True, text=True).stdout.splitlines():
        v, t, n = line.split()
        syms[n] = int(v, 16)
    return open(binf, "rb").read(), syms


def resolve_output(blob, base, md):
    """ring push, calc pop / mail put call sites, HID pen routine + its caller, records buffer, cur struct"""
    def one(sig, what):
        h = [i for i in range(len(blob)) if blob.startswith(sig, i)]
        assert len(h) == 1, f"{what}: {len(h)} hits"
        return h[0]

    def bl_at(a):
        i = next(md.disasm(bytes(blob[a - base:a - base + 4]), a))
        assert i.mnemonic == "bl", (hex(a), i.mnemonic)
        return int(i.op_str.lstrip("#"), 16)

    def lit(i):
        la = ((i.address + 4) & ~3) + int(i.op_str.split("#")[1].rstrip("]"), 16)
        return struct.unpack_from("<I", blob, la - base)[0]

    r = dict(push=base + one(PUSH_SIG, "ring push"))
    r["pop_site"] = base + one(POP_SIG, "calc pop") + 10
    r["pop"] = bl_at(r["pop_site"])
    r["put_site"] = base + one(CALC_SIG, "calc mail put") + 16
    r["put"] = bl_at(r["put_site"])
    h = [m.start() for m in HID_PEN_SIG.finditer(blob)]
    assert len(h) == 1, f"HID pen routine: {len(h)} hits"
    pen = base + h[0] - 0xC
    L = list(md.disasm(bytes(blob[pen - base:pen - base + 0x60]), pen))
    assert L[0].op_str == "{r1, r2, r3, r4, r5, lr}", L[0].op_str
    # cur = literal loaded into r0 right before "movs r1, r5; mov.w r2, #0x798"
    k = next(j for j, i in enumerate(L) if i.op_str == "r1, r5" and L[j + 1].op_str == "r2, #0x798")
    r["cur"] = lit(L[k - 1])
    r["pen"] = pen
    sites = []
    for o in range(0, len(blob) - 4, 2):
        if struct.unpack_from("<H", blob, o)[0] & 0xF800 == 0xF000:
            for i in md.disasm(bytes(blob[o:o + 4]), base + o, 1):
                if i.mnemonic == "bl" and int(i.op_str.lstrip("#"), 16) == pen:
                    sites.append(i.address)
    assert len(sites) == 1, [hex(x) for x in sites]
    r["hid_site"] = sites[0]
    C = list(md.disasm(bytes(blob[sites[0] - base:sites[0] - base + 0x80]), sites[0]))
    k = next(j for j, i in enumerate(C) if i.mnemonic in ("ldr", "ldr.w") and i.op_str.startswith("r1, [pc")
             and any(x.op_str == "r2, #0x1b" for x in C[j + 1:j + 4]))
    r["records"] = lit(C[k])
    assert 0x20000000 < r["records"] < 0x20040000 and 0x20000000 < r["cur"] < 0x20040000
    # pen USB queue: "movs.w r1, #-1; ldr r0, =usbq; ldr r0, [r0]; bl alloc" after the HID call
    k = next(j for j, i in enumerate(C) if i.op_str == "r1, #-1" and C[j + 2].op_str == "r0, [r0]")
    r["usbq"] = lit(C[k + 1])
    # usbif pen send: "ldr r0, =usbq; ldrb r0, [r0, #4]; ... bl get; ... ldr r0, =buf; movs r1, r4; movs r2, #0x1b;
    # bl memcpy; ... bl free ... movs r1, #0x1b; ldr r0, =buf; bl send"
    cands = []
    for o in range(0, len(blob) - 4, 2):
        if blob[o:o + 2] != b"!":                      # movs r1, #0x1b
            continue
        B = list(md.disasm(bytes(blob[o:o + 10]), base + o))
        if len(B) < 3 or not (B[1].mnemonic in ("ldr", "ldr.w") and B[1].op_str.startswith("r0, [pc")
                              and B[2].mnemonic == "bl"):
            continue
        buf = lit(B[1])
        A = list(md.disasm(bytes(blob[o - 0x60:o]), base + o - 0x60))
        if not any(i.mnemonic in ("ldr", "ldr.w") and "pc" in i.op_str and lit(i) == r["usbq"] for i in A):
            continue
        bls = [int(i.op_str.lstrip("#"), 16) for i in A if i.mnemonic == "bl"]
        cands.append((B[2].address, int(B[2].op_str.lstrip("#"), 16), buf, bls))
    assert len(cands) == 1, [(hex(c[0]), hex(c[2])) for c in cands]
    r["send_site"], r["usb_send"], r["usb_buf"], bls = cands[0]
    r["usb_get"], r["usb_free"] = bls[-3], bls[-1]                # get, memcpy, free
    assert bls[-2] != bls[-1]

    # loop delays: "movs r0, #1; bl osDelay" near the call of (a) the function holding the HID pen call,
    # (b) the function holding the USB pen send
    def fn_start(a):
        for st in range(a - 2, a - 0x800, -2):
            i = next(md.disasm(bytes(blob[st - base:st - base + 4]), st), None)
            if i and i.mnemonic in ("push", "push.w") and "lr" in i.op_str:
                return st
        raise AssertionError(hex(a))

    def calls_to(fn):
        out = []
        for o in range(0, len(blob) - 4, 2):
            if blob[o + 1] & 0xF8 == 0xF0:
                i = next(md.disasm(bytes(blob[o:o + 4]), base + o, 1), None)
                if i and i.mnemonic == "bl" and int(i.op_str.lstrip("#"), 16) == fn:
                    out.append(i.address)
        return out

    def loop_delay(site):
        callers = calls_to(fn_start(site))
        assert len(callers) == 1, [hex(c) for c in callers]
        c = callers[0]
        best = None
        for st in range(c - 0x80, c + 0x20, 2):
            L = list(md.disasm(bytes(blob[st - base:st - base + 6]), st))
            if len(L) >= 2 and L[0].op_str == "r0, #1" and L[0].mnemonic == "movs" and L[1].mnemonic == "bl":
                if best is None or abs(L[1].address - c) < abs(best[0] - c):
                    best = (L[1].address, int(L[1].op_str.lstrip("#"), 16))
        assert best, hex(c)
        return best

    r["hid_delay_site"], d1 = loop_delay(r["hid_site"])
    r["usb_delay_site"], d2 = loop_delay(r["send_site"])
    assert d1 == d2, (hex(d1), hex(d2))
    r["os_delay"] = d1
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=("a", "b"), required=True)
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--version", type=lambda s: int(s, 0), default=None)
    ap.add_argument("--noinject", action="store_true", help="log and copy only, frame untouched")
    ap.add_argument("--phase3", action="store_true",
                    help="--output: base pushes 3 frames per loop (v1.65: steps 24/28/29); even timeline in thirds")
    ap.add_argument("--native", action="store_true",
                    help="--output: one report per real measurement (Wacom's per-measurement position), nothing else")
    ap.add_argument("--natavg", type=int, default=1, help="--native: average of the last N real measurements (1-4)")
    ap.add_argument("--minimal", action="store_true",
                    help="--output: minimal native output (s2x/nat.c): Wacom's own report for every S1 / S2 result, "
                         "step-28 result dropped; nothing else")
    ap.add_argument("--layout1", action="store_true", help="--s2norm: v3.45 pass-b layout (edge bursts on the peak)")
    ap.add_argument("--p8c", type=int, default=0, help="first power program: this many bursts (stock 4)")
    ap.add_argument("--natlog", action="store_true", help="--minimal: diagnostic log of the first 2048 results")
    ap.add_argument("--lean", action="store_true", help="compile out the diagnostics (grading, histograms, analysis log)")
    ap.add_argument("--mawin", type=int, default=None,
                    help="Wacom's moving-average window while drawing (stock 4; 3 = one loop on the v1.65 base)")
    ap.add_argument("--mahover", type=int, default=None,
                    help="Wacom's largest hover moving-average window (stock 12); = --mawin keeps the window constant")
    ap.add_argument("--mafast", action="store_true",
                    help="Wacom's moving-average window narrows at once (stock: -1 per result, slow after pen-down)")
    ap.add_argument("--natproc", action="store_true",
                    help="--native: report Wacom's fully processed position (only the timing is native)")
    ap.add_argument("--s2norm", action="store_true",
                    help="S2 layout for ratio-normalised neighbours (bursts 0/5 both on the peak)")
    ap.add_argument("--output", action="store_true", help="1 kHz output engine (tools/s2x/out.c)")
    ap.add_argument("--delay-us", dest="delay_us", type=int, default=5000,
                    help="--output: interpolation delay behind now (us)")
    ap.add_argument("--pos", choices=("perscan", "output"), default="perscan",
                    help="--output: Wacom's per-scan position (real points) or its final filtered output")
    ap.add_argument("--nos2", action="store_true", help="leave the scan stock (no S2 neighbour engine)")
    ap.add_argument("--double", action="store_true",
                    help="--output: two pen reports per tick, two per USB packet (2000/s); implies --pace")
    ap.add_argument("--pace", action="store_true", help="--output: HID / USB task loops paced to exactly 1 ms")
    ap.add_argument("--bridge-ms", dest="bridge_ms", type=int, default=40,
                    help="--output: hold the last in-range report through out-of-range blips up to this long (0 = off)")
    ap.add_argument("--lostlog", action="store_true",
                    help="--output diagnostic: log scan machine / pen-lost counters / level changes at 0x20034000")
    ap.add_argument("--pointlog", action="store_true",
                    help="--output diagnostic: log every history point to a 2048-entry RAM ring at 0x20034000")
    ap.add_argument("--predict", action="store_true",
                    help="--output: past the newest position continue along its direction (<= 4 ms)")
    a = ap.parse_args()
    base = BASES[a.slot]
    blob = bytearray(open(a.src, "rb").read())
    if len(blob) % 0x10:
        raise SystemExit("image tail not 0x10-aligned")
    ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)

    def lit(ins):
        la = ((ins.address + 4) & ~3) + int(ins.op_str.split("#")[1].rstrip("]"), 16)
        return struct.unpack_from("<I", blob, la - base)[0]

    hits = [m.start() for m in STEP_SIG.finditer(blob)]
    assert len(hits) == 1, hits
    scb = base + hits[0]
    ev = [i for i in range(len(blob) - 20) if blob.startswith(EVT_SIG, i) and blob[i + 12:i + 20] == EVT_SIG2]
    assert len(ev) == 1, ev
    evaddr = base + ev[0]
    bls = [i for i in md.disasm(bytes(blob[ev[0]:ev[0] + 0x34]), evaddr) if i.mnemonic == "bl"]
    assert len(bls) == 2
    site, dispatch = bls[1].address, int(bls[1].op_str.lstrip("#"), 16)
    lits = [lit(i) for i in md.disasm(bytes(blob[dispatch - base:dispatch - base + 0x30]), dispatch)
            if i.mnemonic in ("ldr", "ldr.w") and "pc" in i.op_str]
    step = lits[2]
    assert step == lits[1] + 1 and 0x2001E000 < step < 0x20020000, [hex(x) for x in lits]
    prev = [i for i in md.disasm(bytes(blob[site - base - 8:site - base]), site - 8)
            if i.mnemonic in ("ldr", "ldr.w") and i.op_str.startswith("r1, [pc")]
    assert len(prev) == 1
    work = lit(prev[0]) + FRAME_OFF

    defines = {"WORK": hex(work), "STEP_ADDR": hex(step)}
    if a.noinject:
        defines["NOINJECT"] = "1"
    if a.s2norm:
        defines["S2NORM"] = "1"
    if a.lean:
        defines["LEAN"] = "1"
    if a.natlog:
        defines["NATLOG"] = "1"
    if a.p8c:
        assert 1 <= a.p8c <= 4
        defines["P8C"] = str(a.p8c)
    if a.layout1:
        defines["LAYOUT1"] = "1"
    sources = ["s2x.c"]
    O = None
    if a.output:
        O = resolve_output(blob, base, md)
        defines.update(PEN_FN=hex(O["pen"]), RECORDS=hex(O["records"]), CUR_ADDR=hex(O["cur"]),
                       DELAY_US=str(a.delay_us), USBQ=hex(O["usbq"]), USB_GET=hex(O["usb_get"]),
                       USB_FREE=hex(O["usb_free"]), USB_SEND=hex(O["usb_send"]),
                       DOUBLE="1" if a.double else "0", OS_DELAY=hex(O["os_delay"]),
                       PREDICT="1" if a.predict else "0", BRIDGE_MS=str(a.bridge_ms))
        if a.pointlog:
            defines["POINTLOG"] = "1"
        if a.phase3:
            defines["PHASE3"] = "1"
        if a.native:
            defines["NATIVE"] = "1"
            defines["NATAVG"] = str(a.natavg)
            if a.natproc:
                defines["NATPROC"] = "1"
        if a.lostlog:
            defines["LOSTLOG"] = "1"
            defines["LOSTCTR"] = "0x2001F054" if a.slot == "a" else "0x2001F084"
        if a.pos == "output":
            defines["SRC_OUTPUT"] = "1"
        sources.append("nat.c" if a.minimal else "out.c")
        print("output: " + ", ".join(f"{k} 0x{v:08X}" for k, v in O.items()) + f", delay {a.delay_us} us")
    caddr = base + len(blob)
    code, syms = compile_c(caddr, defines, sources)
    blob.extend(code + b"\xFF" * ((-len(code)) % 0x10))
    fs, fe = syms["s2x_step"], syms["s2x_event"]
    print(f"C engine @ 0x{caddr:08X} ({len(code)} B): s2x_step 0x{fs:08X}, s2x_event 0x{fe:08X}; "
          f"work 0x{work:08X}, step byte 0x{step:08X}")
    # no direct calls out of the C blob (the pen routine is reached through a register)
    for i in md.disasm(code, caddr):
        if i.mnemonic in ("bl", "blx", "b.w") and i.op_str.startswith("#"):
            t = int(i.op_str[1:], 16)
            assert caddr <= t < caddr + len(code), f"C code calls out: {i.mnemonic} 0x{t:08X}"

    def emit(src, allowed):
        addr = base + len(blob)
        c = bytes(ks.asm(src, addr)[0])
        for i in md.disasm(c, addr):
            if i.mnemonic in ("bl", "b.w") and i.op_str.startswith("#"):
                t = int(i.op_str[1:], 16)
                assert addr <= t < addr + len(c) or t in allowed, f"0x{i.address:08X} -> 0x{t:08X}"
        blob.extend(c + b"\xFF" * ((-len(c)) % 0x10))
        return addr

    def patch(at, instr):
        b = bytes(ks.asm(instr, at)[0])
        assert len(b) == 4
        blob[at - base:at - base + 4] = b

    if a.nos2:
        print("S2 engine not hooked (--nos2): scan stays stock")
    else:
        tr = emit(TRAMP.format(cont=hex(scb + 4)), {scb + 4})
        ss = emit(STEP_STUB.format(tramp=hex(tr), fn=hex(fs)), {tr, fs})
        patch(scb, f"b.w #{hex(ss)}")
        es = emit(EVT_STUB.format(fn=hex(fe), dispatch=hex(dispatch)), {fe, dispatch})
        patch(site, f"bl #{hex(es)}")
        print(f"step cb 0x{scb:08X} -> stub 0x{ss:08X}; event cb bl at 0x{site:08X} -> stub 0x{es:08X} "
              f"(dispatch 0x{dispatch:08X}){' NOINJECT' if a.noinject else ''}")
    if O:
        ps = emit(PUSH_STUB.format(fn=hex(syms["out_push"]), cont=hex(O["push"] + 4)),
                  {syms["out_push"], O["push"] + 4})
        patch(O["push"], f"b.w #{hex(ps)}")
        for name, at, target in (("out_pop", O["pop_site"], O["pop"]), ("out_put", O["put_site"], O["put"])):
            st = emit(CALL_STUB.format(fn=hex(syms[name]), orig=hex(target)), {syms[name], target})
            patch(at, f"bl #{hex(st)}")
            print(f"{name}: bl at 0x{at:08X} -> stub 0x{st:08X} (then 0x{target:08X})")
        patch(O["hid_site"], f"bl #{hex(syms['out_hid'])}")
        if a.double or a.pace:
            patch(O["hid_delay_site"], f"bl #{hex(syms['out_delay_hid'])}")
            patch(O["usb_delay_site"], f"bl #{hex(syms['out_delay_usb'])}")
            print(f"paced loops: HID osDelay(1) at 0x{O['hid_delay_site']:08X}, USB at 0x{O['usb_delay_site']:08X} "
                  f"(osDelay 0x{O['os_delay']:08X})")
        if a.double:
            patch(O["send_site"], f"bl #{hex(syms['out_usb_pen'])}")
            print(f"out_usb_pen: bl at 0x{O['send_site']:08X} -> 0x{syms['out_usb_pen']:08X} (send 0x{O['usb_send']:08X},"
                  f" get 0x{O['usb_get']:08X}, free 0x{O['usb_free']:08X}, queue 0x{O['usbq']:08X}, buf 0x{O['usb_buf']:08X})")
        print(f"out_push: ring push 0x{O['push']:08X} -> stub 0x{ps:08X}; out_hid: bl at 0x{O['hid_site']:08X} "
              f"-> 0x{syms['out_hid']:08X}")
    if a.mawin is not None:
        # Wacom's position filter table (both slots, used by every pen config): byte 0 low 3 bits = moving-average
        # window while drawing / moving fast; the hover window blends from it up to byte 1 (12)
        sig = bytes.fromhex("240c00000006080c10")
        h = [i for i in range(len(blob) - len(sig)) if blob[i:i + len(sig)] == sig]
        assert len(h) == 1, f"filter table: {len(h)} hits"
        assert 2 <= a.mawin <= 7
        blob[h[0]] = (blob[h[0]] & ~7) | a.mawin
        print(f"mawin: filter table 0x{base + h[0]:08X}: 0x24 -> 0x{blob[h[0]]:02X} (drawing window {a.mawin})")
    if a.mahover is not None:
        # byte 1 of the same table: hover window upper end (the window lerps between byte 0 & 7 and this with speed,
        # moving +-1 per result; at ~400 results/s it swings, and the output alternates ~0.5x / ~1.5x speed)
        sig = bytes.fromhex("0c00000006080c10")
        h = [i for i in range(len(blob) - len(sig)) if blob[i:i + len(sig)] == sig]
        assert len(h) == 1, f"hover window byte: {len(h)} hits"
        assert 1 <= a.mahover <= 12
        blob[h[0]] = a.mahover
        print(f"mahover: filter table 0x{base + h[0]:08X}: 0x0C -> 0x{a.mahover:02X}")
    if a.mafast:
        # moving average 0x080BC0BC (B): "cmp r2, r4; it lt; sublt r4, r4, #1" -> "movlt r4, r2": the window jumps down
        # to its target (pen-down after hover no longer averages ~8 extra results); widening stays +1 per result
        sig = bytes.fromhex("a242b8bf641e")
        h = [i for i in range(len(blob) - len(sig)) if blob[i:i + len(sig)] == sig]
        assert len(h) == 1, f"window slew: {len(h)} hits"
        blob[h[0] + 4:h[0] + 6] = bytes.fromhex("1446")
        print(f"mafast: window slew 0x{base + h[0] + 4:08X}: sublt r4, r4, #1 -> movlt r4, r2")
    if a.version is not None:
        vid = blob.find(struct.pack("<HH", 0x056A, 0x0357))
        struct.pack_into("<H", blob, vid + 4, a.version)
        print(f"bcdDevice -> 0x{a.version:04X}")
    blob += b"\xFF" * ((-len(blob)) % 0x100)
    open(a.out, "wb").write(blob)
    print(f"wrote {a.out} ({len(blob)} B) sha256 {hashlib.sha256(blob).hexdigest()[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
