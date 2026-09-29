"""
Insert K extra coordinate-only cycles (S1 + N x S2, no pressure readout) after
every stock tracking loop, on top of a v1.65 image.

Pipeline model (event of step s consumes the measurement programmed by the
action of the step before it):

  step  event          consumes            action
  23    F  (S1)        eaf65 (S1 pass a)   e9e05 (S1 pass b)
  24    F  (S1 done)   e9e05               e9f17 (pressure 1)
  25x3  stub           pressure 1..3       e9f17 (pressure 2..4)
  26    eb599 (S1 arm) pressure 4          ea2d1 (S2 arm + burst)
  27    stub           ea2d1               ea39d (S2 pass a)
  28    S2 [+push]     ea39d               eaef1 (S2 pass b)
  29    S2 [+push]     eaef1 (S2 done)     eaf65 (S1 pass a)

Extra cycle (new table records 44+), N = --s2:
  23'   F              eaf65               e9e05
  24'   EV_F           e9e05 (S1 done)     ACT_S2 = ea2d1 state, then ea39d
  28'   S2 [+push]     ea39d               eaef1
  29'   S2 [+push]     eaef1 (S2 done)     ACT_S2 (another S2) or eaf65
  ...                                      -> next 23' or stock 23

EV_F re-arms S1 (calls the step-26 event, which reloads the push countdown
[0x2001D86C]) and then runs the stock frame handler, so every S1 completion
takes the full stock path. Stock step 24 gets EV_F too, because extra cycles
consume the countdown it relies on. The table is relocated to the image tail
(the machine descriptor's pointer is patched).

--hold      Pressure is only measured correctly by an S2 that directly follows
            the pressure bursts (frames pushed after stock 28/29). Every push
            tags stage byte 0x700 (0 fresh / 1 stale); a hook before the calc
            task sends its output replaces pressure, contact level and button
            bits of stale frames with the last fresh ones.
--drain     calc task processes every pending ring frame per tick (the 3-slot
            ring otherwise drops frames pushed close together).
--hiddrain  HID task keeps only the newest calc result per tick (measured:
            loses too many frames, 719 Hz; kept for reference).
--mailq N   depth of the calc -> HID mail queue (stock 5). With production just
            above 1000/s the queue sits full; a depth of 2 bounds that lag.

Usage: make_cycles.py --in IMG --slot a|b --k N [--s2 N] [--hold] [--drain]
                      [--hiddrain] [--version 0x0190] --out PATH
Writes NOTHING to the tablet.
"""
import argparse
import hashlib
import re
import struct
import sys

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB

PROFILES = {
    "a": dict(base=0x08040000, table=0x0808F4E4, frame_ev=0x0808B115, set2_ev=0x0808A4A9,
              pblock=0x2001ECFC, memcpy=0x0805B488, ring_push=0x0805D13C),
    "b": dict(base=0x080A0000, table=0x080EF6E0, frame_ev=0x080EB311, set2_ev=0x080EA6A5,
              pblock=0x2001ED24, memcpy=0x080BB488, ring_push=0x080BD13C),
}
NREC = 44            # records 0..43 in the stock machine-4 table
TRIVIAL_ACT_NEXT = {23: 24, 24: 25, 26: 27, 27: 28, 28: 29, 29: 23}

EV_F = """
    push {{r0, r1, r2, lr}}
    bl   #{arm_s1}
    pop  {{r0, r1, r2, lr}}
    b.w  #{frame}
"""
EV_F_TAG = """
    push {{r0, r1, r2, lr}}
    bl   #{arm_s1}
    pop  {{r0, r1, r2, lr}}
    ldr  r3, lit_ring
    ldrh r3, [r3]
    ldr  r12, lit_arr
    add  r3, r12
    mov.w r12, #{val}
    strb.w r12, [r3]
    b.w  #{frame}
    .align 2
lit_ring: .word {ring}
lit_arr:  .word {arr}
"""
TAG_JMP = """
    ldr  r3, lit_ring
    ldrh r3, [r3]
    ldr  r12, lit_arr
    add  r3, r12
    mov.w r12, #{val}
    strb.w r12, [r3]
    b.w  #{target}
    .align 2
lit_ring: .word {ring}
lit_arr:  .word {arr}
"""
# calc's ring pop: note the tag of the slot about to be popped, then pop
POP_TAG = """
    ldr  r1, lit_ring
    ldrh r2, [r1, #2]
    ldrh r3, [r1]
    cmp  r2, r3
    beq  go
    cmp  r2, #2
    bhi  go
    ldr  r1, lit_arr
    ldrb r3, [r1, r2]
    strb r3, [r1, #3]
    movs r3, #0
    strb r3, [r1, r2]
    {stamp}
go:
    b.w  #{pop}
    .align 2
lit_ring: .word {ring}
lit_arr:  .word {arr}
{stamp_lits}
"""
# --nopushx: extra-cycle frames never reach the calc ring (plan R3: Wacom's calc sees only
# the stock loop, so pressure / buttons / contact / hover stay stock). The extra S1-done
# event runs the stock frame handler (scan-side tracking still sees every scan) with NPX
# set; the ring push wrapper drops any push while NPX is set. Extra S2 events use the
# plain S2 handler (no v1.65 push wrapper).
NPX = 0x2003F7E0
EV_FNP = """
    push {{r4, lr}}
    push {{r0, r1, r2}}
    bl   #{arm_s1}
    pop  {{r0, r1, r2}}
    ldr  r4, lit_npx
    movs r3, #1
    strb r3, [r4]
    bl   #{frame}
    movs r3, #0
    strb r3, [r4]
    ldr  r3, [r4, #4]
    adds r3, #1
    str  r3, [r4, #4]
    pop  {{r4, pc}}
    .align 2
lit_npx: .word {npx}
"""
# --nopushx also keeps the stock frames' S2 data post-P: the extra cycle's S2 handler
# overwrites the work frame's S2 blocks (X +0x168.. incl. entries and the decoded pen word
# at +0x39E, Y .. +0x5E5 incl. +0x5DD), and the next stock frames (23, 24) are built from
# it, so they carried extra-cycle S2 (v2.94: pressure stuck at the bogus 533). Stock 29
# saves the block after its handler; the extra 29' restores it after the plain handler.
S2KEEP = 0x2003C800          # 0x47E B block + u32 valid at +0x480 (stock references
                             # 0x2003E000.. -> v2.95 IWDG restarts; nothing references 0x2003C...)
S2KEEP_N = 0x47E
S2_SAVE = """
    push {{r4, lr}}
    mov  r4, r2
    bl   #{orig}
    push {{r0, r1}}
    ldr  r0, lit_keep
    addw r1, r4, #0xbaf
    movw r2, #{n}
    bl   #{memcpy}
    ldr  r0, lit_keep
    movs r1, #1
    str.w r1, [r0, #0x480]
    pop  {{r0, r1}}
    pop  {{r4, pc}}
    .align 2
lit_keep: .word {keep}
"""
S2_REST = """
    push {{r4, lr}}
    mov  r4, r2
    bl   #{orig}
    push {{r0, r1}}
    ldr  r1, lit_keep
    ldr.w r0, [r1, #0x480]
    cmp  r0, #1
    bne  skip
    addw r0, r4, #0xbaf
    movw r2, #{n}
    bl   #{memcpy}
skip:
    pop  {{r0, r1}}
    pop  {{r4, pc}}
    .align 2
lit_keep: .word {keep}
"""
# --btnhold N (with --nopushx): v2.95 recording: a held side button reads on for 3 reports,
# off for 2, every loop (the two stock frames after the extra cycle lose it). On the calc's
# mail copy only (r6; r8 untouched): a nonzero button value is kept through up to N results
# that read 0; out of range (level 0) clears it. State BTNHOLD: +0 held bits, +1 countdown.
BTNHOLD = 0x2003F7F0
BTNHOLD_HOOK = """
    push {{r4, lr}}
    ldr  r4, lit_bh
    ldrb.w r0, [r6, #0x794]
    cmp  r0, #0
    bne  inr
    strb r0, [r4, #1]
    b    out
inr:
    ldrb r0, [r6, #2]
    ands r1, r0, #7
    beq  zero
    strb r1, [r4]
    movs r2, #{n}
    strb r2, [r4, #1]
    b    out
zero:
    ldrb r2, [r4, #1]
    cmp  r2, #0
    beq  out
    subs r2, #1
    strb r2, [r4, #1]
    ldrb r1, [r4]
    bic  r0, r0, #7
    orrs r0, r1
    strb r0, [r6, #2]
out:
    ldr  r0, [r5]
    mov  r1, r6
    pop  {{r4, pc}}
    .align 2
lit_bh: .word {bh}
"""
PUSH_TRAMP_NPX = """
    mov  r1, r0
    push {{r4, r5, lr}}
    b.w  #{cont}
"""
PUSH_GATE_NPX = """
    ldr  r3, lit_npx
    ldrb r2, [r3]
    cmp  r2, #0
    bne  drop
    b.w  #{tramp}
drop:
    ldr  r2, [r3, #8]
    adds r2, #1
    str  r2, [r3, #8]
    movs r0, #0
    bx   lr
    .align 2
lit_npx: .word {npx}
"""
ACT_S2 = """
    push {{r4, r5, r6, lr}}
    mov  r4, r0
    mov  r5, r1
    bl   #{arm_s2}
    mov  r0, r4
    mov  r1, r5
    pop  {{r4, r5, r6, lr}}
    b.w  #{s2a}
"""
# Replaces "ldr r0, [r5]; mov r1, r6" right before the calc task puts its
# output mail (r6 = mail copy of the output struct; r8 = the calc's own live
# struct, which is never modified). Output fields: u16 pressure @+0, button bits
# @+2 [2:0], pen level @+0x794 (3 = contact); r8+0x798 = input frame copy.
# Stale frames read a fixed ~531 pressure and so claim contact; the mail gets
# level, pressure and buttons of the last fresh frame instead.
CALC_HOLD = """
    ldr  r1, lit_arr
    ldrb r2, [r1, #3]
    cmp  r2, #1
    it   hi
    movhi r2, #1
    add.w r3, r1, r2, lsl #1
    ldrh r0, [r3, #10]
    adds r0, #1
    strh r0, [r3, #10]
    ldrb.w r3, [r6, #0x794]
    cmp  r3, #3
    bhi  done
    cbz  r2, fresh
    cmp  r3, #2
    blo  done
    ldrb r2, [r1, #6]
    cmp  r2, #2
    blo  done
    cmp  r2, #3
    bhi  done
    strb.w r2, [r6, #0x794]
    ldrh r3, [r1, #8]
    strh r3, [r6, #8]
    ldrb r3, [r6, #2]
    bic  r3, r3, #7
    ldrb r0, [r1, #7]
    orrs r3, r0
    strb r3, [r6, #2]
    movs r3, #0
    cmp  r2, #3
    it   eq
    ldrheq r3, [r1, #4]
    strh r3, [r6]
    b    done
fresh:
    strb r3, [r1, #6]
    ldrh r3, [r6]
    strh r3, [r1, #4]
    ldrh r3, [r6, #8]
    strh r3, [r1, #8]
    ldrb r3, [r6, #2]
    and  r3, r3, #7
    strb r3, [r1, #7]
done:
    {unfreeze_call}
    ldr  r0, [r5]
    mov  r1, r6
    bx   lr
    .align 2
lit_arr: .word {arr}
"""
# Hold v4, on the mail copy only (r6; the calc's live struct r8 and its input
# frame are never touched). Tag of the frame comes from the side table via
# POP_TAG. Output fields: u16 pressure +0, buttons +2[2:0], u16 hover +8,
# level +0x794 (2 hover, 3 contact; other values = non-pen record, untouched).
# debug: observe-only mail hook - append (tag, level, u16 pressure) of every
# calc result to a 256-entry log at LOG (index u32 at LOG+0x400)
CALC_LOG = """
    ldr  r1, lit_arr
    ldrb r2, [r1, #3]
    ldr  r1, lit_log
    ldr  r3, [r1, #0x800]
    and  r3, r3, #0xff
    add.w r0, r1, r3, lsl #3
    strb r2, [r0]
    ldrb.w r2, [r6, #0x794]
    strb r2, [r0, #1]
    ldrh r2, [r6]
    strh r2, [r0, #2]
    ldrh r2, [r6, #0x1c]
    strh r2, [r0, #4]
    ldrh.w r2, [r6, #0x54]
    strh r2, [r0, #6]
    adds r3, #1
    str  r3, [r1, #0x800]
    ldr  r0, [r5]
    mov  r1, r6
    bx   lr
    .align 2
lit_arr: .word {arr}
lit_log: .word {log}
"""
# debug: observe-only mail hook - 256 x (tag, level, u16 pressure, u16 x, u16 y)
# at LOG, index u32 at LOG+0x800. With --log the push tags are the frame's slot
# in the loop (1 = stock 24, 2 = 28, 3 = 29, 4.. = extra frames in order).
LOG = 0x2003E000

# --fixpa: S2 pass a (blocks 0..6 = frame +0x16E X / +0x3AD Y, 10 B each) is the
# pen phase/pressure measurement; only valid right after the pressure bursts.
# PA_SAVE wraps the stock step-28 event (post-P pass a) and saves those entries;
# PA_FIX replaces the extra cycles' S2 pass-a event: plain S2 handler, restore the
# saved entries into the frame, then (optional tag write and) the stock push.
PA_SAVE = """
    push {{r4, r5, lr}}
    sub  sp, #4
    mov  r4, r2
    bl   #{inner}
    mov  r5, r0
    ldr  r0, lit_save
    addw r1, r4, #0xbb5
    movs r2, #70
    bl   #{memcpy}
    ldr  r0, lit_save
    adds r0, #72
    addw r1, r4, #0xdf4
    movs r2, #70
    bl   #{memcpy}
    ldr  r0, lit_save
    movs r1, #1
    strb.w r1, [r0, #144]
    mov  r0, r5
    add  sp, #4
    pop  {{r4, r5, pc}}
    .align 2
lit_save: .word {save}
"""
PA_FIX = """
    push {{r4, r5, lr}}
    sub  sp, #4
    mov  r4, r2
    bl   #{s2ev}
    mov  r5, r0
    ldr  r1, lit_save
    ldrb.w r0, [r1, #144]
    cbz  r0, nosave
    addw r0, r4, #0xbb5
    movs r2, #70
    bl   #{memcpy}
    ldr  r1, lit_save
    adds r1, #72
    addw r0, r4, #0xdf4
    movs r2, #70
    bl   #{memcpy}
nosave:
    subs r3, r5, #1
    cmp  r3, #1
    bhi  out
    {tagcode}
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
out:
    mov  r0, r5
    add  sp, #4
    pop  {{r4, r5, pc}}
    .align 2
lit_save:   .word {save}
lit_stage:  .word 0x2001B1FC
lit_extra:  .word 0x2001B7E5
lit_pblock: .word {pblock}
{taglits}
"""
PA_TAG = """ldr  r3, lit_ring
    ldrh r3, [r3]
    ldr  r2, lit_arr
    add  r3, r2
    movs r2, #{val}
    strb r2, [r3]"""
PA_TAGLITS = """lit_ring:   .word {ring}
lit_arr:    .word {arr}"""
SAVEA = 0x2003FE00      # 70 B X + pad + 70 B Y, valid flag at +144

CALC_DRAIN = """
    push {{r4, lr}}
    movs r4, #4
again:
    bl   #{calc}
    ldr  r0, lit_ring
    ldrh r1, [r0]
    ldrh r2, [r0, #2]
    cmp  r1, r2
    beq  out
    subs r4, #1
    bne  again
out:
    pop  {{r4, pc}}
    .align 2
lit_ring: .word {ring}
"""
# --unfreeze: on the mail copy, a pen report whose position is an exact repeat of
# the previous one while the pen was moving (|v| >= 4 counts/frame) gets the
# previous output + the last per-frame velocity instead (max 3 in a row). The
# calc's stabilizer holds the cursor 1-3 frames once per loop; real samples pass
# through unchanged. State at UNF: rx, ry, ox, oy, vx, vy, valid(0x5A), frozen.
UNF = 0x2003FEA0
UNFREEZE = """
    push {{r4, r5, r7, lr}}
    ldr  r7, lit_unf
    ldrb.w r0, [r6, #0x794]
    cmp  r0, #2
    blo  reset
    cmp  r0, #3
    bhi  out
    ldr  r0, [r6, #0x1c]
    ldr  r1, [r6, #0x54]
    ldr  r2, [r7, #0]
    ldr  r3, [r7, #4]
    ldr  r4, [r7, #24]
    cmp  r4, #0x5a
    bne  first
    ldr  r4, [r7, #28]
    cmp  r4, #3
    bhi  first
    cmp  r0, r2
    bne  moved
    cmp  r1, r3
    bne  moved
    cmp  r4, #3
    bhs  out
    ldr  r2, [r7, #16]
    ldr  r3, [r7, #20]
    eor  r0, r2, r2, asr #31
    sub  r0, r0, r2, asr #31
    eor  r1, r3, r3, asr #31
    sub  r1, r1, r3, asr #31
    add  r0, r1
    cmp  r0, #4
    blt  out
    ldr  r0, [r7, #8]
    add  r0, r2
    str  r0, [r7, #8]
    str  r0, [r6, #0x1c]
    ldr  r1, [r7, #12]
    add  r1, r3
    str  r1, [r7, #12]
    str  r1, [r6, #0x54]
    adds r4, #1
    str  r4, [r7, #28]
    b    out
moved:
    adds r4, #1
    subs r5, r0, r2
    .short 0xfb95
    .short 0xf5f4
    str  r5, [r7, #16]
    subs r5, r1, r3
    .short 0xfb95
    .short 0xf5f4
    str  r5, [r7, #20]
    b    keep
first:
    movs r4, #0
    str  r4, [r7, #16]
    str  r4, [r7, #20]
keep:
    str  r0, [r7, #0]
    str  r1, [r7, #4]
    str  r0, [r7, #8]
    str  r1, [r7, #12]
    movs r4, #0
    str  r4, [r7, #28]
    movs r4, #0x5a
    str  r4, [r7, #24]
    b    out
reset:
    movs r4, #0
    str  r4, [r7, #24]
out:
    pop  {{r4, r5, r7, pc}}
    .align 2
lit_unf: .word {unf}
"""

# --phasefix: S2 entries (10 B; X at frame+0x16E, Y at +0x3AD, 14 each) hold
# amplitude (+0, position) and phase (+2/+4, +8 -> pressure/contact). Phase is
# only right when S2 directly follows the pressure bursts. PH_SAVE wraps the stock
# step-29 event (post-P S2 complete) and saves all entries; PH_FIX replaces the
# extra cycles' S2 events: plain S2 handler, copy saved phase fields (+2..+5,
# +8..+9) into every entry, keep the fresh amplitude, then the stock push.
PHSAVE = 0x2003FC00     # 140 B X, 140 B Y, valid flag at +288
PH_SAVE = """
    push {{r4, r5, lr}}
    sub  sp, #4
    mov  r4, r2
    bl   #{inner}
    mov  r5, r0
    ldr  r0, lit_save
    addw r1, r4, #0xbb5
    movs r2, #140
    bl   #{memcpy}
    ldr  r0, lit_save
    adds r0, #144
    addw r1, r4, #0xdf4
    movs r2, #140
    bl   #{memcpy}
    ldr  r0, lit_save
    movs r1, #1
    str.w r1, [r0, #288]
    mov  r0, r5
    add  sp, #4
    pop  {{r4, r5, pc}}
    .align 2
lit_save: .word {save}
"""
PH_FIX = """
    push {{r4, r5, r6, lr}}
    mov  r4, r2
    bl   #{s2ev}
    mov  r5, r0
    subs r3, r5, #1
    cmp  r3, #1
    bhi  out
    ldr  r0, lit_save
    ldr.w r1, [r0, #288]
    cbz  r1, push
    addw r1, r4, #0xbb5
    movs r6, #28
ent:
    ldr  r2, [r0, #2]
    str  r2, [r1, #2]
    ldrh r2, [r0, #8]
    strh r2, [r1, #8]
    adds r0, #10
    adds r1, #10
    subs r6, #1
    cmp  r6, #14
    bne  noy
    ldr  r0, lit_save
    adds r0, #144
    addw r1, r4, #0xdf4
noy:
    cmp  r6, #0
    bne  ent
push:
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
out:
    mov  r0, r5
    pop  {{r4, r5, r6, pc}}
    .align 2
lit_save:   .word {save}
lit_stage:  .word 0x2001B1FC
lit_extra:  .word 0x2001B7E5
lit_pblock: .word {pblock}
"""

# --rawhold: the calc filters raw pressure ([r7+4], r7 = calc+0x112A) through a
# median-of-3 (0x080BD234, state 0x200005CC+0x14) and derives contact from the
# result. Hook at the filter call (replaces "add.w r0, r6, #0x14", r1 = raw
# pressure): frames tagged fresh (raw real) save r1, stale frames get the saved
# value, so the calc's pressure/contact chain never sees the fake pressure.
RAWSAVE = 0x2003FF60     # +0 u16 pressure, +2 buttons, +3 proximity, +0x0C..0x15 pen ID
RAW_SIG = bytes.fromhex("b98806f11400")        # ldrh r1,[r7,#4]; add.w r0,r6,#0x14
RAW_HOOK = """
    {rawlog}
    ldr  r2, lit_arr
    ldrb r3, [r2, #19]
    cbnz r3, done
    ldrb r3, [r2, #3]
    ldr  r2, lit_raw
    cbnz r3, stale
    strh r1, [r2]
    ldrb r3, [r7, #3]
    strb r3, [r2, #3]
    ldrb r3, [r7, #6]
    and  r3, r3, #7
    strb r3, [r2, #2]
    movs r0, #0x0c
save:
    ldrb r3, [r7, r0]
    strb r3, [r2, r0]
    adds r0, #1
    cmp  r0, #0x16
    blt  save
    b    done
stale:
    ldrh r1, [r2]
    ldrb r3, [r2, #3]
    strb r3, [r7, #3]
    ldrb r3, [r7, #6]
    bic  r3, r3, #7
    ldrb r0, [r2, #2]
    orrs r3, r0
    strb r3, [r7, #6]
    movs r0, #0x0c
rest:
    ldrb r3, [r2, r0]
    strb r3, [r7, r0]
    adds r0, #1
    cmp  r0, #0x16
    blt  rest
done:
    add.w r0, r6, #0x14
    bx   lr
    .align 2
lit_arr: .word {arr}
lit_raw: .word {raw}
{rawlog_lits}
"""
# --pressfix (v2.27): a side-button press corrupts the pressure readout (+-512 bit
# errors) and the scan keeps running extra cycles for up to ~2.8 ms before the
# stock loop takes over. v2.22's raw hold saved that one bad reading and fed it
# to every frame of the loop, so the calc's median-of-3 passed it (pressure
# spike), and it was never updated in button mode (stale value on release).
#   stock mode (ARR+19): fresh frames are saved, nothing is restored
#   switch window (ARR+18 armed, ARR+19 clear): every frame gets the held input
#   k8: as before (fresh saves, stale restores)
# The previous fresh pressure is kept at RAWSAVE+0x16; CALC_HOLD6 puts it back
# when a press is first seen (the reading that showed the press is suspect).
RAW_HOOK2 = """
    {rawlog}
    ldr  r2, lit_arr
    ldrb r0, [r2, #3]
    ldrb r3, [r2, #19]
    cmp  r3, #0
    beq  k8
    cmp  r0, #0
    bne  done
    b    fresh
k8:
    ldrb r3, [r2, #18]
    cmp  r3, #0
    bne  stale
    cmp  r0, #0
    bne  stale
fresh:
    ldr  r2, lit_raw
    ldrh r3, [r2]
    strh r3, [r2, #0x16]
    strh r1, [r2]
    ldrb r3, [r7, #3]
    strb r3, [r2, #3]
    ldrb r3, [r7, #6]
    and  r3, r3, #7
    strb r3, [r2, #2]
    movs r0, #0x0c
save:
    ldrb r3, [r7, r0]
    strb r3, [r2, r0]
    adds r0, #1
    cmp  r0, #0x16
    blt  save
    b    done
stale:
    ldr  r2, lit_raw
    ldrh r1, [r2]
    ldrb r3, [r2, #3]
    strb r3, [r7, #3]
    ldrb r3, [r7, #6]
    bic  r3, r3, #7
    ldrb r0, [r2, #2]
    orrs r3, r0
    strb r3, [r7, #6]
    movs r0, #0x0c
rest:
    ldrb r3, [r2, r0]
    strb r3, [r7, r0]
    adds r0, #1
    cmp  r0, #0x16
    blt  rest
done:
    add.w r0, r6, #0x14
    bx   lr
    .align 2
lit_arr: .word {arr}
lit_raw: .word {raw}
{rawlog_lits}
"""
# --rawsimple: the v2.10/v2.11 raw hold (pressure only, no button-mode bypass).
# The later hooks skip themselves while ARR+19 ("button mode") is set; that byte
# lives in no-init RAM and survives reflashing, so builds without --btnswitch
# must not use them (v2.33-v2.35 ran with a stale ARR+19 = 1: holds off).
RAW_HOOK_SIMPLE = """
    ldr  r2, lit_arr
    ldrb r3, [r2, #3]
    ldr  r2, lit_raw
    cbnz r3, stale
    strh r1, [r2]
    b    done
stale:
    ldrh r1, [r2]
done:
    add.w r0, r6, #0x14
    bx   lr
    .align 2
lit_arr: .word {arr}
lit_raw: .word {raw}
"""
# debug (--rawlog): before the hold, log [tag, calc+0x112A .. +0x148] per frame,
# 256 x 32 B at RAWLOG (index u32 at RAWLOG-4)
RAWLOG = 0x20034000
RAWLOG_CODE = """push {r4, r5}
    ldr  r2, lit_arr
    ldrb r3, [r2, #3]
    ldr  r4, lit_log
    ldr  r5, [r4, #-4]
    and  r0, r5, #0xff
    add.w r0, r4, r0, lsl #5
    strb r3, [r0]
    adds r0, #1
    movs r2, #0
cp:
    ldrb r3, [r7, r2]
    strb r3, [r0, r2]
    adds r2, #1
    cmp  r2, #31
    blt  cp
    adds r5, #1
    str  r5, [r4, #-4]
    pop  {r4, r5}"""
# The per-pen input struct r7 (calc+0x112A) is filled from the frame before this
# point. On stale frames its pen-state fields are wrong (per-frame log, v2.1A):
# +0/+1 flags, +3 proximity, +4 raw pressure, +6 buttons, +0x0C..0x15 pen ID
# (zeroed on some stale frames with a side button held). Fresh frames save
# only the raw inputs: +3 proximity (the calc copies it to state and treats a
# <2 -> >=2 rise as pen arrival: 5-frame pressure zeroing), +4 raw pressure,
# +6[2:0] buttons, +0x0C..0x15 pen ID. v2.13 also restored +0/+1 and other
# calc-owned bytes and drifted after boot.

# --mailhold (with --rawhold): output hold on the mail copy for pen state the
# raw hold cannot fix (side buttons shift the pen signal, stale frames then
# report level 0/1, tilt 0, hover 63). Fresh (tag 0) results save level, button
# bits, hover, tilt x/y; stale results in or near range get them while the last
# fresh frame was in range (level 2/3). Pressure: calc's own (raw-held) value,
# forced 0 when the held level is hover. State: ARR+4.. (see CALC_HOLD5).
# v2.21+: the hold bypasses while the stock loop runs (ARR+19). v2.23/v2.24's
# switch-window position freeze was worse and has been removed.
CALC_HOLD5 = """
    {nullguard}
    ldr  r1, lit_arr
    ldrb.w r3, [r6, #0x794]
    cmp  r3, #3
    bhi  arm_done
    ldrb r2, [r6, #2]
    tst  r2, #3
    bne  arm
    cmp  r3, #1
    bne  arm_done
arm:
    movs r2, #16
    strb r2, [r1, #18]
arm_done:
    ldrb r2, [r1, #19]
    cbnz r2, done
    ldrb r2, [r1, #3]
    cmp  r3, #3
    bhi  done
    cbnz r2, stale
    strb r3, [r1, #6]
    ldrb r3, [r6, #2]
    and  r3, r3, #7
    strb r3, [r1, #7]
    ldrh r3, [r6, #8]
    strh r3, [r1, #8]
    ldrh.w r3, [r6, #0x46]
    strh r3, [r1, #14]
    ldrh.w r3, [r6, #0x7e]
    strh r3, [r1, #16]
    b    done
stale:
    ldrb r2, [r1, #6]
    cmp  r2, #2
    blo  done
    cmp  r2, #3
    bhi  done
    strb.w r2, [r6, #0x794]
    ldrb r3, [r6, #2]
    bic  r3, r3, #7
    ldrb r0, [r1, #7]
    orrs r3, r0
    strb r3, [r6, #2]
    ldrh r3, [r1, #8]
    strh r3, [r6, #8]
    ldrh r3, [r1, #14]
    strh.w r3, [r6, #0x46]
    ldrh r3, [r1, #16]
    strh.w r3, [r6, #0x7e]
    cmp  r2, #3
    beq  done
    movs r3, #0
    strh r3, [r6]
done:
    {stamp_call}
    ldr  r0, [r5]
    mov  r1, r6
    bx   lr
    .align 2
lit_arr: .word {arr}
"""

# --protect: pen-lost counters at 0x2001F084 ([0] invalid S1 completions,
# [1]/[2] S2 status; any reaching 10 makes the handler return "reset" and the
# machine re-acquires). Stock feeds them from one loop's scans; the extra cycles
# (with a side button held the pen signal shifts and extra scans fail) would
# trip them. Each extra-cycle event runs under a wrapper that restores [0..2].
LOSTCNT = 0x2001F084
# --pressfix mail hold: CALC_HOLD5 plus
#   - first sight of a press reverts the held raw pressure (RAW_HOOK2)
#   - stock mode: results in hover/contact are remembered, nothing is changed
#   - switch window (armed, extra cycles still running): every result gets the
#     last good level/hover/tilt/pressure (ARR+4; 0 if hover), keeping its own buttons
#   - level 0/1 results are never remembered as the good pen state
CALC_HOLD6 = """
    {nullguard}
    ldr  r1, lit_arr
    ldrb.w r3, [r6, #0x794]
    cmp  r3, #3
    bhi  arm_done
    ldrb r2, [r6, #2]
    tst  r2, #3
    bne  arm
    cmp  r3, #1
    bne  arm_done
arm:
    ldrb r2, [r1, #18]
    cmp  r2, #0
    bne  rearm
    ldr  r0, lit_raw
    ldrh r2, [r0, #0x16]
    strh r2, [r0]
rearm:
    movs r2, #16
    strb r2, [r1, #18]
arm_done:
    cmp  r3, #3
    bhi  done
    ldrb r2, [r1, #19]
    cmp  r2, #0
    beq  k8
    cmp  r3, #2
    blo  done
    b    save
k8:
    ldrb r2, [r1, #18]
    cmp  r2, #0
    bne  window
    ldrb r2, [r1, #3]
    cmp  r2, #0
    bne  stale
    cmp  r3, #2
    blo  done
save:
    strb r3, [r1, #6]
    ldrh r0, [r6]
    strh r0, [r1, #4]
    ldrb r3, [r6, #2]
    and  r3, r3, #7
    strb r3, [r1, #7]
    ldrh r3, [r6, #8]
    strh r3, [r1, #8]
    ldrh.w r3, [r6, #0x46]
    strh r3, [r1, #14]
    ldrh.w r3, [r6, #0x7e]
    strh r3, [r1, #16]
    b    done
window:
    ldrb r2, [r1, #6]
    cmp  r2, #2
    blo  done
    cmp  r2, #3
    bhi  done
    ldrh r3, [r1, #4]
    strh r3, [r6]
    b    apply
stale:
    ldrb r2, [r1, #6]
    cmp  r2, #2
    blo  done
    cmp  r2, #3
    bhi  done
    ldrb r3, [r6, #2]
    bic  r3, r3, #7
    ldrb r0, [r1, #7]
    orrs r3, r0
    strb r3, [r6, #2]
apply:
    strb.w r2, [r6, #0x794]
    ldrh r3, [r1, #8]
    strh r3, [r6, #8]
    ldrh r3, [r1, #14]
    strh.w r3, [r6, #0x46]
    ldrh r3, [r1, #16]
    strh.w r3, [r6, #0x7e]
    cmp  r2, #3
    beq  done
    movs r3, #0
    strh r3, [r6]
done:
    {stamp_call}
    ldr  r0, [r5]
    mov  r1, r6
    bx   lr
    .align 2
lit_arr: .word {arr}
lit_raw: .word {raw}
"""
PROTECT = """
    push {{r4, r5, r6, lr}}
    ldr  r4, lit_cnt
    ldr  r5, [r4]
    bl   #{inner}
    ldr  r1, [r4]
    bic  r1, r1, #0x00ffffff
    bic  r5, r5, #0xff000000
    orr  r1, r1, r5
    str  r1, [r4]
    pop  {{r4, r5, r6, pc}}
    .align 2
lit_cnt: .word {cnt}
"""

# --btnswitch (needs --mailhold): side buttons need the pen interrogated about
# every 5 ms (stock). A wrapper on the stock step-29 event returns 3 (-> step
# +10 = 23, i.e. another stock loop, skipping the extra cycles) while the last
# fresh result showed a side button, plus 16 loops of hysteresis.
# State: ARR+7 fresh button bits (CALC_HOLD5), ARR+18 countdown.
BTNSW = """
    push {{r4, lr}}
    bl   #{inner}
    cmp  r0, #1
    bne  out
    ldr  r1, lit_arr
    ldrb r3, [r1, #18]
    cbz  r3, extra
    subs r3, #1
    strb r3, [r1, #18]
    movs r3, #1
    strb r3, [r1, #19]
    movs r0, #3
    b    out
extra:
    strb r3, [r1, #19]
out:
    pop  {{r4, pc}}
    .align 2
lit_arr: .word {arr}
"""
# Extra-cycle ends (29'-type records) get BTNSW_X: switch right away while the
# countdown is armed (no decrement), so a button press takes effect within one
# extra cycle (~2.8 ms) instead of at the end of the whole loop (v2.22).
BTNSW_X = """
    push {{r4, lr}}
    bl   #{inner}
    cmp  r0, #1
    bne  out
    ldr  r1, lit_arr
    ldrb r3, [r1, #18]
    cbz  r3, out
    movs r3, #1
    strb r3, [r1, #19]
    movs r0, #3
out:
    pop  {{r4, pc}}
    .align 2
lit_arr: .word {arr}
"""
# ARR+19 = 1 while the loop actually runs stock (set when step 29 takes alt3,
# cleared when it continues into the extra cycles); the holds bypass on this
# flag, so extra frames still in flight after a button is seen stay held (v2.21)
# (v2.19: the countdown is armed in CALC_HOLD5 by ANY calc result, before the
#  hold rewrites it, that shows a side-button bit or level 1; v2.18 only looked
#  at fresh results and missed button-held drags)

# debug (--diffmap): calc drain loop that, per frame, snapshots the calc's RAM,
# runs the frame, and marks changed words in MAP0 (fresh) / MAP1 (stale).
CALC_DIFF = """
    push {{r4, r5, r6, r7, r8, lr}}
    ldr  r0, lit_magic_at
    ldr  r1, [r0]
    ldr  r2, lit_magic
    cmp  r1, r2
    beq  ready
    str  r2, [r0]
    ldr  r0, lit_map0
    movs r1, #0
    movw r3, #{clr}
clr:
    strb r1, [r0], #1
    subs r3, #1
    bne  clr
ready:
    movs r8, #4
again:
    ldr  r0, lit_ring
    ldrh r1, [r0, #2]
    ldrh r2, [r0]
    cmp  r1, r2
    beq  out
    ldr  r0, lit_arr
    ldrb r7, [r0, r1]
    cmp  r7, #1
    it   hi
    movhi r7, #1
    ldr  r0, lit_snap
    ldr  r1, lit_calc
    movw r2, #{n}
    bl   #{memcpy}
    bl   #{calc}
    ldr  r4, lit_calc
    ldr  r5, lit_snap
    ldr  r6, lit_map0
    cbz  r7, m0
    add  r6, r6, #0x800
m0:
    movw r3, #{words}
cmp_loop:
    ldr  r0, [r4], #4
    ldr  r1, [r5], #4
    cmp  r0, r1
    itt  ne
    movne r0, #1
    strbne r0, [r6]
    adds r6, #1
    subs r3, #1
    bne  cmp_loop
    ldr  r0, lit_cnt
    ldr  r1, [r0, r7, lsl #2]
    adds r1, #1
    str  r1, [r0, r7, lsl #2]
    subs r8, #1
    bne  again
out:
    pop  {{r4, r5, r6, r7, r8, pc}}
    .align 2
lit_magic_at: .word 0x20032FF0
lit_magic:    .word 0x5A17C0DE
lit_map0:     .word 0x20032000
lit_cnt:      .word 0x20032FF8
lit_ring:     .word {ring}
lit_arr:      .word {arr}
lit_snap:     .word 0x20030000
lit_calc:     .word {calcmem}
"""
CALCMEM, CALCN = 0x20010E0C, 0x12F4          # calc RAM 0x20010E0C..0x20012100

# --restore: calc drain loop that, for stale frames, saves the listed calc RAM
# ranges before the frame is processed and restores them afterwards (the frame's
# own output mail is already sent), so the calc's persistent state only ever
# advances on fresh frames.
RESTORE_SAVE = 0x20030000


def calc_restore_src(calc, ring, arr, memcpy, ranges):
    save_lines, rest_lines, off = [], [], 0
    for addr, n in ranges:
        save_lines += [f"    ldr r0, ={hex(RESTORE_SAVE + off)}", f"    ldr r1, ={hex(addr)}",
                       f"    movw r2, #{hex(n)}", f"    bl #{hex(memcpy)}"]
        rest_lines += [f"    ldr r0, ={hex(addr)}", f"    ldr r1, ={hex(RESTORE_SAVE + off)}",
                       f"    movw r2, #{hex(n)}", f"    bl #{hex(memcpy)}"]
        off += (n + 3) & ~3
    return "\n".join([
        "    push {r4, r5, r6, lr}",
        "    movs r5, #4",
        "again:",
        f"    ldr r0, ={hex(ring)}",
        "    ldrh r1, [r0, #2]",
        "    ldrh r2, [r0]",
        "    cmp r1, r2",
        "    beq out",
        f"    ldr r0, ={hex(arr)}",
        "    ldrb r4, [r0, r1]",
        "    cbz r4, run",
        *save_lines,
        "run:",
        f"    bl #{hex(calc)}",
        "    cbz r4, next",
        *rest_lines,
        "next:",
        "    subs r5, #1",
        "    bne again",
        "out:",
        "    pop {r4, r5, r6, pc}",
        "    .ltorg",
    ])

HID_DRAIN = """
    push {{r4, r5, r6, lr}}
    sub  sp, #16
    mov  r4, r0
    mov  r5, r1
    bl   #{get}
again:
    ldr  r0, [r4]
    cmp  r0, #0x20
    bne  out
    mov  r0, sp
    mov  r1, r5
    movs r2, #0
    bl   #{get}
    ldr  r0, [sp]
    cmp  r0, #0x20
    bne  out
    mov  r0, r5
    ldr  r1, [r4, #4]
    bl   #{free}
    ldr  r0, [sp, #4]
    str  r0, [r4, #4]
    b    again
out:
    add  sp, #16
    pop  {{r4, r5, r6, pc}}
"""

# --upsample MS (needs --btnswitch): constant 1 kHz output while a side button
# holds the scan in the stock loop (~600 real frames/s; the pen needs the
# pressure/button readout every ~5 ms, so more real frames are not possible).
# Every frame is stamped with DWT->CYCCNT when it is pushed into the ring
# (PUSHTS, ring slot -> UPS+0x20), the stamp follows it through the calc pop
# (POP_TAG -> UPS+0x18) to its mail (MAILTS, table of (mail ptr, stamp) at
# UPS+0x30), and the HID copy of the mail (COPYTS -> UPS+0x14). The HID pen
# routine gets a wrapper (UPS_HID) that runs once per 1 ms HID tick:
#   - real pen report (1 record, hover/contact): its sample (stamp, x, y) goes
#     into a 4-entry history (UPS+0x70, 12 B each)
#   - tick without a new result: a repeat record is built by the stock report
#     builder from the last result (the builder is a pure function of it)
#   - both: X/Y of the record = history interpolated at now - lag, clamped to
#     the newest/oldest sample (never extrapolated, so no overshoot)
# lag ramps 0 <-> MS at 0.5 ms per tick when the button mode (ARR+19) switches,
# so entering/leaving the mode neither jumps nor steps back. Proximity changes,
# vendor-mode reports and >12 ms without a sample reset it to stock behaviour.
# State (no-init SRAM, magic-checked):
#   +0 magic, +4 lag, +8 samples, +0x10 synth ok, +0x11 new result this tick,
#   +0x14 stamp of the HID copy, +0x18 stamp of the popped frame, +0x1C mail idx,
#   +0x20 u32[3] ring slot stamps, +0x30 8 x (mail, stamp), +0x70 4 x (t, x, y),
#   +0xA0 counters: synthesized, held (data late), interpolated, real
#   +0x12 glitch hold active, +0xB0 time of the last good result, +0xB4 glitches,
#   +0xB8 HID ticks, +0xBC results that produced no pen record (the tick is still filled)
#   +0xC0 ticks without a pen report while a pen state was held, +0xC4 stale (>12 ms)
#   +0xD0 u32[4] reports sent per USB queue (9 B, 9 B, 192 B, pen), +0xE0 endpoint-1 DataIn
# Good levels: 2/3, and 1 (far hover, hover distance 63) once it is the held
# state, nothing better is held, or it lasted CONFIRM.
# A result that drops out of hover/contact (level 0/1: in range cleared, tilt and
# pressure 0 - side-button press) while the pen is tracked is not sent: the host
# keeps getting the last good state (GOOD, 0x798 B copy of that result) for up to
# 16 ms and the delay is not reset. If the change persists, the stock transition
# records are sent (prev = GOOD), so a real pen exit still reaches the host.
UPS = 0x2003FB00
GOOD = 0x2003F000
UPS_MAGIC = 0x35535055     # "UPS5": bump when the state layout changes
DWT_CTRL = 0xE0001000
DEMCR = 0xE000EDFC
PUSHTS = """
    ldr  r1, lit_ring
    ldrh r1, [r1]
    cmp  r1, #2
    bhi  skip
    ldr  r2, lit_cyc
    ldr  r2, [r2]
    ldr  r3, lit_ups
    add.w r3, r3, r1, lsl #2
    str  r2, [r3, #0x20]
skip:
    mov  r1, r0
    push {{r4, r5, lr}}
    b.w  #{back}
    .align 2
lit_ring: .word {ring}
lit_cyc:  .word {cyc}
lit_ups:  .word {ups}
"""
POP_STAMP = """ldr  r1, lit_ups
    add.w r3, r1, r2, lsl #2
    ldr  r3, [r3, #0x20]
    str  r3, [r1, #0x18]"""
# r6 = mail copy; called from CALC_HOLD5 right before the mail put
MAILTS = """
    ldr  r0, lit_ups
    ldr  r1, [r0, #0x1c]
    and  r2, r1, #7
    add.w r2, r0, r2, lsl #3
    str  r6, [r2, #0x30]
    ldr  r3, [r0, #0x18]
    str  r3, [r2, #0x34]
    adds r1, #1
    str  r1, [r0, #0x1c]
    bx   lr
    .align 2
lit_ups: .word {ups}
"""
# --perscan (with --k 0 --upsample): the mail copy (r6; the calc's own struct r8 is never
# written) gets Wacom's per-scan position (r8+0xFA4 - 2800 / r8+0x10D8 - 2840: changes
# exactly on fresh S1 results, before the 4-result moving average) instead of the smoothed
# output, while the pen is tracked (level >= 2). The upsampler then interpolates between
# real scans only (history takes a point only when the position changed, PS_DEDUPE).
PERSCAN = """    ldrb.w r0, [r8, #0x794]
    cmp  r0, #2
    blo  psk
    ldr.w r0, [r8, #0xfa4]
    subw r0, r0, #2800
    cmp  r0, #0
    it   lt
    movlt r0, #0
    str  r0, [r6, #0x1c]
    movw r1, #0x10d8
    ldr.w r0, [r8, r1]
    subw r0, r0, #2840
    cmp  r0, #0
    it   lt
    movlt r0, #0
    str  r0, [r6, #0x54]
psk:
"""
PS_HIST_OLD = """    strb r0, [r5, #0x12]
    ldr  r0, [r5, #8]
    and  r1, r0, #3
    add.w r1, r1, r1, lsl #1
    add.w r1, r5, r1, lsl #2
    ldr  r2, [r5, #0x14]
    str  r2, [r1, #0x70]
    ldr  r2, [r7, #0x1c]
    str  r2, [r1, #0x74]
    ldr  r2, [r7, #0x54]
    str  r2, [r1, #0x78]
    adds r0, #1
    str  r0, [r5, #8]
    movs r0, #1
    strb r0, [r5, #0x10]
"""
PS_HIST_NEW = """    strb r0, [r5, #0x12]
    ldr  r0, [r5, #8]
    subs r1, r0, #1
    and  r1, r1, #3
    add.w r1, r1, r1, lsl #1
    add.w r1, r5, r1, lsl #2
    ldr  r2, [r1, #0x74]
    ldr  r3, [r7, #0x1c]
    cmp  r2, r3
    bne  psnew
    ldr  r2, [r1, #0x78]
    ldr  r3, [r7, #0x54]
    cmp  r2, r3
    beq  psdup
psnew:
    and  r1, r0, #3
    add.w r1, r1, r1, lsl #1
    add.w r1, r5, r1, lsl #2
    ldr  r2, [r5, #0x14]
    str  r2, [r1, #0x70]
    ldr  r2, [r7, #0x1c]
    str  r2, [r1, #0x74]
    ldr  r2, [r7, #0x54]
    str  r2, [r1, #0x78]
    adds r0, #1
    str  r0, [r5, #8]
psdup:
    movs r0, #1
    strb r0, [r5, #0x10]
"""
# --k 0 (no mail hold): replaces "ldr r0, [r5]; mov r1, r6" before the calc's put
MAILTS_PUT = MAILTS.replace("    bx   lr\n", "    ldr  r0, [r5]\n    mov  r1, r6\n    bx   lr\n", 1)
# --k 0: calc ring pop, stamp of the slot about to be popped
POP_TS = """
    ldr  r1, lit_ring
    ldrh r2, [r1, #2]
    ldrh r3, [r1]
    cmp  r2, r3
    beq  go
    cmp  r2, #2
    bhi  go
    ldr  r1, lit_ups
    add.w r3, r1, r2, lsl #2
    ldr  r3, [r3, #0x20]
    str  r3, [r1, #0x18]
go:
    b.w  #{pop}
    .align 2
lit_ring: .word {ring}
lit_ups:  .word {ups}
"""
# replaces the HID's "bl memcpy(cur, mail, 0x798)": r1 = mail
COPYTS = """
    push {{r4, r5, r6, r7}}
    ldr  r3, lit_ups
    ldr  r4, [r3, #0x1c]
    ldr  r6, lit_cyc
    ldr  r6, [r6]
    movs r5, #8
lp:
    subs r4, #1
    and  r7, r4, #7
    add.w r7, r3, r7, lsl #3
    ldr.w r12, [r7, #0x30]
    cmp  r12, r1
    bne  nx
    ldr  r6, [r7, #0x34]
    b    found
nx:
    subs r5, #1
    bne  lp
found:
    str  r6, [r3, #0x14]
    movs r4, #1
    strb r4, [r3, #0x11]
    pop  {{r4, r5, r6, r7}}
    b.w  #{copy}
    .align 2
lit_ups: .word {ups}
lit_cyc: .word {cyc}
"""
# --latmeter: replaces the HID's "bl memcpy(cur, mail, 0x798)" like COPYTS, but
# only measures. Latency = DWT now - ring-push stamp of the frame this mail came
# from (PUSHTS -> POP_TS -> MAILTS_PUT table). LAT: +0 magic, +4 count, +8 sum
# (cycles >> 6), +0xC mails without a stamp, +0x10 max cycles, +0x40 32 u32 bins
# of 16384 cycles = 0.171 ms (last = overflow). Enables DWT itself.
LAT = 0x2003FA00
LAT_MAGIC = 0x3154414C     # "LAT1"
COPYLAT = """
    push {{r0, r1, r2, r4, r5, r6, r7, lr}}
    ldr  r4, lit_demcr
    ldr  r5, [r4]
    orr  r5, r5, #0x1000000
    str  r5, [r4]
    ldr  r4, lit_dwt
    ldr  r5, [r4]
    orr  r5, r5, #1
    str  r5, [r4]
    ldr  r0, [r4, #4]
    ldr  r3, lit_ups
    ldr  r4, [r3, #0x1c]
    movs r5, #8
lp:
    subs r4, #1
    and  r7, r4, #7
    add.w r7, r3, r7, lsl #3
    ldr.w r12, [r7, #0x30]
    cmp  r12, r1
    bne  nx
    ldr  r6, [r7, #0x34]
    b    found
nx:
    subs r5, #1
    bne  lp
    ldr  r3, lit_lat
    ldr  r2, [r3, #0xc]
    adds r2, #1
    str  r2, [r3, #0xc]
    b    out
found:
    subs r6, r0, r6
    ldr  r3, lit_lat
    ldr  r2, lit_magic
    str  r2, [r3]
    ldr  r2, [r3, #4]
    adds r2, #1
    str  r2, [r3, #4]
    ldr  r2, [r3, #8]
    add.w r2, r2, r6, lsr #6
    str  r2, [r3, #8]
    ldr  r2, [r3, #0x10]
    cmp  r6, r2
    it   hi
    strhi r6, [r3, #0x10]
    lsrs r2, r6, #14
    cmp  r2, #31
    it   hi
    movhi r2, #31
    add.w r2, r3, r2, lsl #2
    ldr  r5, [r2, #0x40]
    adds r5, #1
    str  r5, [r2, #0x40]
out:
    pop  {{r0, r1, r2, r4, r5, r6, r7, lr}}
    b.w  #{copy}
    .align 2
lit_demcr: .word {demcr}
lit_dwt:   .word {dwt}
lit_ups:   .word {ups}
lit_lat:   .word {lat}
lit_magic: .word {magic}
"""
# --hidwake: replaces the hidcontroller loop's osDelay(1). First sends any report
# waiting for endpoint 0x81 (usbpush sender), then blocks on the calc -> HID mail
# queue in peek mode (xQueueGenericReceive(q, buf, 1 tick, justPeek=1)): the loop
# runs as soon as a calc result is posted instead of on the next tick. A guard
# (more than 4 passes within 1 ms) falls back to osDelay(1), so an unconsumed
# mail can never make the task spin. State at LAT+0xC0 (window start, count).
HIDWAKE = """
    push {{r4, r5, lr}}
    sub  sp, #12
    ldr  r4, lit_demcr
    ldr  r5, [r4]
    orr  r5, r5, #0x1000000
    str  r5, [r4]
    ldr  r4, lit_dwt
    ldr  r5, [r4]
    orr  r5, r5, #1
    str  r5, [r4]
    ldr  r0, [r4, #4]
    ldr  r4, lit_st
    ldr  r1, [r4]
    subs r1, r0, r1
    ldr  r2, lit_ms
    cmp  r1, r2
    blo  same
    str  r0, [r4]
    movs r1, #0
    str  r1, [r4, #4]
same:
    ldr  r1, [r4, #4]
    adds r1, #1
    str  r1, [r4, #4]
    cmp  r1, #4
    bhi  slow
    bl   #{usbsend}
    ldr  r0, lit_mq
    ldrb r1, [r0, #4]
    cmp  r1, #1
    bne  slow
    ldr  r0, [r0]
    cmp  r0, #0
    beq  slow
    ldr  r0, [r0, #4]
    cmp  r0, #0
    beq  slow
    mov  r1, sp
    movs r2, #1
    movs r3, #1
    bl   #{recv}
    ldr  r1, [r4, #8]
    adds r1, #1
    str  r1, [r4, #8]
    add  sp, #12
    pop  {{r4, r5, pc}}
slow:
    ldr  r1, [r4, #12]
    adds r1, #1
    str  r1, [r4, #12]
    add  sp, #12
    movs r0, #1
    pop  {{r4, r5, lr}}
    b.w  #{delay}
    .align 2
lit_demcr: .word {demcr}
lit_dwt:   .word {dwt}
lit_st:    .word {st}
lit_ms:    .word 96000
lit_mq:    .word {mq}
"""
# --calcwake: the calc task wakes when a frame is pushed instead of on the next
# 1 ms tick. The ring push runs in the sensor ISRs (priority 2/3, above the RTOS
# mask 0x50), which must not call FreeRTOS, so the push hook only pends IRQ 7
# (EXTI1, unused) through NVIC STIR. Its handler (vector patched, priority 6)
# posts a token to a private queue (xQueueGenericSendFromISR) and pends PendSV.
# The calc loop's osDelay(1) becomes xQueueGenericReceive(q, 1 tick), and the
# loop drains every pending ring frame per pass. The queue is created, IRQ 7
# prioritised and enabled in the calc task's entry (once per boot; NVIC enables
# reset with the MCU, so a stale handle from before a reboot is never used).
KICK_IRQ = 7
KICK_H = LAT + 0xD0
PUSHKICK = """
    ldr  r1, lit_ring
    ldrh r1, [r1]
    cmp  r1, #2
    bhi  skip
    ldr  r2, lit_cyc
    ldr  r2, [r2]
    ldr  r3, lit_ups
    add.w r3, r3, r1, lsl #2
    str  r2, [r3, #0x20]
skip:
    ldr  r2, lit_stir
    movs r3, #{irq}
    str  r3, [r2]
    mov  r1, r0
    push {{r4, r5, lr}}
    b.w  #{back}
    .align 2
lit_ring: .word {ring}
lit_cyc:  .word {cyc}
lit_ups:  .word {ups}
lit_stir: .word 0xE000EF00
"""
KICK_ISR = """
    push {{r4, lr}}
    sub  sp, #8
    ldr  r0, lit_h
    ldr  r0, [r0]
    cmp  r0, #0
    beq  out
    movs r3, #0
    str  r3, [sp, #4]
    add  r2, sp, #4
    mov  r1, sp
    bl   #{send}
    ldr  r0, [sp, #4]
    cmp  r0, #0
    beq  out
    ldr  r1, lit_icsr
    mov.w r0, #0x10000000
    str  r0, [r1]
    dsb  sy
    isb  sy
out:
    add  sp, #8
    pop  {{r4, pc}}
    .align 2
lit_h:    .word {h}
lit_icsr: .word 0xE000ED04
"""
KICK_INIT = """
    push {{r4, lr}}
    bl   #{orig}
    ldr  r4, lit_h
    movs r0, #0
    str  r0, [r4]
    movs r0, #4
    movs r1, #1
    movs r2, #0
    bl   #{create}
    str  r0, [r4]
    cmp  r0, #0
    beq  done
    ldr  r1, lit_ipr
    movs r2, #0x60
    strb r2, [r1]
    ldr  r1, lit_iser
    movs r2, #{bit}
    str  r2, [r1]
done:
    pop  {{r4, pc}}
    .align 2
lit_h:    .word {h}
lit_ipr:  .word {ipr}
lit_iser: .word 0xE000E100
"""
CALCWAIT = """
    push {{r4, lr}}
    sub  sp, #8
    ldr  r0, lit_h
    ldr  r0, [r0]
    cmp  r0, #0
    beq  slow
    mov  r1, sp
    movs r2, #1
    movs r3, #0
    bl   #{recv}
    add  sp, #8
    pop  {{r4, pc}}
slow:
    add  sp, #8
    movs r0, #1
    pop  {{r4, lr}}
    b.w  #{delay}
    .align 2
lit_h:    .word {h}
"""
# --tearhold: stock (and v1.65) tracking sometimes loses the pen for 16-54 ms in
# a fast stroke: a contact result with firm pressure is followed within ~2 ms by
# a hover/far result, then contact again (a torn line). A real lift fades the
# pressure out first (last contact result has low pressure) and then hovers.
# Hook on the calc's mail put (r0 queue id, r1 mail): when a contact result with
# pressure >= 200 is followed within 4 ms by a non-contact one, that mail and the
# ones after it are freed instead of posted until contact returns (the stroke
# continues) or 80 ms pass (the drop-out is then real and posted). If the first
# non-contact result is plain hover (level 2), one more result decides: far/out
# -> hold, hover again -> real lift, posted at once. Nothing is invented: during
# a hold the HID simply gets no result. State at TEAR (no-init RAM):
# +0 t_contact, +4 armed, +8 mode (0 pass, 1 one hover held, 2 holding),
# +0xC holds started, +0x10 resumed in contact, +0x14 timed out, +0x18 lifts.
TEAR = 0x2003FA00
TEARHOLD = """
    push {{r4, r5, r6, r7, lr}}
    mov  r4, r0
    mov  r5, r1
    cmp  r5, #0
    beq  post
    ldr  r6, lit_demcr
    ldr  r7, [r6]
    orr  r7, r7, #0x1000000
    str  r7, [r6]
    ldr  r6, lit_dwt
    ldr  r7, [r6]
    orr  r7, r7, #1
    str  r7, [r6]
    ldr  r6, [r6, #4]
    ldr  r7, lit_st
    ldrb.w r0, [r5, #0x794]
    cmp  r0, #3
    bne  noncontact
    ldr  r1, [r7, #8]
    cmp  r1, #0
    beq  c_mode_ok
    ldr  r1, [r7, #0x10]
    adds r1, #1
    str  r1, [r7, #0x10]
    movs r1, #0
    str  r1, [r7, #8]
c_mode_ok:
    ldrh r1, [r5]
    cmp  r1, #200
    blo  c_light
    str  r6, [r7]
    movs r1, #1
    str  r1, [r7, #4]
    b    post
c_light:
    movs r1, #0
    str  r1, [r7, #4]
    b    post
noncontact:
    ldr  r1, [r7, #8]
    cmp  r1, #2
    beq  holding
    cmp  r1, #1
    beq  onehover
    ldr  r1, [r7, #4]
    cmp  r1, #0
    beq  post
    movs r1, #0
    str  r1, [r7, #4]
    ldr  r1, [r7]
    subs r1, r6, r1
    ldr  r2, lit_4ms
    cmp  r1, r2
    bhs  post
    ldr  r1, [r7, #0xc]
    adds r1, #1
    str  r1, [r7, #0xc]
    cmp  r0, #2
    beq  hold1
    movs r1, #2
    str  r1, [r7, #8]
    b    drop
hold1:
    movs r1, #1
    str  r1, [r7, #8]
    b    drop
onehover:
    cmp  r0, #2
    bne  tohold
    movs r1, #0
    str  r1, [r7, #8]
    ldr  r1, [r7, #0x18]
    adds r1, #1
    str  r1, [r7, #0x18]
    b    post
tohold:
    movs r1, #2
    str  r1, [r7, #8]
holding:
    ldr  r1, [r7]
    subs r1, r6, r1
    ldr  r2, lit_80ms
    cmp  r1, r2
    blo  drop
    movs r1, #0
    str  r1, [r7, #8]
    ldr  r1, [r7, #0x14]
    adds r1, #1
    str  r1, [r7, #0x14]
    b    post
drop:
    mov  r0, r4
    mov  r1, r5
    bl   #{free}
    movs r0, #0
    pop  {{r4, r5, r6, r7, pc}}
post:
    mov  r0, r4
    mov  r1, r5
    pop  {{r4, r5, r6, r7, lr}}
    b.w  #{put}
    .align 2
lit_demcr: .word {demcr}
lit_dwt:   .word {dwt}
lit_st:    .word {st}
lit_4ms:   .word 384000
lit_80ms:  .word 7680000
"""
# target delay: MS while the button mode (ARR+19) runs, else 0 (--k 0: always MS)
UPS_TARGET = """    ldr  r0, lit_arr
    ldrb r0, [r0, #19]
    ldr  r1, lit_lag
    cmp  r0, #0
    it   eq
    moveq r1, #0
"""
UPS_HID = """
    @ far branches are written .w: keystone mis-resolves bl targets after it
    @ widens a branch itself (checked after assembly)
    push {{r4, r5, r6, r7, r8, r9, r10, r11, lr}}
    bl   #{fn}
    mov  r4, r0
    ldr  r5, lit_ups
    ldr  r0, [r5]
    ldr  r1, lit_magic
    cmp  r0, r1
    beq  inited
    movs r0, #0
    movs r2, #0xf4
zl:
    subs r2, #4
    str  r0, [r5, r2]
    bne  zl
    str  r1, [r5]
inited:
    ldr  r0, [r5, #0xb8]
    adds r0, #1
    str  r0, [r5, #0xb8]
    ldr  r0, lit_demcr
    ldr  r1, [r0]
    orr  r1, r1, #0x1000000
    str  r1, [r0]
    ldr  r0, lit_dwt
    ldr  r1, [r0]
    orr  r1, r1, #1
    str  r1, [r0]
    ldr  r6, [r0, #4]
    ldr  r7, lit_cur
    ldrb r8, [r5, #0x11]
    movs r0, #0
    strb r0, [r5, #0x11]
    tst  r4, #0x80
    bne  reset
    and  r9, r4, #0xf
    ldrb.w r10, [r7, #0x794]
    cmp  r8, #0
    beq  nomail
    cmp  r9, #0
    bne  gotrec
    ldr  r0, [r5, #0xbc]
    adds r0, #1
    str  r0, [r5, #0xbc]
    b    ramp
gotrec:
    cmp  r10, #2
    blo  low
    cmp  r10, #3
    bhi  bad
goodlvl:
    cmp  r9, #1
    bne  reset
    ldr  r0, lit_good
    mov  r1, r7
    mov.w r2, #0x798
    bl   #{copy}
    str  r6, [r5, #0xb0]
    movs r0, #0
    strb r0, [r5, #0x12]
    ldr  r0, [r5, #8]
    and  r1, r0, #3
    add.w r1, r1, r1, lsl #1
    add.w r1, r5, r1, lsl #2
    ldr  r2, [r5, #0x14]
    str  r2, [r1, #0x70]
    ldr  r2, [r7, #0x1c]
    str  r2, [r1, #0x74]
    ldr  r2, [r7, #0x54]
    str  r2, [r1, #0x78]
    adds r0, #1
    str  r0, [r5, #8]
    movs r0, #1
    strb r0, [r5, #0x10]
    ldr  r0, [r5, #0xac]
    adds r0, #1
    str  r0, [r5, #0xac]
    b    ramp
low:
    cmp  r10, #1
    bne  bad
    cmp  r9, #1
    bne  bad
    ldrb r0, [r5, #0x10]
    cmp  r0, #0
    beq  goodlvl
    ldr  r0, lit_good
    ldrb.w r0, [r0, #0x794]
    cmp  r0, #1
    beq  goodlvl
    ldr  r0, [r5, #0xb0]
    subs r0, r6, r0
    ldr  r1, lit_confirm
    cmp  r0, r1
    bhs  goodlvl
bad:
    @ a result that left hover/contact while the pen was tracked (press glitch):
    @ the host keeps the last good state for up to CONFIRM, the delay is kept
    ldrb r0, [r5, #0x10]
    cmp  r0, #0
    beq  reset
    ldr  r0, [r5, #0xb0]
    subs r0, r6, r0
    ldr  r1, lit_confirm
    cmp  r0, r1
    bhs  reset
    movs r0, #1
    strb r0, [r5, #0x12]
    cmp  r10, #1
    bne  nohist
    ldr  r0, [r5, #8]
    and  r1, r0, #3
    add.w r1, r1, r1, lsl #1
    add.w r1, r5, r1, lsl #2
    ldr  r2, [r5, #0x14]
    str  r2, [r1, #0x70]
    ldr  r2, [r7, #0x1c]
    str  r2, [r1, #0x74]
    ldr  r2, [r7, #0x54]
    str  r2, [r1, #0x78]
    adds r0, #1
    str  r0, [r5, #8]
nohist:
    ldr  r0, lit_prev
    ldr  r1, lit_good
    mov.w r2, #0x798
    bl   #{copy}
    movs r0, #1
    push {{r0, r1}}
    ldr  r0, lit_recptr
    ldr  r0, [r0]
    ldr  r1, lit_good
    mov  r2, r1
    ldrb.w r3, [r1, #0x794]
    bl   #{builder}
    add  sp, #8
    bic  r4, r4, #0xf
    orr  r4, r4, #1
    mov.w r9, #1
    ldr  r0, [r5, #0xb4]
    adds r0, #1
    str  r0, [r5, #0xb4]
    b    ramp
nomail:
    @ glitch outlasted CONFIRM with no newer result: send the stock transition
    @ (prev = last good state, cur = the result that left)
    ldrb r0, [r5, #0x12]
    cmp  r0, #0
    beq  ramp
    ldr  r0, [r5, #0xb0]
    subs r0, r6, r0
    ldr  r1, lit_confirm
    cmp  r0, r1
    blo  ramp
    bl   #{records}
    and  r0, r0, #0xf
    orr  r4, r4, r0
    ldr  r0, lit_prev
    mov  r1, r7
    mov.w r2, #0x798
    bl   #{copy}
reset:
    movs r0, #0
    strb r0, [r5, #0x10]
    strb r0, [r5, #0x12]
    str  r0, [r5, #8]
    str  r0, [r5, #4]
    b    out
ramp:
{target}    ldr  r0, [r5, #4]
    ldr  r2, lit_step
    cmp  r0, r1
    beq  lagok
    bhi  down
    adds r0, r0, r2
    cmp  r0, r1
    it   hi
    movhi r0, r1
    b    lagset
down:
    subs r0, r0, r2
    cmp  r0, r1
    it   lt
    movlt r0, r1
lagset:
    str  r0, [r5, #4]
lagok:
    cmp  r0, #0
    beq.w out
    mov  r11, r0
    ldrb r0, [r5, #0x10]
    cmp  r0, #0
    beq.w out
    ldr  r0, [r5, #8]
    cmp  r0, #0
    beq.w out
    subs r1, r0, #1
    and  r1, r1, #3
    add.w r1, r1, r1, lsl #1
    add.w r1, r5, r1, lsl #2
    ldr  r1, [r1, #0x70]
    subs r1, r6, r1
    ldr  r2, lit_stale
    cmp  r1, r2
    bls  fresh
    ldr  r0, [r5, #0xc4]
    adds r0, #1
    str  r0, [r5, #0xc4]
    movs r0, #0
    strb r0, [r5, #0x10]
    b    out
fresh:
    cmp  r9, #0
    bne  have_rec
    bl   #{usbstate}
    cmp  r0, #6
    bne  out
    movs r0, #1
    push {{r0, r1}}
    ldr  r0, lit_recptr
    ldr  r0, [r0]
    ldr  r1, lit_good
    mov  r2, r1
    ldrb.w r3, [r1, #0x794]
    bl   #{builder}
    add  sp, #8
    orr  r4, r4, #1
    ldr  r0, [r5, #0xa0]
    adds r0, #1
    str  r0, [r5, #0xa0]
    b    interp
have_rec:
    cmp  r9, #1
    bne  out
interp:
    ldr  r0, [r5, #8]
    cmp  r0, #4
    it   hi
    movhi r0, #4
    mov  r10, r0
    movs r9, #0
lp:
    ldr  r0, [r5, #8]
    subs r0, #1
    subs r0, r0, r9
    and  r0, r0, #3
    add.w r0, r0, r0, lsl #1
    add.w r8, r5, r0, lsl #2
    ldr  r1, [r8, #0x70]
    subs r1, r6, r1
    cmp  r1, r11
    bhs  found
    mov  r12, r8
    mov  r2, r1
    adds r9, #1
    cmp  r9, r10
    blo  lp
hold:
    ldr  r0, [r5, #0xa4]
    adds r0, #1
    str  r0, [r5, #0xa4]
    ldr  r0, [r8, #0x74]
    ldr  r1, [r8, #0x78]
    b    write
found:
    cmp  r9, #0
    beq  hold
    subs r3, r1, r11
    lsrs r3, r3, #6
    subs r1, r1, r2
    lsrs r1, r1, #6
    cmp  r1, #0
    bne  lerp
    ldr  r0, [r12, #0x74]
    ldr  r1, [r12, #0x78]
    b    write
lerp:
    ldr  r0, [r5, #0xa8]
    adds r0, #1
    str  r0, [r5, #0xa8]
    ldr  r0, [r12, #0x74]
    ldr  r2, [r8, #0x74]
    subs r0, r0, r2
    mul  r0, r0, r3
    .short 0xfb90, 0xf0f1          @ sdiv r0, r0, r1 (keystone lacks it)
    add  r0, r0, r2
    ldr  r9, [r12, #0x78]
    ldr  r10, [r8, #0x78]
    sub  r9, r9, r10
    mul  r9, r9, r3
    .short 0xfb99, 0xf9f1          @ sdiv r9, r9, r1
    add  r1, r9, r10
write:
    ldr  r2, lit_recptr
    ldr  r2, [r2]
    ldrb r3, [r2, #1]
    orr  r3, r3, r0, lsl #8
    str  r3, [r2, #1]
    ldrb r3, [r2, #4]
    orr  r3, r3, r1, lsl #8
    str  r3, [r2, #4]
out:
    tst  r4, #0x8f
    bne  sent
    ldrb r0, [r5, #0x10]
    cmp  r0, #0
    beq  sent
    ldr  r0, [r5, #0xc0]
    adds r0, #1
    str  r0, [r5, #0xc0]
sent:
    mov  r0, r4
    pop  {{r4, r5, r6, r7, r8, r9, r10, r11, pc}}
    .align 2
lit_ups:    .word {ups}
lit_magic:  .word {magic}
lit_demcr:  .word {demcr}
lit_dwt:    .word {dwt}
lit_cur:    .word {cur}
lit_prev:   .word {prev}
lit_recptr: .word {recptr}
lit_arr:    .word {arr}
lit_lag:    .word {lag}
lit_step:   .word {step}
lit_stale:  .word {stale}
lit_good:   .word {good}
lit_confirm: .word {confirm}
"""
# --safemail: CMSIS osMailAlloc never blocks here (it returns NULL when the pool
# is empty) and the firmware neither checks for NULL nor frees a block whose
# osMailPut failed. Once the calc -> HID queue backs up, NULL mails get queued,
# real puts fail and leak their blocks; after 5 leaks the pool is empty for good
# and every "result" the HID copies comes from address 0 (level 0xFF): the pen is
# dead until reboot (v2.25 on slot A). Guarded on the calc mail and the HID's USB
# report posts: memcpy into NULL is skipped, put(NULL) is not queued, and a block
# whose put failed is freed. The status byte still sees 0xFF, as before.
PUTSAFE = """
    cbz  r1, null
    push {{r0, r1, r4, lr}}
    bl   #{put}
    cmp  r0, #0xff
    bne  ok
    ldr  r0, [sp]
    ldr  r1, [sp, #4]
    bl   #{free}
    movs r0, #0xff
ok:
    add  sp, #8
    pop  {{r4, pc}}
null:
    movs r0, #0xff
    bx   lr
"""
MCPYSAFE = """
    cbz  r0, skip
    b.w  #{memcpy}
skip:
    bx   lr
"""
# --usbpush (implied by --upsample): the usbif task sends at most one report per
# 1 ms tick and only if endpoint 0x81 happens to be free at that moment; with
# 1000 reports/s produced, every tick that lands just before the host's poll is
# lost for good (measured 1000/s made, ~750/s received: gaps 1,1,2 ms).
# Now the next report goes out the moment the endpoint frees: the HID class
# DataIn callback (ISR; the class table is in RAM, pClass = [pdev+0x214], DataIn
# at +0x14, swapped at runtime by USB_TASKSEND) calls USB_SEND, and so do the
# task's four per-tick "ready?" checks, which then report "not ready" so the
# stock code never sends by itself. USB_SEND runs with interrupts masked, takes
# the first queue with a report (9 B, 9 B, 192 B, then pen 27 B) and sends it.
USB_SEND = """
    push {{r4, r5, r6, r7, lr}}
    sub  sp, #20
    .short 0xf3ef, 0x8710          @ mrs r7, primask (keystone lacks it)
    cpsid i
    bl   #{ready}
    cmp  r0, #0
    beq  out
    ldr  r4, lit_table
nextq:
    ldr  r5, [r4]
    cmp  r5, #0
    beq  out
    ldrb r0, [r5, #4]
    cmp  r0, #1
    bne  skipq
    mov  r0, sp
    ldr  r1, [r5]
    movs r2, #0
    bl   #{get}
    ldr  r0, [sp]
    cmp  r0, #0x20
    bne  skipq
    ldr  r6, [sp, #4]
    ldr  r0, [r4, #4]
    mov  r1, r6
    ldr  r2, [r4, #8]
    bl   #{memcpy}
    ldr  r0, [r5]
    mov  r1, r6
    bl   #{free}
    ldr  r0, [r4, #4]
    ldr  r1, [r4, #8]
    bl   #{send}
    ldr  r1, [r4, #12]
    ldr  r0, [r1]
    adds r0, #1
    str  r0, [r1]
    b    out
skipq:
    adds r4, #16
    b    nextq
out:
    .short 0xf387, 0x8810          @ msr primask, r7
    add  sp, #20
    pop  {{r4, r5, r6, r7, pc}}
    .align 2
lit_table: .word {table}
"""
USB_TASKSEND = """
    push {{r4, lr}}
    ldr  r0, lit_pdev
    ldr.w r0, [r0, #0x214]
    cmp  r0, #0
    beq  noinst
    ldr  r1, [r0, #0x14]
    ldr  r2, lit_stock
    cmp  r1, r2
    bne  noinst
    ldr  r2, lit_wrap
    str  r2, [r0, #0x14]
noinst:
    bl   #{usbsend}
    movs r0, #0
    pop  {{r4, pc}}
    .align 2
lit_pdev:  .word {pdev}
lit_stock: .word {stock}
lit_wrap:  .word {wrap}
"""
USB_DATAIN = """
    push {{r4, lr}}
    mov  r4, r1
    bl   #{datain}
    cmp  r4, #1
    bne  done
    ldr  r1, lit_cnt
    ldr  r2, [r1]
    adds r2, #1
    str  r2, [r1]
    push {{r0, r1}}
    bl   #{usbsend}
    pop  {{r0, r1}}
done:
    pop  {{r4, pc}}
    .align 2
lit_cnt: .word {cnt}
"""
# --pack: full-speed USB carries one interrupt packet per 1 ms frame, so a report
# per packet caps the rate at 1000/s. Report 0x10 is 27 B and the endpoint takes
# 64 B, so the sender puts two pen reports into one packet when two are queued;
# the host HID class splits a transfer holding several reports. The pen entry of
# the sender table points at PACKBUF instead of the stock 27 B buffer.
# FAST: +0x40 packets with 2 reports, +0x44 with 1, +0x48 HID extra iterations,
# +0x4C consecutive-extra byte, +0x60 frame23 state (pushed, skipped, coil lists).
FAST = 0x2003F800
PACKBUF = FAST
USB_PACK = """
    ldr  r0, [r4, #4]
    ldr  r1, lit_pbuf
    cmp  r0, r1
    bne  send1
    ldrb r0, [r1]
    cmp  r0, #0x10
    bne  send1
    mov  r0, sp
    ldr  r1, [r5]
    movs r2, #0
    bl   #{get}
    ldr  r0, [sp]
    cmp  r0, #0x20
    bne  send1c
    ldr  r6, [sp, #4]
    ldr  r0, lit_pbuf
    adds r0, #27
    mov  r1, r6
    movs r2, #27
    bl   #{memcpy}
    ldr  r0, [r5]
    mov  r1, r6
    bl   #{free}
    ldr  r0, lit_pbuf
    movs r1, #54
    bl   #{send}
    ldr  r1, lit_pbuf
    ldr  r0, [r1, #0x40]
    adds r0, #1
    str  r0, [r1, #0x40]
    b    counted
send1c:
    ldr  r1, lit_pbuf
    ldr  r0, [r1, #0x44]
    adds r0, #1
    str  r0, [r1, #0x44]
send1:
    ldr  r0, [r4, #4]
    ldr  r1, [r4, #8]
    bl   #{send}
counted:
"""
USB_SEND_TAIL = """
    ldr  r0, [r4, #4]
    ldr  r1, [r4, #8]
    bl   #{send}
"""
# --pack without --usbpush: the task's per-tick ready checks send (at most one
# packet per tick, as stock), the DataIn callback is left alone
USB_TASKSEND_TICK = """
    push {{r4, lr}}
    bl   #{usbsend}
    movs r0, #0
    pop  {{r4, pc}}
"""
# --hidmore: the HID task takes one calc result per 1 ms iteration (1000/s cap).
# Replaces its loop-end osDelay(1): while calc results are waiting (FreeRTOS
# queue uxMessagesWaiting, handle = [mailq cb + 4]), run up to 2 more
# iterations at once, then sleep as before.
HIDMORE = """
    ldr  r0, lit_mq
    ldr  r0, [r0]
    cmp  r0, #0
    beq  sleep
    ldr  r0, [r0, #4]
    cmp  r0, #0
    beq  sleep
    ldr  r0, [r0, #0x38]
    cmp  r0, #0
    beq  sleep
    ldr  r2, lit_st
    ldrb r1, [r2, #0x4c]
    cmp  r1, #2
    bhs  sleep2
    adds r1, #1
    strb r1, [r2, #0x4c]
    ldr  r1, [r2, #0x48]
    adds r1, #1
    str  r1, [r2, #0x48]
    movs r0, #0
    bx   lr
sleep:
    ldr  r2, lit_st
sleep2:
    movs r1, #0
    strb r1, [r2, #0x4c]
    movs r0, #1
    b.w  #{delay}
    .align 2
lit_mq: .word {mq}
lit_st: .word {st}
"""
# --frame23: the set-1 pass-a frame (stock step 23 and the first record of every
# extra cycle; tools/make_frame23.py, v2.45), pushed only when the coil window
# (register image coil lists) equals the one of the previous pass a. With
# --hold it is tagged stale, so the raw hold gives it the last real pressure.
F23_STAGE = 0x2001B1FC
F23_EXTRA = 0x2001B7E5
F23_IMG = 0x2001D358
F23_RANGES = ((0x17, 0x24), (0x4C, 0x59))
FRAME23 = """
    push {{r4, r5, r6, r7, lr}}
    mov  r4, r2
    bl   #{orig}
    subs r3, r0, #1
    cmp  r3, #1
    bhi.w out
    push {{r0, r1}}
    ldr  r5, lit_img
    ldr  r6, lit_st
    movs r7, #0
{compare}
    cmp  r7, #0
    bne.w skip
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
{tag}
    ldr  r0, lit_stage
    bl   #{push}
    ldr  r0, [r6]
    adds r0, #1
    str  r0, [r6]
    b.w  done
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
{taglits}
"""
F23_BYTE = """    ldrb r0, [r5, #{i}]
    ldrb r1, [r6, #{s}]
    cmp  r0, r1
    it   ne
    movne r7, #1
    strb r0, [r6, #{s}]
"""
F23_TAG = """    ldr  r3, lit_ring
    ldrh r3, [r3]
    ldr  r2, lit_arr
    movs r1, #1
    strb r1, [r2, r3]"""
F23_TAGLITS = """lit_ring:   .word {ring}
lit_arr:    .word {arr}"""
# --coillog (diagnostic, observe only): at the calc's mail put, log the result position
# (r8+0x1C X, r8+0x54 Y, u32) with the input frame it came from (r8+0x798: X block
# header+profile +0..0x74, Y block +0xAA..0x11E) into a 64 x 256 B ring.
# Entry: +0 seq, +4 X, +8 Y, +0xC [r8] (pressure/buttons), +0x10 X block, +0x88 Y block.
# Sequence counter u32 at COILLOG_IDX. Replaces "ldr r0, [r5]; mov r1, r6" before the put.
COILLOG = 0x20034000
COILLOG_IDX = FAST + 0x80
COILLOG_HOOK = """
    push {{r4, lr}}
    ldr  r4, lit_idx
    ldr  r0, [r4]
    adds r0, #1
    str  r0, [r4]
    and  r1, r0, #63
    ldr  r4, lit_log
    add.w r4, r4, r1, lsl #8
    str  r0, [r4]
    ldr.w r0, [r8, #0x1c]
    str  r0, [r4, #4]
    ldr.w r0, [r8, #0x54]
    str  r0, [r4, #8]
    ldr.w r0, [r8]
    str  r0, [r4, #12]
    add.w r0, r4, #0x10
    add.w r1, r8, #0x798
    movs r2, #0x75
    bl   #{memcpy}
    add.w r0, r4, #0x88
    addw r1, r8, #0x842
    movs r2, #0x75
    bl   #{memcpy}
    ldr  r0, [r5]
    mov  r1, r6
    pop  {{r4, pc}}
    .align 2
lit_idx: .word {idx}
lit_log: .word {log}
"""
# --framelog (diagnostic, observe only): like --coillog but the whole 0x605 B input frame,
# for 3 consecutive calc results out of every 128 (so the reader can keep up), in a
# 16 x 0x800 B ring at COILLOG. State: +0 entries logged, +4 results seen.
# Entry: +0 seq (written last), +4 X, +8 Y, +0xC [r8], +0x10 frame, +0x620 result number.
FRAMELOG_HOOK = """
    push {{r4, lr}}
    ldr  r1, lit_idx
    ldr  r0, [r1, #4]
    adds r0, #1
    str  r0, [r1, #4]
    and  r2, r0, #127
    cmp  r2, #3
    bhs.w skip
    ldr  r0, [r1]
    adds r0, #1
    and  r2, r0, #15
    ldr  r4, lit_log
    add.w r4, r4, r2, lsl #11
    movs r2, #0
    str  r2, [r4]
    ldr.w r2, [r8, #0x1c]
    str  r2, [r4, #4]
    ldr.w r2, [r8, #0x54]
    str  r2, [r4, #8]
    ldr.w r2, [r8]
    str  r2, [r4, #12]
    add.w r0, r4, #0x10
    add.w r1, r8, #0x798
    movw r2, #0x605
    bl   #{memcpy}
    ldr  r1, lit_idx
    ldr  r2, [r1, #4]
    str.w r2, [r4, #0x620]
    ldr  r0, [r1]
    adds r0, #1
    str  r0, [r4]
    str  r0, [r1]
skip:
    ldr  r0, [r5]
    mov  r1, r6
    pop  {{r4, pc}}
    .align 2
lit_idx: .word {idx}
lit_log: .word {log}
"""
# --ownpos: our own position from the raw coil profiles, sent only when measured.
# On v1.65 cadence the X/Y coil profiles (input frame r8+0x798: X header +0 / coil
# window start +0x0C (the measured window; +0x0D can already name the next one) /
# 10 entries +0x11, Y +0xAA / +0xB6 / +0xBB, entry stride 10, s16
# amplitude first) change once per scan loop; Wacom's other results are interpolated.
# Hook replaces "ldr r0, [r5]; mov r1, r6" before the calc mail put (needs --safemail,
# whose put guard skips NULL):
#   level (r8+0x794) < 2 or just entered tracking      -> pass unchanged
#   profiles changed (fresh measurement)                -> X/Y = own, send
#   not fresh, level changed (touch down/up)            -> X/Y = last own, send
#   otherwise                                           -> free the block, put NULL
# own = 1683 * (start + k) + 1683 * (r - l) / (2 (2c - l - r)) + offset,
# k = argmax of entries 1..8 (fit on logged data: X -1190, Y -1180).
# State OWN: +0 X amps, +0x14 Y amps, +0x28 last level, +0x2C/+0x30 last own X/Y,
# +0x40 fresh, +0x44 dropped, +0x48 level-change sends, +0x4C passed.
OWN = FAST + 0x100
OWNPOS_HOOK = """
    push {{r3, r4, r7, lr}}
    add.w r4, r8, #0x798
    ldr  r7, lit_own
    addw r0, r8, #0x794
    ldrb r0, [r0]
    ldrb r1, [r7, #0x28]
    strb r0, [r7, #0x28]
    cmp  r0, #{minlvl}
    blo.w pass
    cmp  r1, #{minlvl}
    blo.w pass
    mov  r2, r1
    mov.w r12, #0
{compare}
    cmp.w r12, #0
    beq.w stale
{btn}    add.w r0, r4, #0x11
    ldrb r1, [r4, #0x0c]
    adds r1, #4
    bl   calc
    subw r0, r0, #1190
    cmp  r0, #0
    it   lt
    movlt r0, #0
    movw r1, #44800
    cmp  r0, r1
    it   gt
    movgt r0, r1
    str  r0, [r6, #0x1c]
    str  r0, [r7, #0x2c]
    add.w r0, r4, #0xbb
    ldrb.w r1, [r4, #0xb6]
    adds r1, #4
    bl   calc
    subw r0, r0, #1180
    cmp  r0, #0
    it   lt
    movlt r0, #0
    movw r1, #29600
    cmp  r0, r1
    it   gt
    movgt r0, r1
    str  r0, [r6, #0x54]
    str  r0, [r7, #0x30]
{gate}    ldr  r0, [r7, #0x40]
    adds r0, #1
    str  r0, [r7, #0x40]
    movs r0, #0
    strb r0, [r7, #0x29]
    b.w  send
stale:
    ldrb r0, [r7, #0x29]
    adds r0, #1
    cmp  r0, #{stalemax}
    it   hs
    movhs r0, #{stalemax}
    strb r0, [r7, #0x29]
    bhs.w pass
    ldrb r0, [r7, #0x28]
    cmp  r0, r2
    beq.w drop
    ldr  r0, [r7, #0x2c]
    str  r0, [r6, #0x1c]
    ldr  r0, [r7, #0x30]
    str  r0, [r6, #0x54]
    ldr  r0, [r7, #0x48]
    adds r0, #1
    str  r0, [r7, #0x48]
    b.w  send
drop:
    ldr  r0, [r7, #0x44]
    adds r0, #1
    str  r0, [r7, #0x44]
    ldr  r0, [r5]
    mov  r1, r6
    bl   #{free}
    ldr  r0, [r5]
    movs r1, #0
    pop  {{r3, r4, r7, pc}}
pass:
    ldr  r0, [r7, #0x4c]
    adds r0, #1
    str  r0, [r7, #0x4c]
send:
    ldr  r0, [r5]
    mov  r1, r6
    pop  {{r3, r4, r7, pc}}

calc:
    push {{r4, r5, r6, r7, lr}}
    movs r2, #1
    ldrsh.w r3, [r0, #10]
    movs r4, #2
lp:
    cmp  r4, #9
    bge  found
    movs r5, #10
    mla  r12, r4, r5, r0
    ldrsh.w r5, [r12]
    cmp  r5, r3
    ble  nx
    mov  r3, r5
    mov  r2, r4
nx:
    adds r4, #1
    b    lp
found:
    movs r5, #10
    mla  r12, r2, r5, r0
    ldrsh r4, [r12, #-10]
    ldrsh.w r5, [r12, #10]
    subs r6, r5, r4
    movw r7, #1683
    mul  r6, r6, r7
    lsls r3, r3, #1
    subs r3, r3, r4
    subs r3, r3, r5
    lsls r3, r3, #1
    cmp  r3, #0
    ble  noden
    .short 0xfb96, 0xf6f3            @ sdiv r6, r6, r3 (keystone lacks it)
    b    cont
noden:
    movs r6, #0
cont:
    adds r1, r1, r2
    subs r1, #4
    mul  r1, r1, r7
    add  r1, r6
    mov  r0, r1
    pop  {{r4, r5, r6, r7, pc}}
    .align 2
lit_own: .word {own}
{btnlits}
"""
# --ownbtn (with --hold --rawhold, k 1): with a side button held, the S1 that follows an
# S2 without a pressure exchange before it reads wrong (v2.61: 41% zigzag). With k 1 that
# is the stock-loop S1 (it follows the extra S2); the extra S1 follows the stock S2, which
# follows P. Pressure-hold tags tell the S1 frames apart (ARR+3: stock S1 = 1, first extra
# S1 = 0): extra S1 results store the calc's button bits (r8+2 [2:0]) at OWN+0x2A, stock
# S1 results are dropped while that is non-zero. (v2.62 had it the other way round: 23%.)
OWN_BTN = """    ldr  r0, lit_arr
    ldrb r0, [r0, #3]
    cmp  r0, #0
    bne  stockf
    ldrb r0, [r8, #2]
    and  r0, r0, #{btnmask}
    strb r0, [r7, #0x2a]
    b    btnok
stockf:
    ldrb r0, [r7, #0x2a]
    cmp  r0, #0
    beq  btnok
    movs r0, #0
    strb r0, [r7, #0x29]
    b.w  drop
btnok:
"""
# --owncal: position routine v2 for --ownpos. Fixed point (frac in 1/1024 coil).
#  * dropout-robust: when the smaller neighbour of the peak looks like a dropout
#    (min*4 < c and max*5 < 4c), frac comes from the peak and the larger neighbour
#    with the typical peak curvature K (Q10): d = +-((m - c) / (K c) + 1/2)
#  * S-curve correction: 11-knot table per axis over frac -0.5..0.5 (linear interp),
#    fitted against Wacom's position on slow v2.53/v2.64 logs (X full, Y half).
# calc(r0 amps, r1 window start + 4, r2 table, r3 K) -> position without offset.
CALC_V2 = """
calc:
    push {{r4, r5, r6, r7, r8, r9, r10, lr}}
    mov  r9, r2
    mov  r10, r3
    movs r2, #1
    ldrsh.w r3, [r0, #10]
    movs r4, #2
lp:
    cmp  r4, #9
    bge  found
    movs r5, #10
    mla  r12, r4, r5, r0
    ldrsh.w r5, [r12]
    cmp  r5, r3
    ble  nx
    mov  r3, r5
    mov  r2, r4
nx:
    adds r4, #1
    b    lp
found:
    movs r5, #10
    mla  r12, r2, r5, r0
    ldrsh r4, [r12, #-10]
    ldrsh.w r5, [r12, #10]
    cmp  r3, #0
    ble.w zero
    cmp  r4, r5
    itete lt
    movlt r6, r4
    movge r6, r5
    movlt r7, r5
    movge r7, r4
    lsl  r12, r6, #2
    cmp  r12, r3
    bge  parab
    add.w r12, r7, r7, lsl #2
    lsl  r8, r3, #2
    cmp  r12, r8
    bge  parab
    sub  r12, r7, r3
    lsl  r12, r12, #10
    .short 0xfb9c, 0xfcf3            @ sdiv r12, r12, r3
    lsl  r12, r12, #10
    .short 0xfb9c, 0xfcfa            @ sdiv r12, r12, r10
    add  r6, r12, #512
    cmp  r5, r4
    it   lt
    rsblt r6, r6, #0
    b    have
parab:
    sub  r6, r5, r4
    lsl  r6, r6, #10
    lsl  r12, r3, #1
    sub  r12, r12, r4
    sub  r12, r12, r5
    lsl  r12, r12, #1
    cmp  r12, #0
    ble  zero
    .short 0xfb96, 0xf6fc            @ sdiv r6, r6, r12
    b    have
zero:
    movs r6, #0
have:
    add  r12, r6, #512
    cmp  r12, #0
    it   lt
    movlt r12, #0
    cmp  r12, #1024
    it   gt
    movgt r12, #1024
    add.w r8, r12, r12, lsl #2
    lsl  r8, r8, #1
    lsr  r7, r8, #10
    cmp  r7, #9
    it   gt
    movgt r7, #9
    sub  r8, r8, r7, lsl #10
    add.w r12, r9, r7, lsl #1
    ldrsh.w r4, [r12]
    ldrsh.w r5, [r12, #2]
    sub  r5, r5, r4
    mul  r5, r5, r8
    asr  r5, r5, #10
    add  r4, r4, r5
    movw r7, #1683
    add  r1, r1, r2
    subs r1, #4
    mul  r1, r1, r7
    mul  r6, r6, r7
    asr  r6, r6, #10
    add  r1, r6
    add  r1, r4
    mov  r0, r1
    pop  {{r4, r5, r6, r7, r8, r9, r10, pc}}
"""
# --ownbsw: while a side button is held, the stock loop skips the extra cycles (with a
# button the extra-cycle scans are noisy, v2.64: 10-20% glitches per scan type; the stock
# schedule is clean, v2.57). The ownpos hook keeps a countdown at OWN+0x2B: 40 on any calc
# result with button bits (r8+2 [2:0]), -1 otherwise. Stock step 29's event returns 3
# (alt3 = 23) instead of 1 while it is non-zero.
OWN_BSW = """    ldrb r0, [r8, #2]
    ands r0, r0, #7
    beq  nob
    movs r0, #40
    strb r0, [r7, #0x2b]
    b    bswd
nob:
    ldrb r0, [r7, #0x2b]
    cbz  r0, bswd
    subs r0, #1
    strb r0, [r7, #0x2b]
bswd:
"""
STEP29_BSW = """
    push {{r4, lr}}
    bl   #{inner}
    cmp  r0, #1
    bne  out
    ldr  r1, lit_own
    ldrb r1, [r1, #0x2b]
    cbz  r1, out
    movs r0, #3
out:
    pop  {{r4, pc}}
    .align 2
lit_own: .word {own}
"""
# --owngate (with --ownpos, --hold --rawhold, k 1): right at a side-button press the
# extra-cycle S1 reads ~850 counts off for a scan or two (v2.68: all 65 button zigzags
# within 30 ms of a press), before the button reaches the calc and --ownbsw switches the
# schedule. Stock-loop S1 results (tag 1) keep the last two sent positions (OWN+0x50..0x5C);
# an extra S1 result (tag 0) is dropped when it is further than 500 + |v|/2 counts from
# last + 5/8 v (v = last - previous stock position) on either axis. Drops: OWN+0x60.
OWN_GATE = """    ldr  r0, lit_arr
    ldrb r0, [r0, #3]
    cmp  r0, #0
    bne  g_stock
    ldr  r0, [r7, #0x50]
    ldr  r1, [r7, #0x58]
    ldr  r2, [r6, #0x1c]
    bl   g_chk
    cmp  r0, #0
    bne  g_drop
    ldr  r0, [r7, #0x54]
    ldr  r1, [r7, #0x5c]
    ldr  r2, [r6, #0x54]
    bl   g_chk
    cmp  r0, #0
    bne  g_drop
    b    g_ok
g_chk:
    subs r1, r0, r1
    add.w r3, r1, r1, lsl #2
    asrs r3, r3, #3
    add  r3, r0
    subs r2, r2, r3
    cmp  r2, #0
    it   lt
    rsblt r2, r2, #0
    cmp  r1, #0
    it   lt
    rsblt r1, r1, #0
    lsrs r1, r1, #1
    addw r1, r1, #500
    movs r0, #0
    cmp  r2, r1
    it   gt
    movgt r0, #1
    bx   lr
g_drop:
    ldr  r0, [r7, #0x60]
    adds r0, #1
    str  r0, [r7, #0x60]
    movs r0, #0
    strb r0, [r7, #0x29]
    b.w  drop
g_stock:
    ldr  r0, [r7, #0x50]
    str  r0, [r7, #0x58]
    ldr  r0, [r7, #0x54]
    str  r0, [r7, #0x5c]
    ldr  r0, [r6, #0x1c]
    str  r0, [r7, #0x50]
    ldr  r0, [r6, #0x54]
    str  r0, [r7, #0x54]
g_ok:
"""
# --ownanchor (with --ownsrc wacom): Wacom's final output carries a 2D correction
# (bilinear table, 0x080BAF88) that its per-measurement field r8+0xFA4/+0x10D8 lacks;
# without it slow diagonals wobble. Every tracked result stores the raw field and the
# output in 8-deep rings (OWN+0x70 raw X, +0x80 raw Y, +0xA0 out X, +0xB0 out Y, index
# +0x90). While the raw position moved < 30 internal units over 4 results, the
# correction D (+0x94 X, +0x98 Y) follows (out + 2800 - raw 4 results ago) with 1/8 per
# result (initialised to out + 2800 - raw on the first tracked result, flag +0x91).
# Fresh results send raw - 2800 + D. If Wacom's output moved < 60 counts over 4 results
# and the result differs from it by > 150 on either axis within 16 results of a change
# of the calc's button bits (button-press shake; state +0xC8 last bits, +0xC9 countdown),
# Wacom's output is sent instead (count +0xC4). Not outside that window: at the start of
# a movement Wacom's output is still at rest and would pull the cursor back.
ANC_SUBS = """
anc_upd:
    push {{r2, r4, r5, r6, lr}}
    ldrb.w r0, [r8, #2]
    and  r0, r0, #7
    ldrb r1, [r7, #0xc8]
    cmp  r0, r1
    beq  a_bsame
    strb r0, [r7, #0xc8]
    movs r0, #16
    strb r0, [r7, #0xc9]
    b    a_bdone
a_bsame:
    ldrb r0, [r7, #0xc9]
    cmp  r0, #0
    beq  a_bdone
    subs r0, #1
    strb r0, [r7, #0xc9]
a_bdone:
    ldrb r0, [r7, #0x90]
    adds r0, #1
    and  r0, r0, #7
    strb r0, [r7, #0x90]
    sub  r4, r0, #4
    and  r4, r4, #7
    addw r1, r8, #0xfa4
    ldrh r3, [r1]
    add.w r12, r7, #0x70
    strh r3, [r12, r0, lsl #1]
    ldrh r5, [r12, r4, lsl #1]
    add.w r1, r1, #0x134
    ldrh r6, [r1]
    add.w r12, r7, #0x80
    strh r6, [r12, r0, lsl #1]
    ldrh r1, [r12, r4, lsl #1]
    ldr.w r2, [r8, #0x1c]
    add.w r12, r7, #0xa0
    strh r2, [r12, r0, lsl #1]
    ldr.w r2, [r8, #0x54]
    add.w r12, r7, #0xb0
    strh r2, [r12, r0, lsl #1]
    ldrb r2, [r7, #0x91]
    cmp  r2, #0
    bne  a_upd
    movs r2, #1
    strb r2, [r7, #0x91]
    ldr.w r2, [r8, #0x1c]
    addw r2, r2, #2800
    sub  r2, r2, r3
    str  r2, [r7, #0x94]
    ldr.w r2, [r8, #0x54]
    addw r2, r2, #2840
    sub  r2, r2, r6
    str  r2, [r7, #0x98]
    b    a_done
a_upd:
    subs r2, r3, r5
    it   lt
    rsblt r2, r2, #0
    cmp  r2, #30
    bge  a_done
    subs r2, r6, r1
    it   lt
    rsblt r2, r2, #0
    cmp  r2, #30
    bge  a_done
    ldr.w r2, [r8, #0x1c]
    addw r2, r2, #2800
    sub  r2, r2, r5
    ldr  r3, [r7, #0x94]
    sub  r2, r2, r3
    asr  r2, r2, #3
    add  r3, r2
    str  r3, [r7, #0x94]
    ldr.w r2, [r8, #0x54]
    addw r2, r2, #2840
    sub  r2, r2, r1
    ldr  r3, [r7, #0x98]
    sub  r2, r2, r3
    asr  r2, r2, #3
    add  r3, r2
    str  r3, [r7, #0x98]
a_done:
    pop  {{r2, r4, r5, r6, pc}}

anc_fix:
    push {{r2, r4, lr}}
    ldr  r0, [r6, #0x1c]
    ldr  r1, [r7, #0x94]
    add  r0, r1
    cmp  r0, #0
    it   lt
    movlt r0, #0
    movw r1, #44800
    cmp  r0, r1
    it   gt
    movgt r0, r1
    str  r0, [r6, #0x1c]
    ldr  r0, [r6, #0x54]
    ldr  r1, [r7, #0x98]
    add  r0, r1
    cmp  r0, #0
    it   lt
    movlt r0, #0
    movw r1, #29600
    cmp  r0, r1
    it   gt
    movgt r0, r1
    str  r0, [r6, #0x54]
    ldrb r2, [r7, #0xc9]
    cmp  r2, #0
    beq  f_done
    ldrb r2, [r7, #0x90]
    sub  r4, r2, #4
    and  r4, r4, #7
    add.w r12, r7, #0xa0
    ldrh r0, [r12, r2, lsl #1]
    ldrh r1, [r12, r4, lsl #1]
    subs r0, r0, r1
    it   lt
    rsblt r0, r0, #0
    cmp  r0, #60
    bge  f_done
    add.w r12, r7, #0xb0
    ldrh r0, [r12, r2, lsl #1]
    ldrh r1, [r12, r4, lsl #1]
    subs r0, r0, r1
    it   lt
    rsblt r0, r0, #0
    cmp  r0, #60
    bge  f_done
    ldr  r0, [r6, #0x1c]
    ldr.w r1, [r8, #0x1c]
    subs r0, r0, r1
    it   lt
    rsblt r0, r0, #0
    cmp  r0, #150
    bgt  f_sub
    ldr  r0, [r6, #0x54]
    ldr.w r1, [r8, #0x54]
    subs r0, r0, r1
    it   lt
    rsblt r0, r0, #0
    cmp  r0, #150
    ble  f_done
f_sub:
    ldr.w r0, [r8, #0x1c]
    str  r0, [r6, #0x1c]
    ldr.w r0, [r8, #0x54]
    str  r0, [r6, #0x54]
    ldr  r0, [r7, #0xc4]
    adds r0, #1
    str  r0, [r7, #0xc4]
f_done:
    ldr  r0, [r6, #0x1c]
    str  r0, [r7, #0x2c]
    ldr  r0, [r6, #0x54]
    str  r0, [r7, #0x30]
    pop  {{r2, r4, pc}}
"""
# --nosmooth (with --ownpos): the calc runs its position through linearization, tilt and
# pressure compensation and then, if feature flag 0x08 is set (flag word read by
# 0x080BB5D6, 0x2001F06C in slot B, stock value 0x38), stores a moving average /
# stationary filter result over it (0x080BC3B4..C3C4): Wacom's 5-10 ms lag and the equal
# interpolation steps. The ownpos hook clears bit 0x08 on every calc result, except for 16
# results after a change of the calc's button bits (the pen signal shakes right after a
# press; OWN+0xC8 last bits, +0xC9 countdown), where it sets it again. The filter state is
# kept up to date by the calc either way.
OWN_NOSMOOTH = """    ldrb.w r0, [r8, #2]
    and  r0, r0, #7
    ldrb r1, [r7, #0xc8]
    cmp  r0, r1
    beq  ns_same
    strb r0, [r7, #0xc8]
    movs r0, #16
    strb r0, [r7, #0xc9]
    b    ns_set
ns_same:
    ldrb r0, [r7, #0xc9]
    cmp  r0, #0
    beq  ns_clr
    subs r0, #1
    strb r0, [r7, #0xc9]
ns_set:
    movw r0, #{flo}
    movt r0, #{fhi}
    ldr  r1, [r0]
    orr  r1, r1, #8
    str  r1, [r0]
    b    ns_done
ns_clr:
    movw r0, #{flo}
    movt r0, #{fhi}
    ldr  r1, [r0]
    bic  r1, r1, #8
    str  r1, [r0]
ns_done:
"""
# --phasegate N (with --ownpos): the phase (degrees, byte +8 of a coil entry) at the X
# peak coil is ~45 deg and steady while drawing (v2.53/v2.64 logs: consecutive fresh
# results differ by <= 1 deg in 99%); a side button shifts the pen's resonance and the
# phase jumps (~58 deg), at the very first affected scan, before the calc's button bits
# change (v2.72: a 300-450 count bump starting ~8 ms before the button bit). A fresh result
# whose phase differs from the previous fresh one by more than N degrees is dropped, and
# output resumes after two consecutive results agree. State: OWN+0xCA last phase (0xFF =
# none, set when tracking starts), +0xCB agreeing results since the jump, +0x64 drops.
OWN_PHASEGATE = """    add.w r0, r4, #17
    movs r1, #1
    ldrsh.w r2, [r0, #10]
    movs r3, #2
pg_lp:
    cmp  r3, #9
    bge  pg_found
    add.w r12, r3, r3, lsl #2
    ldrsh.w r12, [r0, r12, lsl #1]
    cmp  r12, r2
    ble  pg_nx
    mov  r2, r12
    mov  r1, r3
pg_nx:
    adds r3, #1
    b    pg_lp
pg_found:
    add.w r12, r1, r1, lsl #2
    add.w r12, r0, r12, lsl #1
    ldrb.w r1, [r12, #8]
    ldrb r2, [r7, #0xca]
    strb r1, [r7, #0xca]
    cmp  r2, #0xff
    beq  pg_init
    subs r2, r1, r2
    it   lt
    rsblt r2, r2, #0
    cmp  r2, #{thr}
    bgt  pg_jump
    ldrb r2, [r7, #0xcb]
    cmp  r2, #1
    bhs  pg_ok
    adds r2, #1
    strb r2, [r7, #0xcb]
    b    pg_drop
pg_jump:
    movs r2, #0
    strb r2, [r7, #0xcb]
pg_drop:
    ldr  r0, [r7, #0x64]
    adds r0, #1
    str  r0, [r7, #0x64]
    movs r0, #0
    strb r0, [r7, #0x29]
    b.w  drop
pg_init:
    movs r2, #1
    strb r2, [r7, #0xcb]
pg_ok:
"""
# --pressgate (with --ownpos): v2.73 still had single 600-1000 count spikes 2-3 ms after
# a side-button press while hovering, on scans whose phase did not jump. For 12 fresh
# results after any change of the calc's button bits (OWN+0xC8 bits, +0xC9 countdown), a
# fresh result is dropped when it is further than 250 + (1.5 + n) |v| from last + v
# (v = last - previous accepted position, OWN+0xD0/+0xD4 last, +0xD8/+0xDC previous;
# n = fresh results since the last accepted one, OWN+0xF4, so a pen that moved on while
# scans were dropped is not locked out - v2.75/v2.76 froze 60-120 ms after presses).
# Never two drops in a row (OWN+0xF5): the spikes are single scans, and a click that starts
# a fast movement (v2.77: up to 83 ms frozen) must not be held. Drops: OWN+0xE0.
OWN_PRESSGATE = """    ldrb.w r0, [r8, #2]
    and  r0, r0, #7
    ldrb r1, [r7, #0xc8]
    cmp  r0, r1
    beq  pz_same
    strb r0, [r7, #0xc8]
    movs r0, #12
    strb r0, [r7, #0xc9]
    b    pz_chk
pz_same:
    ldrb r0, [r7, #0xc9]
    cmp  r0, #0
    beq  pz_upd
    subs r0, #1
    strb r0, [r7, #0xc9]
pz_chk:
    ldrb r0, [r7, #0xf5]
    cmp  r0, #0
    bne  pz_upd
    ldrb r3, [r7, #0xf4]
    ldr  r0, [r7, #0xd0]
    ldr  r1, [r7, #0xd8]
    ldr  r2, [r6, #0x1c]
    bl   pz_dev
    cmp  r0, #0
    bne  pz_drop
    ldrb r3, [r7, #0xf4]
    ldr  r0, [r7, #0xd4]
    ldr  r1, [r7, #0xdc]
    ldr  r2, [r6, #0x54]
    bl   pz_dev
    cmp  r0, #0
    bne  pz_drop
    b    pz_upd
pz_dev:
    subs r1, r0, r1
    subs r2, r2, r0
    subs r2, r2, r1
    cmp  r2, #0
    it   lt
    rsblt r2, r2, #0
    cmp  r1, #0
    it   lt
    rsblt r1, r1, #0
    mul  r3, r3, r1
    add.w r1, r1, r1, lsr #1
    add  r1, r3
    add.w r1, r1, #250
    movs r0, #0
    cmp  r2, r1
    it   gt
    movgt r0, #1
    bx   lr
pz_drop:
    movs r0, #1
    strb r0, [r7, #0xf5]
    ldr  r0, [r7, #0xe0]
    adds r0, #1
    str  r0, [r7, #0xe0]
    movs r0, #0
    strb r0, [r7, #0x29]
    b.w  drop
pz_upd:
    movs r0, #0
    strb r0, [r7, #0xf4]
    strb r0, [r7, #0xf5]
    ldr  r0, [r7, #0xd0]
    str  r0, [r7, #0xd8]
    ldr  r0, [r7, #0xd4]
    str  r0, [r7, #0xdc]
    ldr  r0, [r6, #0x1c]
    str  r0, [r7, #0xd0]
    ldr  r0, [r6, #0x54]
    str  r0, [r7, #0xd4]
"""
# --btnkeep (with --ownpos and the gates): a gate drop, or a skipped non-fresh result, must
# never lose a side-button change (v2.74: quick clicks mid-stroke were not seen, all reports
# carrying the button fell into the dropped transition scans). Every sent result records its
# button bits (mail +2 [2:0]) at OWN+0xE4 and its position at +0xE8/+0xEC (previous one at
# +0xD8/+0xDC, used by --pressgate). A dropped result whose button bits differ is sent
# instead, with the last sent position (count +0xF0); such button-only sends update the
# button record but not the position history (v2.75 did, which zeroed the press gate's
# velocity and dropped a moving pen's scans for 60-120 ms after each press).
OWN_GDROP = """gdrop:
    ldrb r0, [r6, #2]
    and  r0, r0, #7
    ldrb r1, [r7, #0xe4]
    cmp  r0, r1
    beq.w drop
gsend:
    ldr  r0, [r7, #0xe8]
    str  r0, [r6, #0x1c]
    ldr  r0, [r7, #0xec]
    str  r0, [r6, #0x54]
    ldr  r0, [r7, #0xf0]
    adds r0, #1
    str  r0, [r7, #0xf0]
    ldrb r0, [r6, #2]
    and  r0, r0, #7
    strb r0, [r7, #0xe4]
    b.w  send_nr
"""
OWN_SENDREC = """    ldrb r0, [r6, #2]
    and  r0, r0, #7
    strb r0, [r7, #0xe4]
    ldr  r0, [r6, #0x1c]
    str  r0, [r7, #0xe8]
    ldr  r0, [r6, #0x54]
    str  r0, [r7, #0xec]
"""
# --btndeb (with --ownpos): Wacom's calc flickers the side-button bits for a result or two
# around a press (v2.77/v2.78 forwarded that: 28-39 one-report flickers per minute, v2.74 6,
# stock schedule 0). A button state counts once it held for N consecutive results (v2.79's
# N = 2 let the k 1 pattern through: 3 results with, 3 without the button while it is held)
# (candidate OWN+0xF6, count +0xF7, debounced +0xF8); every mail gets the debounced bits in
# +2 [2:0], so --btnkeep only forwards real changes.
OWN_BTNDEB = """    ldrb.w r0, [r8, #2]
    and  r0, r0, #7
    ldrb r1, [r7, #0xf6]
    ldrb r2, [r7, #0xf7]
    cmp  r0, r1
    ite  eq
    addeq r2, #1
    movne r2, #1
    strb r0, [r7, #0xf6]
    cmp  r2, #{deb}
    it   hs
    strbhs r0, [r7, #0xf8]
    cmp  r2, #{deb}
    it   hi
    movhi r2, #{deb}
    strb r2, [r7, #0xf7]
    ldrb r0, [r7, #0xf8]
    and  r0, r0, #7
    ldrb r1, [r6, #2]
    bic  r1, r1, #7
    orr  r1, r1, r0
    strb r1, [r6, #2]
"""
# --fastpass N (with --phasegate/--pressgate): while the pen moves fast (sum of |dx|+|dy| of
# the last two accepted positions > N counts per fresh result, OWN+0xF9), transition scans
# are sent instead of dropped (user choice: a hold-then-catch-up across the ~15 ms press
# transition is worse than 2-3 slightly-off points in a fast stroke).
OWN_FASTFLAG = """    ldr  r0, [r7, #0xd0]
    ldr  r1, [r7, #0xd8]
    subs r0, r0, r1
    it   lt
    rsblt r0, r0, #0
    ldr  r1, [r7, #0xd4]
    ldr  r2, [r7, #0xdc]
    subs r1, r1, r2
    it   lt
    rsblt r1, r1, #0
    add  r0, r1
    cmp  r0, #{fast}
    ite  hi
    movhi r0, #1
    movls r0, #0
    strb r0, [r7, #0xf9]
"""
OWN_CMP = """    ldrh.w r0, [r4, #{src}]
    ldrh.w r1, [r7, #{dst}]
    cmp  r0, r1
    it   ne
    movne r12, #1
    strh.w r0, [r7, #{dst}]
"""
# --calclog (diagnostic, observe only): at the calc mail put, every 128th result:
# entry +0 seq (written last), +4 result number, +0x10 X block (0x75 B from r8+0x798),
# +0x88 Y block (r8+0x842), +0x100 calc RAM 0x20010E0C..+0x12F4. 4 x 0x1800 B ring at
# COILLOG, state COILLOG_IDX: +0 entries logged, +4 results seen.
CALCLOG_BASE, CALCLOG_N, CALCLOG_ESZ = 0x20010E0C, 0x12F4, 0x1800
CALCLOG_HOOK = """
    push {{r4, lr}}
    ldr  r1, lit_idx
    ldr  r0, [r1, #4]
    adds r0, #1
    str  r0, [r1, #4]
    ands r2, r0, #127
    bne.w skip
    ldr  r0, [r1]
    adds r0, #1
    and  r3, r0, #3
    movw r2, #0x1800
    mul  r3, r3, r2
    ldr  r4, lit_log
    add  r4, r3
    movs r2, #0
    str  r2, [r4]
    ldr  r2, [r1, #4]
    str  r2, [r4, #4]
    add.w r0, r4, #0x10
    add.w r1, r8, #0x798
    movs r2, #0x75
    bl   #{memcpy}
    add.w r0, r4, #0x88
    addw r1, r8, #0x842
    movs r2, #0x75
    bl   #{memcpy}
    add.w r0, r4, #0x100
    ldr  r1, lit_calc
    movw r2, #{n}
    bl   #{memcpy}
    ldr  r1, lit_idx
    ldr  r0, [r1]
    adds r0, #1
    str  r0, [r4]
    str  r0, [r1]
skip:
    ldr  r0, [r5]
    mov  r1, r6
    pop  {{r4, pc}}
    .align 2
lit_idx:  .word {idx}
lit_log:  .word {log}
lit_calc: .word {calc}
"""
# --poslog (diagnostic, observe only): bursts of 32 consecutive calc results every 512,
# 128 B entries in a 64-entry ring at COILLOG (state COILLOG_IDX: +0 logged, +4 seen).
# Entry: +0 seq (written last), +4 result no., +8 out X, +0xC out Y (r8+0x1C/+0x54),
# +0x10 u16 r8+0xFA0, +0xFA4, +0x10D4, +0x10D8, +0x18 X window start, +0x19 Y start,
# +0x1A u16 [r8] (pressure), +0x1C X amps (10 x s16), +0x30 Y amps, +0x44 r8+2 buttons.
POSLOG_HOOK = """
    push {{r4, lr}}
    ldr  r1, lit_idx
    ldr  r0, [r1, #4]
    adds r0, #1
    str  r0, [r1, #4]
    ubfx r2, r0, #0, #9
    cmp  r2, #32
    bhs.w skip
    ldr  r0, [r1]
    adds r0, #1
    and  r3, r0, #63
    ldr  r4, lit_log
    add.w r4, r4, r3, lsl #7
    movs r2, #0
    str  r2, [r4]
    ldr  r2, [r1, #4]
    str  r2, [r4, #4]
    ldr.w r2, [r8, #0x1c]
    str  r2, [r4, #8]
    ldr.w r2, [r8, #0x54]
    str  r2, [r4, #12]
    addw r3, r8, #0xfa0
    ldrh r2, [r3]
    strh r2, [r4, #0x10]
    ldrh r2, [r3, #4]
    strh r2, [r4, #0x12]
    add.w r3, r3, #0x134
    ldrh r2, [r3]
    strh r2, [r4, #0x14]
    ldrh r2, [r3, #4]
    strh r2, [r4, #0x16]
    add.w r3, r8, #0x798
    ldrb r2, [r3, #0x0c]
    strb r2, [r4, #0x18]
    ldrb.w r2, [r3, #0xb6]
    strb r2, [r4, #0x19]
    ldrh.w r2, [r8]
    strh r2, [r4, #0x1a]
    ldrb.w r2, [r8, #2]
    strb.w r2, [r4, #0x44]
{copy}
    ldr  r1, lit_idx
    ldr  r0, [r1]
    adds r0, #1
    str  r0, [r4]
    str  r0, [r1]
skip:
    ldr  r0, [r5]
    mov  r1, r6
    pop  {{r4, pc}}
    .align 2
lit_idx: .word {idx}
lit_log: .word {log}
"""
# --watch (diagnostic): find the code that writes given RAM words with the Cortex-M4 DWT
# data watchpoints. The DebugMonitor vector (image+0x30, stock: bx lr) points to WATCH_DM;
# the calc mail hook arms (once, magic at WATCH+4): DEMCR TRCENA|MON_EN, COMPn = address,
# MASKn = 0, FUNCTIONn = 6 (write). The handler logs the stacked PC (instruction after the
# write) into 16 slots {pc, hits, comparator bits} at WATCH+0x10; total hits at WATCH+0;
# all comparators are switched off after 5000 hits.
WATCH = 0x2003FA00
WATCH_MAGIC = 0x57415443
WATCH_SETUP = """
    push {{r4, lr}}
    ldr  r0, lit_w
    ldr  r1, [r0, #4]
    ldr  r2, lit_magic
    cmp  r1, r2
    beq  w_done
    str  r2, [r0, #4]
    movs r1, #0
    str  r1, [r0]
    add.w r3, r0, #0x10
    movs r4, #48
w_clr:
    str  r1, [r3], #4
    subs r4, #1
    bne  w_clr
    ldr  r2, lit_demcr
    ldr  r1, [r2]
    orr  r1, r1, #0x01000000
    orr  r1, r1, #0x00010000
    str  r1, [r2]
    ldr  r2, lit_dwt
    ldr  r1, lit_a0
    str  r1, [r2, #0x20]
    movs r1, #0
    str  r1, [r2, #0x24]
    movs r1, #6
    str  r1, [r2, #0x28]
    ldr  r1, lit_a1
    str  r1, [r2, #0x30]
    movs r1, #0
    str  r1, [r2, #0x34]
    movs r1, #6
    str  r1, [r2, #0x38]
    ldr  r1, lit_a2
    str  r1, [r2, #0x40]
    movs r1, #0
    str  r1, [r2, #0x44]
    movs r1, #6
    str  r1, [r2, #0x48]
w_done:
    ldr  r0, [r5]
    mov  r1, r6
    pop  {{r4, pc}}
    .align 2
lit_w:     .word {w}
lit_magic: .word {magic}
lit_demcr: .word 0xE000EDFC
lit_dwt:   .word 0xE0001000
lit_a0:    .word {a0}
lit_a1:    .word {a1}
lit_a2:    .word {a2}
"""
WATCH_DM = """
    tst  lr, #4
    bne  w_psp
    .short 0xf3ef, 0x8008            @ mrs r0, msp
    b    w_got
w_psp:
    .short 0xf3ef, 0x8009            @ mrs r0, psp
w_got:
    ldr  r1, [r0, #24]
    ldr  r3, lit_dwt2
    ldr  r0, [r3, #0x28]
    ubfx r0, r0, #24, #1
    ldr  r2, [r3, #0x38]
    ubfx r2, r2, #24, #1
    orr  r0, r0, r2, lsl #1
    ldr  r2, [r3, #0x48]
    ubfx r2, r2, #24, #1
    orr  r0, r0, r2, lsl #2
    ldr  r2, lit_w2
    ldr  r3, [r2]
    adds r3, #1
    str  r3, [r2]
    movw r12, #5000
    cmp  r3, r12
    blo  w_keep
    ldr  r3, lit_dwt2
    movs r12, #0
    str  r12, [r3, #0x28]
    str  r12, [r3, #0x38]
    str  r12, [r3, #0x48]
w_keep:
    add.w r2, r2, #0x10
    movs r3, #16
w_loop:
    ldr  r12, [r2]
    cmp  r12, r1
    beq  w_found
    cmp  r12, #0
    beq  w_new
    adds r2, #12
    subs r3, #1
    bne  w_loop
    bx   lr
w_new:
    str  r1, [r2]
w_found:
    ldr  r12, [r2, #4]
    add  r12, r12, #1
    str  r12, [r2, #4]
    ldr  r12, [r2, #8]
    orr  r12, r12, r0
    str  r12, [r2, #8]
    bx   lr
    .align 2
lit_dwt2: .word 0xE0001000
lit_w2:   .word {w}
"""
USB_READY_SIG = re.compile(rb"\xdf\xf8..\x90\xf8\xfc\x01\x03\x28\x11\xd1\xdf\xf8..\xd0\xf8\x18\x02"
                           rb"\x00\x28\x0b\xd0\xdf\xf8..\xd1\xf8\x18\x12\x00\x29\x05\xd0\x90\xf8\x10\x02", re.S)
USB_DATAIN_SIG = re.compile(rb"\x00\xb5\x83\xb0\xd0\xf8\x18\x02\x00\x28\x01\xd1\x02\x20..\xc9\xb2"
                            rb"\x01\x29\x03\xd1\x00\x21\x80\xf8\x10\x12", re.S)


def resolve_usb(blob, base, md):
    """usbif report sender: ready check, queues on endpoint 0x81, DataIn callback."""
    md.detail = True
    w = lambda a: struct.unpack_from("<I", blob, a - base)[0]
    lit = lambda i: w(((i.address + 4) & ~3) + i.operands[1].mem.disp)

    def one(sig, what):
        h = [m.start() for m in sig.finditer(blob)]
        if len(h) != 1:
            raise SystemExit(f"{what}: {len(h)} signature hits - aborting")
        return base + h[0]
    ready = one(USB_READY_SIG, "usb ready check")
    datain = one(USB_DATAIN_SIG, "HID DataIn callback")
    pdev = lit(next(md.disasm(bytes(blob[ready - base:ready - base + 4]), ready)))
    sites, queues = [], []
    for o in range(0, len(blob) - 4, 2):
        if struct.unpack_from("<H", blob, o)[0] & 0xF800 != 0xF000:
            continue
        i = next(md.disasm(bytes(blob[o:o + 4]), base + o, 1), None)
        if not (i and i.mnemonic == "bl" and i.operands[0].imm == ready):
            continue
        B = list(md.disasm(bytes(blob[o:o + 0x80]), base + o))
        if not (B[1].op_str == "r0, #0" and B[2].mnemonic == "beq" and "pc" in B[3].op_str
                and B[4].op_str == "r0, [r0, #4]"):
            continue
        cp = next(k for k, x in enumerate(B) if k > 5 and x.mnemonic == "bl"
                  and B[k - 1].mnemonic == "movs" and B[k - 1].op_str.startswith("r2, #"))
        n = B[cp - 1].operands[1].imm
        snd = next(x for k, x in enumerate(B) if k > cp and x.mnemonic == "bl"
                   and B[k - 2].mnemonic == "movs" and B[k - 2].operands[1].imm == n)
        sites.append(i.address)
        queues.append((lit(B[3]), lit(B[cp - 3]), n, snd.operands[0].imm))
    B = list(md.disasm(bytes(blob[sites[0] - base:sites[0] - base + 0x60]), sites[0]))
    bls = [x.operands[0].imm for x in B if x.mnemonic == "bl"]
    md.detail = False
    return dict(ready=ready, datain=datain, pdev=pdev, sites=sites, queues=queues,
                get=bls[1], memcpy=bls[2], free=bls[3])


# HID task pacing (--k 0): the hidcontroller loop ends in osDelay(1), so an
# iteration that runs late (preempted, long work) loses a whole tick - measured
# 914-968 iterations/s while hovering. PACE keeps a due time on DWT->CYCCNT
# (UPS+0xF0): on time -> osDelay(1) as before; behind -> return at once so the
# missed iteration is made up; >4 ms off either way -> resync.
PACE = """
    push {{r4, lr}}
    ldr  r4, lit_ups
    ldr  r0, lit_demcr
    ldr  r1, [r0]
    orr  r1, r1, #0x1000000
    str  r1, [r0]
    ldr  r0, lit_cyc
    ldr  r1, [r0, #-4]
    orr  r1, r1, #1
    str  r1, [r0, #-4]
    ldr  r1, lit_cyc
    ldr  r1, [r1]
    ldr  r2, [r4, #0xf0]
    ldr  r3, lit_period
    add  r2, r2, r3
    subs r0, r1, r2
    ldr  r3, lit_resync
    cmp  r0, r3
    bge  resync
    rsb  r3, r3, #0
    cmp  r0, r3
    ble  resync
    cmp  r0, #0
    blt  sleep
    str  r2, [r4, #0xf0]
    pop  {{r4, pc}}
resync:
    mov  r2, r1
sleep:
    str  r2, [r4, #0xf0]
    movs r0, #1
    pop  {{r4, lr}}
    b.w  #{delay}
    .align 2
lit_ups:    .word {ups}
lit_cyc:    .word {cyc}
lit_period: .word {period}
lit_resync: .word {resync}
lit_demcr:  .word {demcr}
"""


def resolve_pace(blob, base, md, report_site):
    """osDelay(1) at the end of the hidcontroller loop that calls the report assembler."""
    o = report_site - base
    while blob[o:o + 4] != bytes.fromhex("2de9f047"):
        o -= 2
        assert report_site - base - o < 0x400, "report assembler start not found"
    fn = base + o
    for q in range(0, len(blob) - 4, 2):
        if struct.unpack_from("<H", blob, q)[0] & 0xF800 != 0xF000:
            continue
        i = next(md.disasm(bytes(blob[q:q + 4]), base + q, 1), None)
        if i and i.mnemonic == "bl" and int(i.op_str.lstrip("#"), 16) == fn:
            nxt = next(md.disasm(bytes(blob[q + 4:q + 6]), base + q + 4, 1))
            assert nxt.mnemonic == "b", nxt
            tail = int(nxt.op_str.lstrip("#"), 16)
            L = list(md.disasm(bytes(blob[tail - base:tail - base + 16]), tail))
            k = next(k for k, x in enumerate(L) if x.mnemonic == "bl")
            assert L[k - 1].mnemonic == "movs" and L[k - 1].op_str == "r0, #1", L[k - 1]
            return L[k].address, int(L[k].op_str.lstrip("#"), 16)
    raise SystemExit("hidcontroller loop not found")


HID_PEN_SIG = re.compile(rb"\x00\x22.\x48\x01\x68\x68\x46....\x00\x98\x20\x28.\xd1\x01\x9d", re.S)


def resolve_hid(blob, base, md):
    """Addresses of the HID pen routine and the report builder it uses."""
    md.detail = True
    w = lambda a: struct.unpack_from("<I", blob, a - base)[0]
    lit = lambda i: w(((i.address + 4) & ~3) + i.operands[1].mem.disp)
    bl = lambda i: i.operands[0].imm
    dis = lambda a, n: list(md.disasm(bytes(blob[a - base:a - base + 4 * n]), a))[:n]
    sigs = [m.start() for m in HID_PEN_SIG.finditer(blob)]
    if len(sigs) != 1:
        raise SystemExit(f"HID pen routine: {len(sigs)} signature hits - aborting")
    fn = base + sigs[0] - 0xC
    L = dis(fn, 120)
    assert L[0].op_str == "{r1, r2, r3, r4, r5, lr}", L[0].op_str
    bls = [i for i in L if i.mnemonic == "bl"]
    r = dict(fn=fn, copy_site=bls[1].address, copy=bl(bls[1]), free=bl(bls[2]),
             usbstate=bl(bls[3]))
    r["cur"] = lit(next(i for i in L if i.mnemonic == "ldr" and "pc" in i.op_str
                        and i.address >= r["copy_site"] - 8))
    last = [i for i in L if i.mnemonic == "mov.w" and i.op_str.endswith("#0x798")][-1]
    k = L.index(last)
    r["prev"] = lit(L[k - 2])
    assert lit(L[k - 1]) == r["cur"], "HID final copy prev <- cur not found"
    r["records"] = bl([i for i in L[:k] if i.mnemonic == "bl"][-2])
    B = dis(r["records"], 60)
    r["builder"] = bl(next(i for i in B if i.mnemonic == "bl"))
    r["recptr"] = next(lit(i) for j, i in enumerate(B)
                       if i.mnemonic in ("ldr", "ldr.w") and "pc" in i.op_str
                       and B[j + 1].op_str == "r0, [r0]")
    callers = []
    for o in range(0, len(blob) - 4, 2):
        if struct.unpack_from("<H", blob, o)[0] & 0xF800 == 0xF000:
            for i in md.disasm(bytes(blob[o:o + 4]), base + o, 1):
                if i.mnemonic == "bl" and bl(i) == fn:
                    callers.append(i.address)
    r["callers"] = callers
    # USB report post after the call: movs.w r1, #-1; ldr r0, =usbq; ldr r0, [r0]; bl alloc
    C = dis(callers[0], 40) if len(callers) == 1 else []
    for j, i in enumerate(C):
        if i.mnemonic == "bl" and C[j - 1].op_str == "r0, [r0]" and "#-1" in C[j - 3].op_str:
            r["alloc"], r["usbq"] = bl(i), lit(C[j - 2])
            break
    md.detail = False
    return r
# Hold state in no-init SRAM (bootloader-only area; not in any frame data, which
# the calc reads - v1.87..v1.98 kept the tag in the stage tail and that broke the
# calc's pressure): +0..2 tag per ring slot, +3 tag of the frame being processed,
# +4 u16 pressure, +6 level, +7 buttons, +8 u16 hover, +10/+12 u16 fresh/stale.
ARR = 0x2003FFD0
CALC_SIG = bytes.fromhex("064641464ff4f36200f06df82868314602f02afd")  # memcpy(mail, r8, 0x798); ldr r0,[r5]; mov r1,r6; bl put
LOOP_SIG = bytes.fromhex("fef797fafef707f8dee7")           # bl bb5cc; bl calc_one; b loop
RING_IDX = 0x2000F8E8 + 0x151E                             # u16 write, u16 read
POP_SIG = bytes.fromhex("4ff4f360dff880834044")            # mov.w r0,#0x798; ldr.w r8,=calc; add r0,r8; (bl pop)
HID_SIG = re.compile(rb"\x00\x22.\x48\x01\x68\x68\x46(....)\x00\x98\x20\x28.\xd1\x01\x9d", re.S)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--slot", choices=("a", "b"), required=True)
    ap.add_argument("--k", type=int, required=True)
    ap.add_argument("--s2", type=int, default=1, help="S2 scans per extra cycle")
    ap.add_argument("--rawhold", action="store_true",
                    help="with --hold tag infra: hold raw pressure before the calc's "
                         "median filter instead of holding the output")
    ap.add_argument("--btnswitch", action="store_true",
                    help="run stock loops (no extra cycles) while a side button is held")
    ap.add_argument("--upsample", type=float, default=None, metavar="MS",
                    help="with --btnswitch: constant 1 kHz reports in button mode, "
                         "positions interpolated MS ms behind the newest frame")
    ap.add_argument("--rawsimple", action="store_true",
                    help="v2.10/v2.11 raw hold (pressure only)")
    ap.add_argument("--usbpush", action="store_true",
                    help="send each USB report as soon as endpoint 0x81 frees (implied by --upsample)")
    ap.add_argument("--pace", action="store_true",
                    help="fixed-rate 1 ms HID loop (implied by --upsample)")
    ap.add_argument("--pressfix", action="store_true",
                    help="with --btnswitch: hold pen input/state through the press "
                         "switch window, keep the raw hold current in button mode")
    ap.add_argument("--nobuttons", action="store_true",
                    help="side-button bits always 0 in the calc output (buttons disabled)")
    ap.add_argument("--tearhold", action="store_true",
                    help="--k 0: hold a stroke across short tracking drop-outs (no pen-up for <= 80 ms "
                         "after firm contact), nothing else changed")
    ap.add_argument("--nomedian", action="store_true",
                    help="--k 0: bypass the calc's median-of-3 on raw pressure (contact and pressure "
                         "follow each reading instead of lagging one)")
    ap.add_argument("--calcwake", action="store_true",
                    help="--k 0: calc task wakes on each frame push (IRQ 7 kick + queue), drains the ring")
    ap.add_argument("--hidwake", action="store_true",
                    help="--k 0 --usbpush: HID loop wakes on calc mail and sends at once")
    ap.add_argument("--latmeter", action="store_true",
                    help="--k 0: measure frame push -> HID report latency (RAM histogram at LAT)")
    ap.add_argument("--safemail", action="store_true",
                    help="no NULL mails, free blocks whose put failed (calc and HID->USB); "
                         "implied by --upsample")
    ap.add_argument("--protect", action="store_true",
                    help="extra-cycle events cannot advance the pen-lost counters")
    ap.add_argument("--mailhold", action="store_true",
                    help="with --rawhold: also hold level/buttons/hover/tilt on stale outputs")
    ap.add_argument("--rawlog", action="store_true",
                    help="debug with --rawhold: log the calc's per-pen struct per frame")
    ap.add_argument("--phasefix", action="store_true",
                    help="extra-cycle S2 entries get the post-pressure phase fields "
                         "(amplitude stays fresh); use without --hold")
    ap.add_argument("--unfreeze", action="store_true",
                    help="with --hold: replace frozen (repeated) positions while moving")
    ap.add_argument("--restore", default=None,
                    help="ADDR:LEN[,ADDR:LEN...] calc RAM ranges restored after stale frames")
    ap.add_argument("--diffmap", action="store_true",
                    help="debug: map calc RAM words changed by fresh / stale frames")
    ap.add_argument("--fixpa", action="store_true",
                    help="restore the post-pressure S2 pass-a entries into extra-cycle frames")
    ap.add_argument("--s2only", action="store_true",
                    help="extra cycles are S2 scans only (one evenly spaced frame per "
                         "~1.09 ms, no S2 pass-a frame); S1 runs in the stock loop")
    ap.add_argument("--interleave", action="store_true",
                    help="extra cycle as S1a, S2a, S1b, S2b so its 3 frames are evenly "
                         "spaced (~0.83/0.9/1.0 ms) instead of 1.67/0.19/0.9 ms")
    ap.add_argument("--burst26", action="store_true",
                    help="run step 26's action as a real burst in each extra cycle")
    ap.add_argument("--ownpos", action="store_true",
                    help="own position from raw coil profiles, real measurements only (k 0)")
    ap.add_argument("--s2short", action="store_true",
                    help="extra cycles: S1 + only the short S2 pass a (experimental)")
    ap.add_argument("--s1only", action="store_true",
                    help="extra cycles are coordinate scans only (S1), no pen-data readout")
    ap.add_argument("--ownanchor", action="store_true",
                    help="--ownsrc wacom: add the slowly learned output correction, substitute Wacom output on shake")
    ap.add_argument("--owngate", action="store_true",
                    help="--ownpos: drop extra-cycle positions far from the stock-loop prediction")
    ap.add_argument("--fastpass", type=int, default=0, metavar="N",
                    help="--phasegate/--pressgate: no drops while the pen moves faster than N counts/result")
    ap.add_argument("--btndeb", type=int, default=0, metavar="N",
                    help="--ownpos: debounce side-button bits over N results (k 1: button bits come in "
                         "blocks of 3 good / 3 bad results while a button is held, so N >= 4)")
    ap.add_argument("--btnkeep", action="store_true",
                    help="--ownpos: never drop a result that changes the side-button bits")
    ap.add_argument("--pressgate", action="store_true",
                    help="--ownpos: strict outlier drop for 12 fresh results after a button change")
    ap.add_argument("--phasegate", type=int, default=0, metavar="DEG",
                    help="--ownpos: drop fresh results whose peak phase jumps more than DEG")
    ap.add_argument("--nosmooth-win", dest="nosmooth_win", type=int, default=16,
                    help="--nosmooth: results with smoothing on after a button change (0 = never)")
    ap.add_argument("--nosmooth", action="store_true",
                    help="--ownpos: clear the calc smoothing flag (0x08) except right after button changes")
    ap.add_argument("--ownsrc", choices=("coils", "wacom", "keep"), default="coils",
                    help="--ownpos: position from our coil formula or Wacom's per-measurement field")
    ap.add_argument("--owncal", default=None, metavar="LUT_JSON",
                    help="--ownpos: v2 position routine (dropout-robust + S-curve table)")
    ap.add_argument("--ownbsw", action="store_true",
                    help="--ownpos: skip the extra cycles while a side button is held")
    ap.add_argument("--ownbtn", action="store_true",
                    help="--ownpos: drop extra-cycle positions while a side button is held")
    ap.add_argument("--ownlvl", type=int, default=2,
                    help="--ownpos: lowest calc level that gets own positions (1 = far hover too)")
    ap.add_argument("--ownstale", type=int, default=255,
                    help="--ownpos: after this many results without a fresh scan, pass Wacom's")
    ap.add_argument("--framelog", action="store_true",
                    help="diagnostic: log calc position + its whole input frame (k 0 only)")
    ap.add_argument("--watch", default=None, metavar="ADDR[,ADDR[,ADDR]]",
                    help="diagnostic: log the code that writes these RAM words (k 0)")
    ap.add_argument("--poslog", action="store_true",
                    help="diagnostic: bursts of 32 results with calc position fields (k 0)")
    ap.add_argument("--calclog", action="store_true",
                    help="diagnostic: every 128th result, coil blocks + calc RAM (k 0)")
    ap.add_argument("--coillog", action="store_true",
                    help="diagnostic: log calc position + its input coil profiles (k 0 only)")
    ap.add_argument("--nobtnsrc", action="store_true",
                    help="side buttons zeroed at the pen data word decode")
    ap.add_argument("--pack", action="store_true",
                    help="two pen reports per 64 B USB packet (above 1000 reports/s)")
    ap.add_argument("--hidmore", action="store_true",
                    help="HID task handles up to 3 calc results per tick")
    ap.add_argument("--nopushx", action="store_true",
                    help="extra-cycle frames never reach the calc (stock calc input); with --frame23 on stock 23")
    ap.add_argument("--btnhold", type=int, default=0, metavar="N",
                    help="with --nopushx: keep a pressed button through up to N zero results (mail copy)")
    ap.add_argument("--perscan", action="store_true",
                    help="--k 0 --upsample: interpolate Wacom's per-scan position (no moving average)")
    ap.add_argument("--frame23", action="store_true",
                    help="push the set-1 pass-a frame when the coil window is unchanged")
    ap.add_argument("--hold", action="store_true")
    ap.add_argument("--drain", action="store_true")
    ap.add_argument("--log", action="store_true",
                    help="with --hold: log every calc result instead of holding (debug)")
    ap.add_argument("--hiddrain", action="store_true")
    ap.add_argument("--mailq", type=int, default=None)
    ap.add_argument("--version", type=lambda s: int(s, 0), default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    P = PROFILES[a.slot]
    base, T = P["base"], P["table"]
    if not 0 <= a.k <= 20 or not 1 <= a.s2 <= 4:
        print("--k must be 0..20, --s2 1..4")
        return 1
    blob = bytearray(open(a.src, "rb").read())

    def rec(s):
        o = T - base + 12 * s
        ev, act = struct.unpack_from("<II", blob, o)
        return ev, act, bytes(blob[o + 8:o + 12])

    R = {s: rec(s) for s in range(NREC)}
    ptr_hits = [i for i in range(0, len(blob) - 3, 4)
                if struct.unpack_from("<I", blob, i)[0] == T]
    checks = [
        ("table pointer in descriptor found once", len(ptr_hits) == 1),
        ("steps 23/24 event = stock frame handler",
         R[23][0] == R[24][0] == P["frame_ev"]),
        ("stock loop order 23-24-25-26-27-28-29-23",
         all(R[s][2][0] == n for s, n in TRIVIAL_ACT_NEXT.items())
         and R[25][2][0] == 26),
        ("step 25 count 3, others 1",
         R[25][2][3] == 3 and all(R[s][2][3] == 1 for s in (23, 24, 26, 27, 28, 29))),
        ("28/29 share one S2 action pair with 39/40",
         R[28][1] == R[39][1] and R[29][1] == R[40][1]),
        ("step 27 action == step 38 action (S2 pass a)", R[27][1] == R[38][1]),
        ("step 26 event == step 35 event (S1 arm)", R[26][0] == R[35][0]),
        ("record 44 is not a scan record",
         not (base <= struct.unpack_from("<I", blob, T - base + 12 * NREC)[0] < base + len(blob))),
        ("image tail 0x100-aligned", len(blob) % 0x100 == 0),
    ]
    ok = True
    for n, g in checks:
        print(f"  {'PASS' if g else 'FAIL'}  {n}")
        ok &= g
    if not ok:
        return 1

    ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)

    def emit(src):
        addr = base + len(blob)
        code, _ = ks.asm(src, addr)
        code = bytes(code)
        # keystone silently shifts call targets after it widens a branch itself:
        # every bl / b.w leaving the snippet must hit an address named in the source
        named = {int(x, 16) for x in re.findall(r"#(0x[0-9a-fA-F]+)", src)}
        for i in md.disasm(code, addr):
            if i.mnemonic in ("bl", "b.w") and i.op_str.startswith("#"):
                t = int(i.op_str[1:], 16)
                if not addr <= t < addr + len(code):
                    assert t in named, f"keystone: {i.mnemonic} at 0x{i.address:08X} -> 0x{t:08X}"
        blob.extend(code + b"\xFF" * ((-len(code)) % 0x10))
        return addr, code

    def bl_target(addr):
        ins = next(md.disasm(bytes(blob[addr - base:addr - base + 4]), addr))
        assert ins.mnemonic == "bl", (hex(addr), ins.mnemonic)
        return int(ins.op_str.lstrip("#"), 16)

    def patch_bl(site, target):
        bl, _ = ks.asm(f"bl #{hex(target)}", site)
        assert len(bl) == 4
        blob[site - base:site - base + 4] = bytes(bl)

    def find1(sig, what):
        hits = ([m.start(1) for m in sig.finditer(blob)] if hasattr(sig, "finditer")
                else [i for i in range(len(blob)) if blob.startswith(sig, i)])
        if len(hits) != 1:
            raise SystemExit(f"{what}: {len(hits)} signature hits - aborting")
        return hits[0]

    USB = {}

    def add_hidwake():
        site, delay = resolve_pace(blob, base, md, H["callers"][0])
        L = list(md.disasm(bytes(blob[H["fn"] - base:H["fn"] - base + 40]), H["fn"]))
        ld = next(i for i in L if i.mnemonic == "ldr" and "pc" in i.op_str)
        mq = struct.unpack_from("<I", blob, ((ld.address + 4) & ~3) + int(ld.op_str.split("#")[1].rstrip("]"), 16) - base)[0]
        get = int(next(i for i in L if i.mnemonic == "bl").op_str.lstrip("#"), 16)
        G = list(md.disasm(bytes(blob[get - base:get - base + 0x90]), get))
        k = next(k for k, i in enumerate(G) if i.mnemonic == "movs" and i.op_str == "r2, r6")
        recv = next(int(i.op_str.lstrip("#"), 16) for i in G[k:k + 4] if i.mnemonic == "bl")
        hw, _ = emit(HIDWAKE.format(usbsend=hex(USB["send"]), recv=hex(recv), delay=hex(delay),
                                    demcr=hex(DEMCR), dwt=hex(DWT_CTRL), st=hex(LAT + 0xC0), mq=hex(mq)))
        patch_bl(site, hw)
        print(f"hidwake: loop osDelay(1) @ 0x{site:08X} -> 0x{hw:08X} (mail queue 0x{mq:08X}, "
              f"queue receive 0x{recv:08X}, osDelay 0x{delay:08X}), state 0x{LAT + 0xC0:08X}")

    def add_pushhook():
        rp = P["ring_push"] - base
        if bytes(blob[rp:rp + 4]) != bytes.fromhex("014630b5"):
            raise SystemExit("ring push prologue is not 'mov r1, r0; push {r4, r5, lr}'")
        src = PUSHKICK if a.calcwake else PUSHTS
        pt, _ = emit(src.format(back=hex(P["ring_push"] + 4), ring=hex(RING_IDX),
                                cyc=hex(DWT_CTRL + 4), ups=hex(UPS), irq=KICK_IRQ))
        b, _ = ks.asm(f"b.w #{hex(pt)}", P["ring_push"])
        assert len(b) == 4
        blob[rp:rp + 4] = bytes(b)
        return pt

    def add_calcwake():
        ls = base + find1(LOOP_SIG, "calc loop")
        # drain: every pending ring frame per pass
        dr, _ = emit(CALC_DRAIN.format(calc=hex(bl_target(ls + 4)), ring=hex(RING_IDX)))
        patch_bl(ls + 4, dr)
        tail = next(md.disasm(bytes(blob[ls + 8 - base:ls + 10 - base]), ls + 8))
        assert tail.mnemonic == "b", tail
        tail = int(tail.op_str.lstrip("#"), 16)
        T = list(md.disasm(bytes(blob[tail - base:tail - base + 20]), tail))
        k = next(k for k, i in enumerate(T) if i.mnemonic == "bl")
        assert T[k - 1].op_str == "r0, #1", ("calc poll is not osDelay(1)", T[k - 1])
        dsite, delay = T[k].address, int(T[k].op_str.lstrip("#"), 16)
        # task entry: push {r4, lr}; sub sp, #0x20; ldr r0, =...; bl X
        o = ls - base
        while blob[o:o + 4] != bytes.fromhex("10b588b0"):
            o -= 2
            assert ls - base - o < 0x200, "calc task entry not found"
        E = list(md.disasm(bytes(blob[o:o + 12]), base + o))
        assert E[3].mnemonic == "bl", E
        isite, iorig = E[3].address, int(E[3].op_str.lstrip("#"), 16)
        # FreeRTOS: create from osMailCreate, receive from osMailGet, send-from-ISR from osMailPut
        mc = base + find1(bytes.fromhex("10b584b004006846002100220023"), "osMailCreate")
        M = list(md.disasm(bytes(blob[mc - base:mc - base + 0x40]), mc))
        j = next(j for j, i in enumerate(M) if i.op_str == "r1, #4" and M[j - 1].op_str == "r2, #0")
        create = next(int(i.op_str.lstrip("#"), 16) for i in M[j:j + 3] if i.mnemonic == "bl")
        HL = list(md.disasm(bytes(blob[H["fn"] - base:H["fn"] - base + 40]), H["fn"]))
        get = int(next(i for i in HL if i.mnemonic == "bl").op_str.lstrip("#"), 16)
        G = list(md.disasm(bytes(blob[get - base:get - base + 0x90]), get))
        k = next(k for k, i in enumerate(G) if i.mnemonic == "movs" and i.op_str == "r2, r6")
        recv = next(int(i.op_str.lstrip("#"), 16) for i in G[k:k + 4] if i.mnemonic == "bl")
        put = bl_target(csig + 16)
        U = list(md.disasm(bytes(blob[put - base:put - base + 0x30]), put))
        k = next(k for k, i in enumerate(U) if i.op_str == "r2, sp")
        send = next(int(i.op_str.lstrip("#"), 16) for i in U[k:k + 4] if i.mnemonic == "bl")
        isr, _ = emit(KICK_ISR.format(send=hex(send), h=hex(KICK_H)))
        vec = 4 * (16 + KICK_IRQ)
        old = struct.unpack_from("<I", blob, vec)[0]
        stub = bytes(blob[(old & ~1) - base:(old & ~1) - base + 4])
        assert stub == b"\xff\xf7\xfe\xbf", f"IRQ {KICK_IRQ} vector is not a default 'b.w .' stub"
        struct.pack_into("<I", blob, vec, isr | 1)
        ini, _ = emit(KICK_INIT.format(orig=hex(iorig), create=hex(create), h=hex(KICK_H),
                                       ipr=hex(0xE000E400 + KICK_IRQ), bit=hex(1 << KICK_IRQ)))
        patch_bl(isite, ini)
        cw, _ = emit(CALCWAIT.format(recv=hex(recv), delay=hex(delay), h=hex(KICK_H)))
        patch_bl(dsite, cw)
        print(f"calcwake: drain @ 0x{dr:08X}; IRQ {KICK_IRQ} vector 0x{old:08X} -> ISR 0x{isr:08X} "
              f"(send 0x{send:08X}); init @ 0x{ini:08X} at entry 0x{isite:08X} (create 0x{create:08X}); "
              f"wait @ 0x{cw:08X} replaces osDelay(1) @ 0x{dsite:08X} (recv 0x{recv:08X}); "
              f"queue handle 0x{KICK_H:08X}")

    def add_latmeter():
        pt = add_pushhook()
        mts, _ = emit(MAILTS_PUT.format(ups=hex(UPS)))
        patch_bl(csig + 12, mts)
        psite = base + find1(POP_SIG, "calc ring pop") + 10
        ph, _ = emit(POP_TS.format(pop=hex(bl_target(psite)), ring=hex(RING_IDX), ups=hex(UPS)))
        patch_bl(psite, ph)
        ct, _ = emit(COPYLAT.format(copy=hex(H["copy"]), demcr=hex(DEMCR), dwt=hex(DWT_CTRL),
                                    ups=hex(UPS), lat=hex(LAT), magic=hex(LAT_MAGIC)))
        patch_bl(H["copy_site"], ct)
        print(f"latmeter: push stamp @ 0x{pt:08X}, mail stamp @ 0x{mts:08X}, pop stamp @ "
              f"0x{ph:08X}, meter @ 0x{ct:08X} (site 0x{H['copy_site']:08X}), state 0x{LAT:08X}")

    def add_upsample(always):
        lag = round(a.upsample * 96000)
        if len(H["callers"]) != 1:
            raise SystemExit(f"HID pen routine: {len(H['callers'])} callers - aborting")
        rp = P["ring_push"] - base
        if bytes(blob[rp:rp + 4]) != bytes.fromhex("014630b5"):
            raise SystemExit("ring push prologue is not 'mov r1, r0; push {r4, r5, lr}'")
        pt, _ = emit(PUSHTS.format(back=hex(P["ring_push"] + 4), ring=hex(RING_IDX),
                                   cyc=hex(DWT_CTRL + 4), ups=hex(UPS)))
        b, _ = ks.asm(f"b.w #{hex(pt)}", P["ring_push"])
        assert len(b) == 4
        blob[rp:rp + 4] = bytes(b)
        ct, _ = emit(COPYTS.format(copy=hex(H["copy"]), ups=hex(UPS), cyc=hex(DWT_CTRL + 4)))
        patch_bl(H["copy_site"], ct)
        src = UPS_HID.replace("{target}", UPS_TARGET)
        if a.perscan:
            old = PS_HIST_OLD.replace("{", "{{").replace("}", "}}")
            assert src.count(old) == 1, src.count(old)
            src = src.replace(old, PS_HIST_NEW.replace("{", "{{").replace("}", "}}"))
        if lag == 0:
            # delay 0 (sample-and-hold): every tick still gets a record (the newest
            # result, or a repeat of it), but X/Y are never moved
            old_lag = "lagok:\n    cmp  r0, #0\n    beq.w out\n    mov  r11, r0\n"
            assert src.count(old_lag) == 1
            src = src.replace(old_lag, "lagok:\n    mov  r11, r0\n")
            assert src.count("\ninterp:\n") == 1
            src = src.replace("\ninterp:\n", "\ninterp:\n    cmp  r11, #0\n    beq  out\n")
        if True:
            # every label branch 32-bit: keystone never has to relax (and mis-resolve)
            src = re.sub(r"^(\s+)(b(?:eq|ne|lo|hi|hs|ls|lt|gt|ge|le|mi|pl)?)\s+([a-z_]\w*)\s*$",
                         r"\1\2.w \3", src, flags=re.M)
            # no button mode: the delay is held at MS all the time
            src = src.replace(UPS_TARGET, "    ldr  r1, lit_lag\n")
            assert UPS_TARGET not in src
        ups_src = src.format(
            fn=hex(H["fn"]), usbstate=hex(H["usbstate"]), builder=hex(H["builder"]),
            ups=hex(UPS), magic=hex(UPS_MAGIC), demcr=hex(DEMCR), dwt=hex(DWT_CTRL),
            cur=hex(H["cur"]), prev=hex(H["prev"]), recptr=hex(H["recptr"]), arr=hex(ARR),
            lag=hex(lag), step=hex(48000), stale=hex(12 * 96000), good=hex(GOOD),
            confirm=hex(16 * 96000 if lag else 0), copy=hex(H["copy"]),
            records=hex(H["records"]))
        uh, uc = emit(ups_src)
        ncode = next(i.address + i.size for i in md.disasm(uc, uh)
                     if i.mnemonic.startswith("pop") and "pc" in i.op_str) - uh
        patch_bl(H["callers"][0], uh)
        for n in ("fn", "copy", "usbstate", "builder", "cur", "prev", "recptr"):
            print(f"  hid {n:9s} 0x{H[n]:08X}")
        print(f"upsample: push stamp @ 0x{pt:08X}, copy stamp @ 0x{ct:08X} (site "
              f"0x{H['copy_site']:08X}), HID wrapper @ 0x{uh:08X} (site "
              f"0x{H['callers'][0]:08X}), lag {a.upsample} ms = {lag} cycles"
            + (" (sample-and-hold, no delay)" if lag == 0 else ""))
        # every branch in the wrapper must land inside it, every call on its target
        # (keystone silently shifts targets when it relaxes a branch)
        calls = []
        for i in md.disasm(uc[:ncode], uh):
            if i.mnemonic == "bl":
                calls.append(int(i.op_str.lstrip("#"), 16))
            elif i.mnemonic.startswith(("b", "cb")) and i.mnemonic not in ("bic", "bics", "bx"):
                t = int(i.op_str.split("#")[-1], 16)
                assert uh <= t < uh + ncode, (hex(i.address), i.mnemonic, i.op_str)
        want = [H["fn"], H["copy"], H["copy"], H["builder"], H["records"], H["copy"],
                H["usbstate"], H["builder"]]
        assert calls == want, [hex(c) for c in calls]
        for addr, n, want in ((pt, 0x20, P["ring_push"] + 4), (ct, 0x40, H["copy"])):
            tail = [i for i in md.disasm(bytes(blob[addr - base:addr - base + n]), addr)
                    if i.mnemonic == "b.w"]
            assert int(tail[0].op_str.lstrip("#"), 16) == want, (hex(addr), tail[0].op_str)

    def add_safemail():
        put, free = bl_target(csig + 16), H["free"]
        ps, pc = emit(PUTSAFE.format(put=hex(put), free=hex(free)))
        got = [int(i.op_str.lstrip("#"), 16) for i in md.disasm(pc, ps) if i.mnemonic == "bl"]
        assert got == [put, free], [hex(g) for g in got]
        safe = {}

        def guard_copy(site):
            t = bl_target(site)
            if t not in safe:
                safe[t], _ = emit(MCPYSAFE.format(memcpy=hex(t)))
            patch_bl(site, safe[t])
        guard_copy(csig + 8)
        patch_bl(csig + 16, ps)
        n_usb = 0
        site = H["callers"][0]
        seq = list(md.disasm(bytes(blob[site - base:site - base + 0x300]), site))
        last_alloc = None
        for i in seq:
            if i.mnemonic != "bl":
                continue
            t = int(i.op_str.lstrip("#"), 16)
            if t == H["alloc"]:
                last_alloc = i.address
            elif t == put and last_alloc is not None:
                copies = [c for c in seq if last_alloc < c.address < i.address and c.mnemonic == "bl"]
                assert len(copies) == 1, [hex(c.address) for c in copies]
                guard_copy(copies[0].address)
                patch_bl(i.address, ps)
                n_usb += 1
                last_alloc = None
        print(f"safemail: put guard @ 0x{ps:08X} (put 0x{put:08X}, free 0x{free:08X}) on the "
              f"calc mail + {n_usb} HID->USB posts, copy guards "
              + ", ".join(f"0x{v:08X}->0x{k:08X}" for k, v in safe.items()))

    def add_pace():
        ps_site, delay = resolve_pace(blob, base, md, H["callers"][0])
        pc_, _ = emit(PACE.format(ups=hex(UPS), cyc=hex(DWT_CTRL + 4), period=hex(96000),
                                  resync=hex(4 * 96000), delay=hex(delay), demcr=hex(DEMCR)))
        patch_bl(ps_site, pc_)
        print(f"pace: HID loop osDelay(1) @ 0x{ps_site:08X} -> 0x{pc_:08X} (osDelay 0x{delay:08X})")

    def add_usbpush():
        U = resolve_usb(blob, base, md)
        sends = {q[3] for q in U["queues"]}
        if len(U["sites"]) != 4 or len(sends) != 1 or [q[2] for q in U["queues"]] != [27, 9, 9, 192]:
            raise SystemExit(f"usbif sender layout unexpected: {U['queues']}")
        order = U["queues"][1:] + U["queues"][:1]          # rare reports first, pen last
        while len(blob) % 4:
            blob.append(0xFF)
        table = base + len(blob)
        for k, (q, buf, n, _) in enumerate(order):
            if a.pack and n == 27:
                buf = PACKBUF
            blob.extend(struct.pack("<IIII", q, buf, n, UPS + 0xD0 + 4 * k))
        blob.extend(b"\0" * 4 + b"\xFF" * ((-(len(blob) + 4)) % 0x10))
        src = USB_SEND
        if a.pack:
            esc = lambda t: t.replace("{", "{{").replace("}", "}}")
            unesc = lambda t: re.sub(r"\{\{(get|memcpy|free|send)\}\}", r"{\1}", t)
            tail = unesc(esc(USB_SEND_TAIL))
            assert src.count(tail) == 1
            src = src.replace(tail, unesc(esc(USB_PACK)))
            src = src.replace("lit_table: .word {table}\n",
                              f"lit_table: .word {{table}}\nlit_pbuf:  .word {hex(PACKBUF)}\n")
            assert "lit_pbuf" in src.split("lit_table")[1]
        us, _ = emit(src.format(ready=hex(U["ready"]), get=hex(U["get"]), memcpy=hex(U["memcpy"]),
                                free=hex(U["free"]), send=hex(sends.pop()), table=hex(table)))
        if a.usbpush or a.upsample is not None or a.hidwake:
            dw, _ = emit(USB_DATAIN.format(datain=hex(U["datain"]), usbsend=hex(us), cnt=hex(UPS + 0xE0)))
            ts, _ = emit(USB_TASKSEND.format(usbsend=hex(us), pdev=hex(U["pdev"]),
                                             stock=hex(U["datain"] | 1), wrap=hex(dw | 1)))
        else:
            dw = U["datain"]
            ts, _ = emit(USB_TASKSEND_TICK.format(usbsend=hex(us)))
        for site in U["sites"]:
            patch_bl(site, ts)
        USB["send"] = us
        print(f"usbpush: sender @ 0x{us:08X} (queues " + ", ".join(
            f"0x{q:08X}/{n}" for q, _, n, _ in order) + f"), DataIn wrapper @ 0x{dw:08X} "
            f"(stock 0x{U['datain']:08X}, installed at runtime via pdev 0x{U['pdev']:08X}), "
            f"task hook @ 0x{ts:08X} on {len(U['sites'])} ready checks")

    def add_hidmore():
        if a.pace or a.hidwake:
            raise SystemExit("--hidmore replaces the loop delay; not with --pace/--hidwake")
        site, delay = resolve_pace(blob, base, md, H["callers"][0])
        L = list(md.disasm(bytes(blob[H["fn"] - base:H["fn"] - base + 40]), H["fn"]))
        ld = [i for i in L if i.mnemonic == "ldr" and "pc" in i.op_str][:2]
        lv = [struct.unpack_from("<I", blob, ((i.address + 4) & ~3)
                                 + int(i.op_str.split("#")[1].rstrip("]"), 16) - base)[0] for i in ld]
        assert lv[0] == lv[1], [hex(x) for x in lv]
        hm, _ = emit(HIDMORE.format(mq=hex(lv[0]), st=hex(FAST), delay=hex(delay)))
        patch_bl(site, hm)
        print(f"hidmore: loop osDelay(1) @ 0x{site:08X} -> 0x{hm:08X} (calc mail var 0x{lv[0]:08X})")

    def add_frame23():
        cmp_src, s_ = "", 0x10
        for lo, hi in F23_RANGES:
            for i in range(lo, hi):
                cmp_src += F23_BYTE.format(i=i, s=s_)
                s_ += 1
        f23, fc = emit(FRAME23.format(
            orig=hex(P["frame_ev"] & ~1), memcpy=hex(P["memcpy"]), push=hex(P["ring_push"]),
            stage=hex(F23_STAGE), extra=hex(F23_EXTRA), pblock=hex(P["pblock"]), img=hex(F23_IMG),
            st=hex(FAST + 0x60), compare=cmp_src, tag=F23_TAG if a.hold else "",
            taglits=F23_TAGLITS.format(ring=hex(RING_IDX), arr=hex(ARR)) if a.hold else ""))
        calls = [int(i.op_str.lstrip("#"), 16) for i in md.disasm(fc, f23) if i.mnemonic == "bl"]
        assert calls == [P["frame_ev"] & ~1, P["memcpy"], P["memcpy"], P["ring_push"]], \
            [hex(c) for c in calls]
        return f23

    def add_ownpos():
        if not a.safemail or a.coillog or a.framelog or a.upsample is not None or a.tearhold:
            raise SystemExit("--ownpos needs --safemail and no other calc mail hook")
        cmp = "".join(OWN_CMP.format(src=0x11 + 10 * i, dst=2 * i) for i in range(10)) +                 "".join(OWN_CMP.format(src=0xBB + 10 * i, dst=0x14 + 2 * i) for i in range(10))
        if a.ownbtn and not (a.hold and a.rawhold and a.k > 0):
            raise SystemExit("--ownbtn needs --hold --rawhold and extra cycles")
        src = OWNPOS_HOOK
        if a.owncal or a.ownbsw:
            head = "    ldr  r7, lit_own\n"
            assert src.count(head) == 1
            src = src.replace(head, head + ("{bsw}" if a.ownbsw else ""))
        if a.owncal:
            import json as _json
            lut = _json.load(open(a.owncal))
            ctr = [-0.45 + 0.1 * i for i in range(10)]

            def knots(v, scale):
                out = []
                for j in range(11):
                    x = -0.5 + 0.1 * j
                    if x <= ctr[0]:
                        y = v[0]
                    elif x >= ctr[-1]:
                        y = v[-1]
                    else:
                        i = min(int((x - ctr[0]) / 0.1), 8)
                        t = (x - ctr[i]) / 0.1
                        y = v[i] + (v[i + 1] - v[i]) * t
                    out.append(int(round(y * scale)))
                return out
            kx, ky = knots(lut["X"], 1.0), knots(lut["Y"], 0.5)
            while len(blob) % 4:
                blob.append(0xFF)
            tx = base + len(blob)
            blob.extend(struct.pack("<11h", *kx) + bytes(2))
            ty = base + len(blob)
            blob.extend(struct.pack("<11h", *ky) + bytes(2))
            blob.extend(bytes([0xFF]) * ((-len(blob)) % 0x10))
            for axis, (st, lit, kq) in {"x": ("    ldrb r1, [r4, #0x0c]\n    adds r1, #4\n", "lit_lx", 973),
                                       "y": ("    ldrb.w r1, [r4, #0xb6]\n    adds r1, #4\n", "lit_ly", 932)}.items():
                assert src.count(st) == 1, axis
                src = src.replace(st, st + f"    ldr  r2, {lit}\n    movw r3, #{kq}\n")
            c0 = src.index("\ncalc:")
            c1 = src.index("    .align 2\nlit_own:")
            src = src[:c0] + CALC_V2 + src[c1:]
            src = src.replace("lit_own: .word {own}\n", "lit_own: .word {own}\nlit_lx: .word " + hex(tx)
                              + "\nlit_ly: .word " + hex(ty) + "\n")
            print(f"owncal: S-curve knots X {kx}, Y {ky} @ 0x{tx:08X}/0x{ty:08X}")
        if a.btndeb:
            head = "    ldr  r7, lit_own\n"
            assert src.count(head) == 1
            src = src.replace(head, head + OWN_BTNDEB.replace("{deb}", str(a.btndeb)))
            print(f"btndeb: side buttons debounced over {a.btndeb} results")
        if a.nosmooth:
            fsig = bytes.fromhex("0142 01d0 0120 00e0 0020 7047".replace(" ", ""))
            fl = [m.start() for m in re.finditer(re.escape(fsig), bytes(blob))]
            fl = [o - 4 for o in fl if blob[o - 3] == 0x49 and blob[o - 2:o] == bytes.fromhex("0968")]
            assert len(fl) == 1, fl
            fo = fl[0]
            lit = ((base + fo + 4) & ~3) + blob[fo] * 4
            flag = struct.unpack_from("<I", blob, lit - base)[0]
            assert 0x20000000 <= flag < 0x20040000, hex(flag)
            head = "    ldr  r7, lit_own\n"
            assert src.count(head) == 1
            if a.nosmooth_win == 0:
                ns = ("    movw r0, #{flo}\n    movt r0, #{fhi}\n    ldr  r1, [r0]\n"
                      "    bic  r1, r1, #8\n    str  r1, [r0]\n")
            else:
                ns = OWN_NOSMOOTH.replace("    movs r0, #16\n", f"    movs r0, #{a.nosmooth_win}\n")
            src = src.replace(head, head + ns.replace("{flo}", hex(flag & 0xFFFF))
                              .replace("{fhi}", hex(flag >> 16)))
            print(f"nosmooth: flag word 0x{flag:08X} (check fn 0x{base + fo:08X}), bit 0x08 off "
                  + (f"except {a.nosmooth_win} results after a button change" if a.nosmooth_win else "always"))
        if a.pressgate:
            assert src.count("{gate}") == 1 and not a.ownanchor
            pz = OWN_PRESSGATE
            if a.fastpass:
                t = "pz_chk:\n"
                assert pz.count(t) == 1
                pz = pz.replace(t, t + "    ldrb r0, [r7, #0xf9]\n    cmp  r0, #0\n    bne  pz_upd\n")
            src = src.replace("{gate}", pz + "{gate}")
            print("pressgate: 12 fresh results after a button change, drop > 250 + 1.5|v| off the line")
        if a.phasegate:
            fr = "    cmp.w r12, #0\n    beq.w stale\n"
            assert src.count(fr) == 1
            cnt = ("    ldrb r0, [r7, #0xf4]\n    cmp  r0, #255\n    it   lo\n    addlo r0, #1\n"
                   "    strb r0, [r7, #0xf4]\n") if a.pressgate else ""
            ff = OWN_FASTFLAG.replace("{fast}", str(a.fastpass)) if a.fastpass else ""
            pg = OWN_PHASEGATE.replace("{thr}", str(a.phasegate))
            if a.fastpass:
                t = "    cmp  r2, #0xff\n    beq  pg_init\n"
                assert pg.count(t) == 1
                pg = pg.replace(t, t + "    ldrb r0, [r7, #0xf9]\n    cmp  r0, #0\n    bne  pg_init\n")
            src = src.replace(fr, fr + cnt + ff + pg)
            ps = "pass:\n"
            assert src.count(ps) == 1
            src = src.replace(ps, ps + "    movs r0, #0xff\n    strb r0, [r7, #0xca]\n")
            print(f"phasegate: drop fresh results whose X peak phase jumps > {a.phasegate} deg")
        if a.ownsrc == "keep":
            # send the calc's own output unchanged on fresh results
            m = re.search(r"    add\.w r0, r4, #0x11\n.*?    str  r0, \[r7, #0x2c\]\n", src, re.S)
            assert m
            src = src[:m.start()] + "    ldr  r0, [r6, #0x1c]\n    str  r0, [r7, #0x2c]\n" + src[m.end():]
            m = re.search(r"    add\.w r0, r4, #0xbb\n.*?    str  r0, \[r7, #0x30\]\n", src, re.S)
            assert m
            src = src[:m.start()] + "    ldr  r0, [r6, #0x54]\n    str  r0, [r7, #0x30]\n" + src[m.end():]
            print("ownsrc: keep the calc output (fresh results only)")
        if a.ownsrc == "wacom":
            # Wacom's calibrated per-measurement position (calc r8+0xFA4 X, +0x10D8 Y,
            # internal units; updated on every fresh measurement, ~10 ms ahead of its
            # smoothed output r8+0x1C/+0x54 = r8+0xFA0 - 2800 / r8+0x10D4 - 2840)
            xs = ("    add.w r0, r4, #0x11\n    ldrb r1, [r4, #0x0c]\n    adds r1, #4\n"
                  "    bl   calc\n    subw r0, r0, #1190\n")
            ys = ("    add.w r0, r4, #0xbb\n    ldrb.w r1, [r4, #0xb6]\n    adds r1, #4\n"
                  "    bl   calc\n    subw r0, r0, #1180\n")
            assert src.count(xs) == 1 and src.count(ys) == 1 and not a.owncal
            src = src.replace(xs, "    addw r0, r8, #0xfa4\n    ldrh r0, [r0]\n    subw r0, r0, #2800\n")
            src = src.replace(ys, "    addw r0, r8, #0xfa4\n    add.w r0, r0, #0x134\n    ldrh r0, [r0]\n"
                                  "    subw r0, r0, #2840\n")
            print("ownsrc: Wacom per-measurement position (r8+0xFA4 / +0x10D8)")
        if a.ownanchor:
            if a.ownsrc != "wacom":
                raise SystemExit("--ownanchor needs --ownsrc wacom")
            lv = "    blo.w pass\n    mov  r2, r1\n"
            assert src.count(lv) == 1
            src = src.replace(lv, lv + "    bl   anc_upd\n")
            gt = "{gate}"
            assert src.count(gt) == 1
            src = src.replace(gt, "    bl   anc_fix\n{gate}")
            ps = "pass:\n"
            assert src.count(ps) == 1
            src = src.replace(ps, ps + "    movs r0, #0\n    strb r0, [r7, #0x91]\n")
            c0 = src.index("\ncalc:")
            src = src[:c0] + ANC_SUBS + src[c0:]
            # keystone relaxes a short branch that went out of range and then shifts
            # later bl targets: write every label branch 32-bit
            src = re.sub(r"(?m)^(\s+)(b|beq|bne|blt|bgt|ble|bge|blo|bhs|bhi|bls|bmi|bpl)(\s+)([A-Za-z_]\w*)$",
                         lambda m: f"{m.group(1)}{m.group(2)}.w{m.group(3)}{m.group(4)}", src)
            for lit, val in (("lit_own", OWN), ("lit_arr", ARR)):
                src = re.sub(r"(?m)^(\s+)ldr\s+(r\d+), " + lit + "$",
                             lambda m, v=val: f"{m.group(1)}movw {m.group(2)}, #{v & 0xFFFF:#x}\n"
                                              f"{m.group(1)}movt {m.group(2)}, #{v >> 16:#x}", src)
            assert not re.search(r"(?m)^\s+ldr\s+r\d+, lit_(own|arr)$", src)
            print("ownanchor: Wacom-output-anchored correction + press-shake substitution")
        if a.btnkeep:
            if not (a.pressgate and a.phasegate):
                raise SystemExit("--btnkeep is written for --pressgate --phasegate")
            n_d = 0
            for lbl in ("pg_init:", "pg_ok:", "g_stock:"):
                t = "    strb r0, [r7, #0x29]\n    b.w  drop\n" + lbl
                if src.count(t) == 1:
                    src = src.replace(t, "    strb r0, [r7, #0x29]\n    b.w  gdrop\n" + lbl)
                    n_d += 1
            t = "    strb r0, [r7, #0x29]\n    b.w  drop\npz_upd:"
            assert src.count(t) == 1
            src = src.replace(t, "    strb r0, [r7, #0x29]\n    b.w  gdrop\npz_upd:")
            n_d += 1
            # stale results: a button change is sent (last position) like a level change
            st = "    ldrb r0, [r7, #0x28]\n    cmp  r0, r2\n    beq.w drop\n"
            assert src.count(st) == 1
            src = src.replace(st, "    ldrb r0, [r6, #2]\n    and  r0, r0, #7\n    ldrb r1, [r7, #0xe4]\n"
                                  "    cmp  r0, r1\n    bne.w gsend\n" + st)
            ps = "pass:\n"
            assert src.count(ps) == 1
            src = src.replace(ps, OWN_GDROP + ps)
            sd = "\nsend:\n"
            assert src.count(sd) == 1
            src = src.replace(sd, sd + OWN_SENDREC + "send_nr:\n")
            print(f"btnkeep: {n_d} gate drops keep button changes; stale results send button changes")
        if a.pressgate and not a.ownanchor:
            src = re.sub(r"(?m)^(\s+)(b|beq|bne|blt|bgt|ble|bge|blo|bhs|bhi|bls|bmi|bpl)(\s+)([A-Za-z_]\w*)$",
                         lambda m: f"{m.group(1)}{m.group(2)}.w{m.group(3)}{m.group(4)}", src)
            for lit, val in (("lit_own", OWN), ("lit_arr", ARR)):
                src = re.sub(r"(?m)^(\s+)ldr\s+(r\d+), " + lit + "$",
                             lambda m, v=val: m.group(1) + "movw " + m.group(2) + ", #" + hex(v & 0xFFFF) + chr(10)
                             + m.group(1) + "movt " + m.group(2) + ", #" + hex(v >> 16), src)
        oh, oc = emit(src.format(compare=cmp, free=hex(H["free"]), own=hex(OWN), bsw=OWN_BSW,
                                         minlvl=a.ownlvl, stalemax=a.ownstale,
                                         btn=OWN_BTN.replace("{btnmask}", "7") if a.ownbtn else "",
                                         btnlits=f"lit_arr: .word {hex(ARR)}" if (a.ownbtn or a.owngate) else "",
                                         gate=(OWN_GATE.replace("    b.w  drop\ng_stock:", "    b.w  gdrop\ng_stock:")
                                               if a.btnkeep else OWN_GATE) if a.owngate else ""))
        ext = [int(i.op_str.lstrip("#"), 16) for i in md.disasm(oc, oh)
               if i.mnemonic in ("bl", "b.w") and not oh <= int(i.op_str.lstrip("#"), 16) < oh + len(oc)]
        assert ext == [H["free"]], [hex(x) for x in ext]
        assert blob[csig + 12 - base:csig + 16 - base] == bytes.fromhex("28683146")
        patch_bl(csig + 12, oh)
        print(f"ownpos @ 0x{oh:08X} ({len(oc)} B) on calc mail site 0x{csig + 12:08X}, state 0x{OWN:08X}")

    def add_framelog():
        if a.ownpos or a.upsample is not None or a.tearhold or a.coillog or a.mailhold:
            raise SystemExit("--framelog hooks the calc mail site; use it alone")
        cl, cc = emit(FRAMELOG_HOOK.format(memcpy=hex(P["memcpy"]), idx=hex(COILLOG_IDX),
                                           log=hex(COILLOG)))
        calls = [int(i.op_str.lstrip("#"), 16) for i in md.disasm(cc, cl) if i.mnemonic == "bl"]
        assert calls == [P["memcpy"]], [hex(c) for c in calls]
        assert blob[csig + 12 - base:csig + 16 - base] == bytes.fromhex("28683146")
        patch_bl(csig + 12, cl)
        print(f"framelog @ 0x{cl:08X} on calc mail site 0x{csig + 12:08X}, 16 x 0x800 at "
              f"0x{COILLOG:08X}, seq 0x{COILLOG_IDX:08X}")

    def add_calclog():
        cl, cc = emit(CALCLOG_HOOK.format(memcpy=hex(P["memcpy"]), idx=hex(COILLOG_IDX), log=hex(COILLOG),
                                          calc=hex(CALCLOG_BASE), n=hex(CALCLOG_N)))
        calls = [int(i.op_str.lstrip("#"), 16) for i in md.disasm(cc, cl) if i.mnemonic == "bl"]
        assert calls == [P["memcpy"]] * 3, [hex(c) for c in calls]
        assert blob[csig + 12 - base:csig + 16 - base] == bytes.fromhex("28683146")
        patch_bl(csig + 12, cl)
        print(f"calclog @ 0x{cl:08X} on calc mail site 0x{csig + 12:08X}, 4 x 0x1800 at 0x{COILLOG:08X}")

    def add_poslog():
        cp = ""
        for i in range(10):
            cp += f"    ldrh.w r2, [r3, #{0x11 + 10 * i}]\n    strh.w r2, [r4, #{0x1c + 2 * i}]\n"
            cp += f"    ldrh.w r2, [r3, #{0xbb + 10 * i}]\n    strh.w r2, [r4, #{0x30 + 2 * i}]\n"
        cl, cc = emit(POSLOG_HOOK.format(idx=hex(COILLOG_IDX), log=hex(COILLOG), copy=cp))
        assert blob[csig + 12 - base:csig + 16 - base] == bytes.fromhex("28683146")
        patch_bl(csig + 12, cl)
        print(f"poslog @ 0x{cl:08X} ({len(cc)} B) on calc mail site 0x{csig + 12:08X}")

    def add_watch():
        addrs = [int(x, 0) for x in a.watch.split(",")]
        addrs += [addrs[-1]] * (3 - len(addrs))
        dm, dc = emit(WATCH_DM.format(w=hex(WATCH)))
        old = struct.unpack_from("<I", blob, 0x30)[0]
        ins = next(md.disasm(bytes(blob[(old & ~1) - base:(old & ~1) - base + 2]), old & ~1))
        assert ins.mnemonic == "bx" and ins.op_str == "lr", ins
        struct.pack_into("<I", blob, 0x30, dm | 1)
        ws, _ = emit(WATCH_SETUP.format(w=hex(WATCH), magic=hex(WATCH_MAGIC), a0=hex(addrs[0]),
                                        a1=hex(addrs[1]), a2=hex(addrs[2])))
        assert blob[csig + 12 - base:csig + 16 - base] == bytes.fromhex("28683146")
        patch_bl(csig + 12, ws)
        print(f"watch: DebugMon vector 0x{old:08X} -> 0x{dm | 1:08X}, arm hook 0x{ws:08X}, "
              f"addresses {', '.join(hex(x) for x in addrs)}, log 0x{WATCH:08X}")

    def add_coillog():
        if a.ownpos or a.framelog or a.upsample is not None or a.tearhold or a.mailhold:
            raise SystemExit("--coillog hooks the calc mail site; use it alone")
        cl, cc = emit(COILLOG_HOOK.format(memcpy=hex(P["memcpy"]), idx=hex(COILLOG_IDX),
                                          log=hex(COILLOG)))
        calls = [int(i.op_str.lstrip("#"), 16) for i in md.disasm(cc, cl) if i.mnemonic == "bl"]
        assert calls == [P["memcpy"]] * 2, [hex(c) for c in calls]
        assert blob[csig + 12 - base:csig + 16 - base] == bytes.fromhex("28683146")
        patch_bl(csig + 12, cl)
        print(f"coillog @ 0x{cl:08X} on calc mail site 0x{csig + 12:08X}, log 0x{COILLOG:08X}, "
              f"seq 0x{COILLOG_IDX:08X}")

    if a.k == 0:
        # stock tracking loop (v1.65: 3 real frames per ~5 ms, pressure and side
        # buttons read every loop) with the 1 kHz output stage on all the time
        # Without --upsample only real reports are sent: every calc result goes out
        # as soon as the endpoint frees (--usbpush), none are repeated.
        a.safemail = a.safemail or a.upsample is not None or a.usbpush or a.hidwake or a.pace \
            or a.pack or a.hidmore
        csig = base + find1(CALC_SIG, "calc mail put")
        H = resolve_hid(blob, base, md)
        if a.upsample is not None:
            msrc = MAILTS_PUT
            if a.perscan:
                msrc = PERSCAN.replace("{", "{{").replace("}", "}}") + msrc
            mts, _ = emit(msrc.format(ups=hex(UPS)))
            if a.perscan:
                print("perscan: mail copy X/Y = Wacom per-scan position (r8+0xFA4 / +0x10D8)")
            patch_bl(csig + 12, mts)
            psite = base + find1(POP_SIG, "calc ring pop") + 10
            pop = bl_target(psite)
            ph, _ = emit(POP_TS.format(pop=hex(pop), ring=hex(RING_IDX), ups=hex(UPS)))
            patch_bl(psite, ph)
            print(f"mail stamp @ 0x{mts:08X} (site 0x{csig + 12:08X}), pop stamp @ 0x{ph:08X}")
            add_upsample(always=True)
            a.usbpush = a.pace = True
        else:
            if a.latmeter:
                add_latmeter()
            elif a.calcwake:
                add_pushhook()
            if a.calcwake:
                add_calcwake()
        if a.safemail:
            add_safemail()
        if a.tearhold:
            if a.safemail:
                raise SystemExit("--tearhold hooks the same mail put as --safemail")
            put = bl_target(csig + 16)
            th, tc = emit(TEARHOLD.format(put=hex(put), free=hex(H["free"]), demcr=hex(DEMCR),
                                          dwt=hex(DWT_CTRL), st=hex(TEAR)))
            got = [int(i.op_str.lstrip("#"), 16) for i in md.disasm(tc, th) if i.mnemonic in ("bl", "b.w")
                   and i.op_str.startswith("#") and not th <= int(i.op_str.lstrip("#"), 16) < th + len(tc)]
            assert got == [H["free"], put], [hex(g) for g in got]
            patch_bl(csig + 16, th)
            print(f"tearhold @ 0x{th:08X} on calc mail put 0x{csig + 16:08X} (put 0x{put:08X}, "
                  f"free 0x{H['free']:08X}), state 0x{TEAR:08X}")
        if a.ownpos:
            add_ownpos()
        if a.framelog:
            add_framelog()
        if a.coillog:
            add_coillog()
        if a.calclog:
            add_calclog()
        if a.poslog:
            add_poslog()
        if a.watch:
            add_watch()
        if a.nomedian:
            # "bl median" after "ldrh r1,[r7,#4]; add.w r0,r6,#0x14" (pressure) and
            # "ldrh r1,[r7,#0xa]; add.w r0,r6,#0x18" (second channel) -> mov r0, r1; nop
            for sig, what in ((RAW_SIG, "pressure median call"),
                              (bytes.fromhex("798906f11800"), "second median call")):
                site = base + find1(sig, what) + 6
                ins = next(md.disasm(bytes(blob[site - base:site - base + 4]), site))
                assert ins.mnemonic == "bl", ins
                blob[site - base:site - base + 4] = bytes.fromhex("084600bf")
                print(f"nomedian: {what} @ 0x{site:08X} ({ins.op_str}) -> mov r0, r1")
        if a.usbpush or a.hidwake or a.pack:
            add_usbpush()
        if a.hidmore:
            add_hidmore()
        if a.frame23:
            # stock table in place: step 23's event (the frame handler) -> gated push
            f23 = add_frame23()
            o23 = T - base + 12 * 23
            assert struct.unpack_from("<I", blob, o23)[0] == P["frame_ev"]
            struct.pack_into("<I", blob, o23, f23 | 1)
            print(f"frame23: stock step 23 event -> 0x{f23 | 1:08X}")
        if a.hidwake:
            if a.pace:
                raise SystemExit("--hidwake replaces --pace")
            add_hidwake()
        elif a.pace:
            add_pace()
        if a.version is not None:
            vid = blob.find(struct.pack("<HH", 0x056A, 0x0357))
            struct.pack_into("<H", blob, vid + 4, a.version)
            print(f"bcdDevice -> 0x{a.version:04X}")
        blob.extend(b"\xFF" * ((-len(blob)) % 0x100))
        open(a.out, "wb").write(blob)
        print(f"wrote {a.out}  ({len(blob)} B)\nsha256 {hashlib.sha256(blob).hexdigest()}")
        return 0

    ev28x, ev29x = R[28][0], R[29][0]
    ev_f0 = None
    if a.upsample is not None and not (a.hold and a.rawhold and a.mailhold):
        raise SystemExit("--upsample needs --hold --rawhold --mailhold")
    stamp_call = ""
    a.safemail |= a.upsample is not None
    if (a.usbpush or a.pace or a.pack or a.hidmore) and not a.safemail:
        raise SystemExit("--usbpush/--pace/--pack/--hidmore need --safemail")
    csig = base + find1(CALC_SIG, "calc mail put") if a.safemail else None
    H = resolve_hid(blob, base, md) if a.safemail else None     # before any patch
    if a.upsample is not None:
        mts, _ = emit(MAILTS.format(ups=hex(UPS)))
        stamp_call = f"push {{lr}}\n    bl #{hex(mts)}\n    pop {{lr}}"
        print(f"mail stamp @ 0x{mts:08X}")
    if a.hold:
        site = base + find1(CALC_SIG, "calc mail put") + 12
        ev_f, c1 = emit(EV_F_TAG.format(arm_s1=hex(R[26][0] & ~1), frame=hex(P["frame_ev"] & ~1),
                                        ring=hex(RING_IDX), arr=hex(ARR), val=1))
        ev_f0, _ = emit(EV_F_TAG.format(arm_s1=hex(R[26][0] & ~1), frame=hex(P["frame_ev"] & ~1),
                                        ring=hex(RING_IDX), arr=hex(ARR), val=0))
        unf_call = ""
        if a.unfreeze:
            unf, _ = emit(UNFREEZE.format(unf=hex(UNF)))
            print(f"unfreeze @ 0x{unf:08X}")
            # the hold hook is entered by bl from the calc, so lr must survive
            unf_call = "push {lr}\n    bl #" + hex(unf) + "\n    pop {lr}"
        if a.rawhold and a.mailhold:
            msite = base + find1(CALC_SIG, "calc mail put") + 12
            mh, _ = emit((CALC_HOLD6 if a.pressfix else CALC_HOLD5).format(
                                           arr=hex(ARR), raw=hex(RAWSAVE), stamp_call=stamp_call,
                                           nullguard="cmp r6, #0\n    beq done" if a.safemail else ""))
            patch_bl(msite, mh)
            print(f"mail hold (v5) @ 0x{mh:08X}, site 0x{msite:08X}")
        if a.rawhold and a.log and a.rawlog:
            # diagnostic: log the per-pen input struct (raw site) and the output
            # (mail site) per frame, tags = slot in loop, no holds at all
            rsite = base + find1(RAW_SIG, "raw pressure filter call") + 2
            rh, _ = emit("\n".join([
                "    " + RAWLOG_CODE,
                "    add.w r0, r6, #0x14",
                "    bx lr",
                "    .align 2",
                "lit_arr: .word " + hex(ARR),
                "lit_log: .word " + hex(RAWLOG)]))
            patch_bl(rsite, rh)
            print(f"diag raw log @ 0x{rh:08X}, site 0x{rsite:08X}")
            hook, hc = emit(CALC_LOG.format(arr=hex(ARR), log=hex(LOG)))
        elif a.rawhold:
            site = base + find1(RAW_SIG, "raw pressure filter call") + 2
            raw_src = RAW_HOOK_SIMPLE if a.rawsimple else RAW_HOOK2 if a.pressfix else RAW_HOOK
            if not a.btnswitch and raw_src is RAW_HOOK:
                # ARR+19 is only maintained by --btnswitch; elsewhere it is a stale
                # no-init byte and must not switch the hold off
                bypass = "    ldrb r3, [r2, #19]\n    cbnz r3, done\n"
                assert raw_src.count(bypass) == 1
                raw_src = raw_src.replace(bypass, "")
            hook, hc = emit(raw_src.format(
                arr=hex(ARR), raw=hex(RAWSAVE),
                rawlog=RAWLOG_CODE if a.rawlog else "",
                rawlog_lits=f"lit_log: .word {hex(RAWLOG)}" if a.rawlog else ""))
        else:
            hook, hc = emit(CALC_LOG.format(arr=hex(ARR), log=hex(LOG)) if a.log
                            else CALC_HOLD.format(arr=hex(ARR), unfreeze_call=unf_call))
        psite = base + find1(POP_SIG, "calc ring pop") + 10
        pop = bl_target(psite)
        pop_src = POP_TAG
        if a.rawsimple:
            # v2.10/v2.11 pop hook: slot tags are not cleared after reading
            pop_src = pop_src.replace("    movs r3, #0\n    strb r3, [r1, r2]\n", "", 1)
        ph, _ = emit(pop_src.format(
            pop=hex(pop), ring=hex(RING_IDX), arr=hex(ARR),
            stamp=POP_STAMP if a.upsample is not None else "",
            stamp_lits=f"lit_ups:  .word {hex(UPS)}" if a.upsample is not None else ""))
        patch_bl(psite, ph)
        print(f"pop tag hook @ 0x{ph:08X} (pop 0x{pop:08X}), site 0x{psite:08X}")
        patch_bl(site, hook)
        print(f"calc hold hook @ 0x{hook:08X} (on the mail copy), site 0x{site:08X}")
        for i in md.disasm(hc, hook):
            print(f"    {i.address:08X}  {i.mnemonic:6s} {i.op_str}")
        tags = {}
        # the calc's pressure trails its input by 3 frames: the pressure measured
        # in the stock loop comes out on the 3 frames after it (log, v1.9A/v1.9B),
        # so those are "fresh" and every other frame is "stale"
        for step, val in ((28, 1), (29, 1), (-28, 1), (-29, 1), (128, 0), (129, 0)):
            tags[step], _ = emit(TAG_JMP.format(val=val, target=hex(R[abs(step) % 100][0] & ~1),
                                                ring=hex(RING_IDX), arr=hex(ARR)))
        uniq = {}

        def tagged(kind, val):
            key = (kind, val)
            if key not in uniq:
                if kind == "f":
                    uniq[key], _ = emit(EV_F_TAG.format(arm_s1=hex(R[26][0] & ~1),
                                                        frame=hex(P["frame_ev"] & ~1),
                                                        ring=hex(RING_IDX), arr=hex(ARR), val=val))
                else:
                    uniq[key], _ = emit(TAG_JMP.format(val=val, target=hex(R[int(kind)][0] & ~1),
                                                       ring=hex(RING_IDX), arr=hex(ARR)))
            return uniq[key] | 1
        ev28x, ev29x = tags[-28] | 1, tags[-29] | 1
    else:
        ev_f, c1 = emit(EV_F.format(arm_s1=hex(R[26][0] & ~1), frame=hex(P["frame_ev"] & ~1)))
    act_s2, c2 = emit(ACT_S2.format(arm_s2=hex(R[26][1] & ~1), s2a=hex(R[27][1] & ~1)))
    ev_fnp = None
    if a.nopushx:
        if a.hold or a.s2only or a.interleave or a.s1only or a.burst26 or a.s2short or a.ownpos:
            raise SystemExit("--nopushx is for the plain k-cycle schedule")
        ev_fnp, cn = emit(EV_FNP.format(arm_s1=hex(R[26][0] & ~1), frame=hex(P["frame_ev"] & ~1),
                                        npx=hex(NPX)))
        calls = [int(i.op_str.lstrip("#"), 16) for i in md.disasm(cn, ev_fnp) if i.mnemonic == "bl"]
        assert calls == [R[26][0] & ~1, P["frame_ev"] & ~1], [hex(c) for c in calls]
        rp = P["ring_push"]
        assert blob[rp - base:rp - base + 4] == bytes.fromhex("014630b5"), "ring push prologue"
        pt, _ = emit(PUSH_TRAMP_NPX.format(cont=hex(rp + 4)))
        pg, _ = emit(PUSH_GATE_NPX.format(tramp=hex(pt), npx=hex(NPX)))
        bw, _ = ks.asm(f"b.w #{hex(pg)}", rp)
        assert len(bw) == 4
        blob[rp - base:rp - base + 4] = bytes(bw)
        print(f"nopushx: extra S1-done EV_FNP 0x{ev_fnp:08X}, ring push 0x{rp:08X} gate 0x{pg:08X}, "
              f"state 0x{NPX:08X} (+0 flag, +4 extra S1 done, +8 pushes dropped)")
    for name, addr, code in (("EV_F", ev_f, c1), ("ACT_S2", act_s2, c2)):
        print(f"{name} @ 0x{addr:08X}:")
        for i in md.disasm(code, addr):
            print(f"    {i.address:08X}  {i.mnemonic:6s} {i.op_str}")

    if a.drain:
        site = base + find1(LOOP_SIG, "calc loop") + 4
        calc_one = bl_target(site)
        if a.restore:
            rngs = [tuple(int(x, 0) for x in r.split(":")) for r in a.restore.split(",")]
            dr, _ = emit(calc_restore_src(calc_one, RING_IDX, ARR, P["memcpy"], rngs))
            print("restore ranges:", ", ".join(f"0x{x:08X}+0x{n:X}" for x, n in rngs))
        elif a.diffmap:
            dr, _ = emit(CALC_DIFF.format(calc=hex(calc_one), ring=hex(RING_IDX), arr=hex(ARR),
                                          memcpy=hex(P["memcpy"]), calcmem=hex(CALCMEM),
                                          n=hex(CALCN), words=hex(CALCN // 4), clr=hex(0xFF0)))
        else:
            dr, _ = emit(CALC_DRAIN.format(calc=hex(calc_one), ring=hex(RING_IDX)))
        patch_bl(site, dr)
        print(f"calc drain hook @ 0x{dr:08X} (calls 0x{calc_one:08X}), site 0x{site:08X}")

    if a.hiddrain:
        site = base + find1(HID_SIG, "HID mail get")
        get, free = bl_target(site), bl_target(site + 0x1E)
        hd, _ = emit(HID_DRAIN.format(get=hex(get), free=hex(free)))
        patch_bl(site, hd)
        print(f"HID drain hook @ 0x{hd:08X} (get 0x{get:08X}, free 0x{free:08X}), "
              f"site 0x{site:08X}")

    if a.mailq is not None:
        mq = find1(struct.pack("<3I", 5, 0x798, 0), "calc mail queue definition")
        struct.pack_into("<I", blob, mq, a.mailq)
        print(f"calc mail queue @ 0x{base + mq:08X}: depth 5 -> {a.mailq}")

    recs = [list(R[s]) for s in range(NREC)]
    recs[24][0] = ev_f | 1
    if a.hold:
        recs[28][0], recs[29][0] = tags[28] | 1, tags[29] | 1
        if a.rawhold:
            recs[29][0] = tags[129] | 1        # stock 29 frame: raw pressure real
    slot = [3]
    if a.hold and a.log:
        recs[24][0], recs[28][0], recs[29][0] = tagged("f", 1), tagged("28", 2), tagged("29", 3)
    nb = lambda nxt, alt2, alt3=1: bytes([nxt, alt2, alt3, 1])
    if a.s2only:
        # stock 29 [S2 done] now starts another S2 (ACT_S2) instead of S1a; each
        # extra cycle: Y1 [plain S2 handler <- S2a, no push] act eaef1,
        #              Y2 [S2 push, done    <- S2b]          act ACT_S2 / eaf65
        fresh = [3 if a.hold else 0]

        def ev_push():
            f = fresh[0] > 0
            fresh[0] -= 1
            if not a.hold:
                return R[29][0]
            if a.log:
                slot[0] += 1
                return tagged("29", slot[0])
            return tags[129 if f else -29] | 1

        recs[29][1] = act_s2 | 1
        for j in range(a.k):
            last = j == a.k - 1
            i = len(recs)
            recs.append([P["set2_ev"], R[28][1], nb(i + 1, i + 1)])
            recs.append([ev_push(), R[29][1] if last else act_s2 | 1,
                         nb(23 if last else i + 2, R[29][2][1])])
    for j in range(0 if a.s2only else a.k):
        last = j == a.k - 1
        fresh_left = (1 if a.rawhold else 3) if (a.hold and j == 0) else 0

        def ev_for(kind):
            nonlocal fresh_left
            f = fresh_left > 0
            fresh_left -= 1
            if a.hold and a.log:
                slot[0] += 1
                return tagged(kind, slot[0])
            if a.nopushx:
                return (ev_fnp if kind == "f" else P["set2_ev"]) | 1
            if kind == "f":
                return (ev_f0 if f else ev_f) | 1
            if not a.hold:
                return R[28][0] if kind == "28" else R[29][0]
            return tags[(128 if kind == "28" else 129) if f else -int(kind)] | 1

        if a.interleave:
            # records (event consumes the previous action's measurement):
            #   X1 [F partial      <- S1a (eaf65)]   act ACT_S2 (S2 arm + S2a)
            #   X2 [S2 push        <- S2a]           act e9e05 (S1b)
            #   X3 [EV_F, S1 done  <- S1b]           act eaef1 (S2b)
            #   X4 [S2 push, done  <- S2b]           act eaf65 (S1a) -> X1 / stock 23
            cyc = [[P["frame_ev"], act_s2 | 1],
                   [ev_for("28"), R[23][1]],
                   [ev_for("f"), R[28][1]],
                   [ev_for("29"), R[29][1]]]
            i, per = len(recs), len(cyc)
            for n, (ev, act) in enumerate(cyc):
                if n < per - 1:
                    recs.append([ev, act, nb(i + n + 1, i + n + 1)])
                else:
                    recs.append([ev, act, nb(23 if last else i + per, R[29][2][1])])
            continue
        if a.s1only:
            # coordinate-only extra cycle: S1a -> S1b -> S1 done (push) + next S1a
            cyc = [[R[23][0], R[23][1]], [ev_for("f"), R[29][1]]]
            i, per = len(recs), len(cyc)
            for n, (ev, act) in enumerate(cyc):
                if n < per - 1:
                    recs.append([ev, act, nb(i + n + 1, i + n + 1)])
                else:
                    recs.append([ev, act, nb(23 if last else i + per, R[29][2][1])])
            continue
        cyc = [[R[23][0], R[23][1]]]
        if a.burst26:
            cyc += [[ev_for("f"), R[26][1]], [R[27][0], R[27][1]]]
        else:
            cyc += [[ev_for("f"), act_s2 | 1]]
        if a.s2short:
            # only the short S2 pass a (step 27 action, ~0.19 ms) before the next S1:
            # its event (the pass-a handler) programs S1a directly, S2 pass b is skipped
            cyc += [[ev_for("28"), R[29][1]]]
        else:
            for m in range(a.s2):
                cyc += [[ev_for("28"), R[28][1]],
                        [ev_for("29"), R[29][1] if m == a.s2 - 1 else act_s2 | 1]]
        i, per = len(recs), len(cyc)
        for n, (ev, act) in enumerate(cyc):
            if n < per - 1:
                recs.append([ev, act, nb(i + n + 1, i + n + 1)])
            else:
                recs.append([ev, act, nb(23 if last else i + per, R[29][2][1])])
    if a.btnswitch:
        if not (a.hold and a.mailhold):
            raise SystemExit("--btnswitch needs --hold --rawhold --mailhold")
        bs, _ = emit(BTNSW.format(inner=hex(recs[29][0] & ~1), arr=hex(ARR)))
        recs[29][0] = bs | 1
        t = bytearray(recs[29][2])
        t[2] = 23                      # alt3: event returns 3 -> stock step 23
        recs[29][2] = bytes(t)
        print(f"button switch @ 0x{bs:08X} on stock 29 (alt3 -> 23)")
        n_x = 0
        for n in range(NREC, len(recs)):
            if recs[n][1] == R[29][1]:          # extra-cycle end: act eaf65 (S1a)
                bx, _ = emit(BTNSW_X.format(inner=hex(recs[n][0] & ~1), arr=hex(ARR)))
                recs[n][0] = bx | 1
                t = bytearray(recs[n][2])
                t[2] = 23
                recs[n][2] = bytes(t)
                n_x += 1
        print(f"early switch on {n_x} extra-cycle ends")

    if a.upsample is not None:
        add_upsample(always=False)
    if a.safemail:
        add_safemail()
    if a.framelog:
        add_framelog()
    if a.coillog:
        add_coillog()
    if a.ownpos:
        if a.hold and not a.rawhold or a.mailhold:
            raise SystemExit("--ownpos with extra cycles needs the mail site free (--rawhold, no --mailhold)")
        add_ownpos()
    if a.upsample is not None or a.usbpush or a.pack:
        add_usbpush()
    if a.upsample is not None or a.pace:
        add_pace()
    if a.hidmore:
        add_hidmore()
    if a.nopushx:
        sv, sc = emit(S2_SAVE.format(orig=hex(recs[29][0] & ~1), memcpy=hex(P["memcpy"]),
                                     keep=hex(S2KEEP), n=hex(S2KEEP_N)))
        calls = [int(i.op_str.lstrip("#"), 16) for i in md.disasm(sc, sv) if i.mnemonic == "bl"]
        assert calls == [recs[29][0] & ~1, P["memcpy"]], [hex(c) for c in calls]
        recs[29][0] = sv | 1
        rs, rc = emit(S2_REST.format(orig=hex(P["set2_ev"] & ~1), memcpy=hex(P["memcpy"]),
                                     keep=hex(S2KEEP), n=hex(S2KEEP_N)))
        calls = [int(i.op_str.lstrip("#"), 16) for i in md.disasm(rc, rs) if i.mnemonic == "bl"]
        assert calls == [P["set2_ev"] & ~1, P["memcpy"]], [hex(c) for c in calls]
        nrest = 0
        for n in range(NREC, len(recs)):
            # an extra cycle's last S2 event (its action leads back to S1 pass a)
            if recs[n][0] == P["set2_ev"] | 1 and recs[n][1] == R[29][1]:
                recs[n][0] = rs | 1
                nrest += 1
        if a.btnhold:
            csite = base + find1(CALC_SIG, "calc mail put") + 12
            assert blob[csite - base:csite - base + 4] == bytes.fromhex("28683146")
            bh, bc = emit(BTNHOLD_HOOK.format(n=a.btnhold, bh=hex(BTNHOLD)))
            assert not [i for i in md.disasm(bc, bh) if i.mnemonic == "bl"]
            patch_bl(csite, bh)
            print(f"btnhold: {a.btnhold} results @ 0x{bh:08X} on calc mail site 0x{csite:08X}, "
                  f"state 0x{BTNHOLD:08X}")
        print(f"s2keep: stock 29 saves S2 blocks (0x{S2KEEP_N:X} B @ 0x{S2KEEP:08X}) 0x{sv:08X}, "
              f"{nrest} extra 29' restore 0x{rs:08X}")
    if a.frame23:
        f23 = add_frame23()
        n23 = 0
        # --nopushx: extra frames must not reach the calc -> stock step 23 only
        for n in [23] + ([] if a.nopushx else list(range(NREC, len(recs)))):
            if recs[n][0] == P["frame_ev"] and recs[n][1] == R[23][1]:
                recs[n][0] = f23 | 1
                n23 += 1
        print(f"frame23: hook @ 0x{f23:08X} on {n23} set-1 pass-a events (stock 23 + extra cycles)"
              + (", tagged stale" if a.hold else ""))

    if a.ownbsw:
        if not (a.ownpos and a.k > 0):
            raise SystemExit("--ownbsw needs --ownpos and extra cycles")
        bw, _ = emit(STEP29_BSW.format(inner=hex(recs[29][0] & ~1), own=hex(OWN)))
        recs[29][0] = bw | 1
        t = bytearray(recs[29][2])
        t[2] = 23
        recs[29][2] = bytes(t)
        print(f"ownbsw: stock step 29 event -> 0x{bw:08X} (alt3 -> 23 while a side button is held)")

    if a.protect:
        wraps = {}
        for n in range(NREC, len(recs)):
            ev = recs[n][0]
            if ev not in wraps:
                w, _ = emit(PROTECT.format(inner=hex(ev & ~1), cnt=hex(LOSTCNT)))
                wraps[ev] = w | 1
            recs[n][0] = wraps[ev]
        print(f"protect: {len(wraps)} wrappers over {len(recs) - NREC} extra records")

    if a.phasefix:
        if a.hold:
            raise SystemExit("--phasefix is used without --hold")
        ph_save, _ = emit(PH_SAVE.format(inner=hex(recs[29][0] & ~1), memcpy=hex(P["memcpy"]),
                                         save=hex(PHSAVE)))
        recs[29][0] = ph_save | 1
        ph_fix, _ = emit(PH_FIX.format(s2ev=hex(P["set2_ev"] & ~1), memcpy=hex(P["memcpy"]),
                                       push=hex(P["ring_push"]), pblock=hex(P["pblock"]),
                                       save=hex(PHSAVE)))
        n_fix = 0
        for n in range(NREC, len(recs)):
            if recs[n][0] in (R[28][0], R[29][0]):
                recs[n][0] = ph_fix | 1
                n_fix += 1
        print(f"phase save hook @ 0x{ph_save:08X} (stock 29), fix hook @ 0x{ph_fix:08X} "
              f"on {n_fix} extra S2 events")

    if a.fixpa:
        pa_save, _ = emit(PA_SAVE.format(inner=hex(recs[28][0] & ~1), memcpy=hex(P["memcpy"]),
                                         save=hex(SAVEA)))
        recs[28][0] = pa_save | 1
        fixes = {}
        for n in range(NREC, len(recs)):
            ev = recs[n][0]
            if recs[n][1] != R[28][1]:          # S2 pass-a events are the ones whose
                continue                         # action is eaef1 (pass b follows)
            if ev in fixes:
                recs[n][0] = fixes[ev]
                continue
            val = None
            if a.hold and a.log:
                # keep the slot number the replaced tag hook wrote
                val = next(v for (k, v), addr in uniq.items() if (addr | 1) == ev)
            elif a.hold:
                # recover the tag value this event wrote: fresh tag hooks vs stale
                val = 0 if ev == (tags[128] | 1) else 1
            code = PA_FIX.format(
                s2ev=hex(P["set2_ev"] & ~1), memcpy=hex(P["memcpy"]), push=hex(P["ring_push"]),
                pblock=hex(P["pblock"]), save=hex(SAVEA),
                tagcode=PA_TAG.format(val=val) if val is not None else "",
                taglits=PA_TAGLITS.format(ring=hex(RING_IDX), arr=hex(ARR)) if val is not None else "")
            fx, _ = emit(code)
            fixes[ev] = fx | 1
            recs[n][0] = fx | 1
        print(f"pass-a save hook @ 0x{pa_save:08X} (stock 28), fix hooks: "
              f"{', '.join(hex(v) for v in fixes.values())}")

    nx = bytearray(recs[29][2])
    nx[0] = NREC
    recs[29][2] = bytes(nx)
    if len(recs) > 127:
        print("too many records")
        return 1

    while len(blob) % 4:
        blob.append(0xFF)
    new_t = base + len(blob)
    for ev, act, tail in recs:
        blob += struct.pack("<II", ev, act) + tail
    blob += b"\xFF" * ((-len(blob)) % 0x100)
    struct.pack_into("<I", blob, ptr_hits[0], new_t)
    print(f"table 0x{T:08X} -> 0x{new_t:08X}, {len(recs)} records "
          f"(descriptor ptr @ 0x{base + ptr_hits[0]:08X})")
    print(f"step 24 event -> EV_F; step 29 next 23 -> {NREC}; "
          f"{a.k} extra cycle(s) {NREC}..{len(recs) - 1}")

    if a.nobuttons:
        # calc output stage: "ldrb r0, [r7, #4]; bfi r1, r0, #0, #3" copies the side-button
        # bits into the result; load 0 instead, so reports never carry a side button
        site = find1(bytes.fromhex("387960f302017889"), "button copy in calc output")
        blob[site:site + 2] = bytes.fromhex("0020")
        print(f"nobuttons: 0x{base + site:08X} ldrb r0, [r7, #4] -> movs r0, #0")
    if a.nobtnsrc:
        # pen data word decode (13-bit and 11-bit pressure variants): the bits above
        # the pressure are the side buttons ("lsrs r1, r1, #13" / "#11"); make them
        # 0 where they are first extracted, so no later stage (debounce, raw hold,
        # 0x200005CC copy) ever sees a button
        sig = re.compile(rb"\xc1\xf3\x0c\x00(\x49\x0b)\x05\xe0\xc1\xf3\x0a\x02\x50\x0a"
                         rb"\x40\xea\x82\x00(\xc9\x0a)\x68\x83", re.S)
        m = list(sig.finditer(blob))
        if len(m) != 1:
            raise SystemExit(f"pen word decode: {len(m)} signature hits - aborting")
        for g in (1, 2):
            blob[m[0].start(g):m[0].start(g) + 2] = bytes.fromhex("0021")
        print(f"nobtnsrc: 0x{base + m[0].start(1):08X} lsrs r1,#13 and 0x{base + m[0].start(2):08X} "
              f"lsrs r1,#11 -> movs r1, #0")

    if a.version is not None:
        vid = blob.find(struct.pack("<HH", 0x056A, 0x0357))
        struct.pack_into("<H", blob, vid + 4, a.version)
        print(f"bcdDevice -> 0x{a.version:04X}")
    loop = 36000 + a.k * ((22500 if a.burst26 else 20000) + (a.s2 - 1) * 7900)
    frames = 3 + (1 + 2 * a.s2) * a.k
    if a.s2only:
        loop, frames = 36000 + a.k * 7900, 3 + a.k
    print(f"estimate: {frames} frames per {loop * 0.138 / 1000:.1f} ms "
          f"-> ~{frames / (loop * 0.138e-6):.0f} frames/s, pressure at "
          f"~{1 / (loop * 0.138e-6):.0f} Hz")
    open(a.out, "wb").write(blob)
    print(f"wrote {a.out}  ({len(blob)} B)\nsha256 {hashlib.sha256(blob).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
