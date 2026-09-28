# PTH-660 flash protocol (for a WebHID flasher)

Verified by use: every flash in this project ran this sequence, and
`reference/pth660_flash.py` (a direct port target) flashed the tablet end to end
on 2026-09-28 (erase, program, checksums, commit, reboot: ~16 s).

## Device

| | |
|---|---|
| Device | Wacom Intuos Pro M, PTH-660 |
| USB | VID `0x056A`, PID `0x0357` |
| Interface | the HID collection with **usage page `0xFF0D`** (vendor). All reports below are feature reports on it. |
| Firmware version | `bcdDevice`; WebHID doesn't expose it, so read it from the image in flash (see *Installed firmware version per slot*) |

WebHID: `navigator.hid.requestDevice({filters: [{vendorId: 0x056A, productId: 0x0357, usagePage: 0xFF0D}]})`,
then use the device whose `collections` contain usage page `0xFF0D`.
`sendFeatureReport(id, data)` takes the payload **without** the id byte;
`receiveFeatureReport(id)` returns a DataView that, per the WebHID spec, **includes** the id as byte 0.
Check this on the first call and strip it if present; the offsets below are payload offsets.

## Feature reports used

Payload lengths exclude the report-id byte. Multi-byte values are little-endian.

| Id | Dir | Len | Payload | Meaning |
|---|---|---|---|---|
| `0xD5` | GET | 4 | `[0]` status | `0x00`/`0x20` = ok/idle, `1` = bad sector index, `4` = address outside the writable slot |
| `0xD0` | GET | 8 | `[0]` status 2 | informational (`0x20` idle, `0x30` after commit) |
| `0xD9` | GET | 2560 | 256 × `{u32 addr, u32 size, u16 flags}` | flash sector map; skip entries with addr=size=0; index = position among the rest |
| `0xD3` | SET | 4 | `u16 sector_index, u16 0` | erase one sector |
| `0xD2` | SET | 260 | `u32 addr, 256 bytes` | program one 256-byte block |
| `0xDB` | SET | 6 | `u16 sector_index, u32 sum` | declare a sector's expected checksum (required before commit) |
| `0xD6` | SET | 4 | `u32 1` | commit: rewrite the slot table so the written slot boots next |
| `0xD4` | SET | 4 | `u32 addr` | set the `0xD1` read pointer |
| `0xD1` | GET | 260 | `u32 addr, 256 bytes` | read 256 bytes at the pointer, then pointer += 256 |
| `0x35` | SET | 10 | 10 bytes | reboot handshake (same 10 bytes twice) |

## Memory layout and slots

| Region | Address | Notes |
|---|---|---|
| bootloader | `0x08000000`–`0x08003FFF` | **never touch** |
| slot table | `0x08004000`–`0x080040FF` | written only by the commit (`0xD6`); `[0x08004005]` = boot slot (1 = A, 2 = B) |
| key block | `0x0800C000`–`0x0800C1FF` | never touch |
| config | `0x08010000`–`0x080100FF` | never touch |
| **slot A** | `0x08040000`–`0x0809FFFF` | firmware built on v1.51 |
| **slot B** | `0x080A0000`–`0x080FFFFF` | firmware built on v1.52 |

- The bootloader has **no CRC**; it boots `[0x08004005]` and falls back to the other slot only if that
  slot's table record is flagged unusable.
- The firmware only lets you write the **slot it is not running from**. It checks the write address itself
  (status 4 if outside), but the host must still enforce everything below, because **erase is only
  checked against the sector count**: the sector map covers the bootloader too.
- An image is **linked for one slot** (absolute addresses). A package therefore carries one image per slot,
  and you always flash the image for the *inactive* slot.

## Which slot is running

Read the bootloader's published choice from RAM (read-only):

```
SET 0xD4  u32 0x2003FF00        ; pointer to SRAM
GET 0xD1  -> u32 addr, 256 bytes ; check addr == 0x2003FF00, else retry (up to 5x)
running = data[0x80]            ; 1 = A, 2 = B
```

Only point `0xD4` at SRAM (`0x20000000`–`0x2003FF00`). **Never walk `0xD1` past `0x000FFF00` in flash:
reading past the last flash block wedges the whole device until a full power-down.**

## Installed firmware version per slot

WebHID doesn't expose `bcdDevice`, but every image stores it after its VID/PID: bytes `6A 05 57 03`
then `u16 version` (`0x0245` = v2.45). The block is at the same address in every build of a base:
`0x080917A8` (slot A) and `0x080F19A0` (slot B). Read one 256-byte window and search it:

```
SET 0xD4  u32 0x08091700 (slot A) / 0x080F1900 (slot B)
GET 0xD1  -> u32 addr (must equal the request), 256 bytes; find 6A 05 57 03, version = next u16
```

These addresses are well inside flash. Only read inside the slot windows, never near `0x08100000`.
Checked live: slot B (running) read `0x0249` = the USB bcdDevice, slot A read `0x0248`.

## Reading a slot (backup, read-back verify)

`SET 0xD4 u32 addr` once, then `GET 0xD1` repeatedly: each read returns `u32 addr, 256 bytes` and the
pointer advances by 256 (verified on the tablet: 64 sequential reads, 0.56 ms each). Check every
returned address; re-send `0xD4` on a mismatch. Read only `0x08040000`..`0x080FEFFF` (both slots, stopping
4 KB short of the end of flash). A slot's image = its contents with the erased `0xFF` tail removed,
rounded up to 256 bytes.

## Flashing one slot

`target` = the slot that is **not** running. `lo, hi` = its window. `image` = the package image for `target`.

1. **Validate the image**
   - initial SP `u32 image[0]` is in `0x20000000..0x20040000`
   - reset vector `u32 image[4]` is odd (Thumb) and `(rv & ~1)` is inside `lo..hi`
   - `lo + len - 1 <= hi`
   - sha256 matches the manifest
   - pad with `0xFF` to a multiple of 256
2. **Preconditions:** `GET 0xD5` status is `0x00` or `0x20`; target ≠ running.
3. **Plan:** `GET 0xD9`; take the sectors with `addr < lo + len` and `addr + size > lo`.
   Every one must lie fully inside `lo..hi` and must not overlap any never-touch region. Otherwise abort.
   (slot A ≈ 82 sectors, slot B ≈ 82 sectors, 4 KB each.)
4. **Erase:** for each sector, `SET 0xD3 (index, 0)`, then `GET 0xD5` must be ok.
5. **Program:** for block `i`, `SET 0xD2 (lo + 256·i, image[256·i .. +256])`, ascending.
   Check `GET 0xD5` at least every 64 blocks and after the last one.
6. **Declare sector checksums:** pad the image with `0xFF` to the end of the last planned sector. For each
   sector, `sum` = wrapping u32 sum of its 1024 little-endian words. `SET 0xDB (index, sum)`.
   Then `GET 0xD5` must be ok. **Without this the commit silently refuses.**
6b. **Read-back verify (recommended):** read `lo .. lo+len` back as above and compare with the image;
   on any difference stop here (no commit: the running slot keeps booting).
7. **Commit:** `SET 0xD6 (u32 1)`; wait ~50 ms; `GET 0xD5` reads `0x30` after a good commit.
8. **Reboot** (below). The tablet comes back on the new slot.

Programming speed: ~1300 blocks take ~3.5 s; the whole flash with reboot takes ~16 s.

### Both slots

Flash the inactive slot, reboot, confirm it now runs (running-slot read), then flash the other slot
(which is now inactive) with the package's other image, and reboot again. If the first reboot does not
land on the new slot, stop: do not write the second slot.

## Reboot

```
SET 0x35  52 42 54 00 00 00 00 00 00 00     ; stored
wait ~150 ms
SET 0x35  52 42 54 00 00 00 00 00 00 00     ; matches -> MCU reset
```

Any fixed 10 bytes work; only "same twice" matters. The device disconnects within ~0.1 s and
re-enumerates after ~8–9 s. WebHID: listen for `disconnect`/`connect` on `navigator.hid`, then
reopen. On reconnect the device may need a short delay (~2 s) before it answers feature reports.
The reboot saves the tablet's config through its own routine; it does not touch either slot.

## Package format (`.pkg`)

```
"PTH660PKG\0"           10 bytes magic
u32 LE  n               manifest length
n bytes                 manifest JSON
...                     image bytes; each image at manifest.images[i].offset, size bytes
```

Manifest: `{format: 1, name, device, created, images: [{slot: "a"|"b", base, version: "0x0245",
size, offset, sha256, desc, source}]}`. Verify each image's sha256 before flashing.
`firmware/manifest.json` lists the shipped packages with their own sha256 and a description for the UI.

## Safety rules for the UI

- Flash only the **inactive** slot, only with the image linked for it, and only after all checks pass.
- Never send `0xD3`/`0xD2` for anything outside the target slot window; hard-code the never-touch list.
- Abort on the first bad status. A half-written inactive slot is harmless: the commit never happened, so
  the tablet still boots the other slot. Just flash it again.
- Don't let the tablet disconnect mid-flash (warn about USB hubs and sleep).
- Keep a known-good image in the other slot. If a new build misbehaves, flash the stock or v2.45 package
  again: the flashing path lives in the running firmware, so as long as the tablet enumerates it can be
  reflashed.
- If the device stops answering feature reports entirely, only a full power-down (hold the power
  button until the LEDs go out) clears it.

## Other things a UI may want

- Firmware version: read per slot from flash (above); `0x0245` = v2.45, stock `0x0151`/`0x0152`.
- Pen reports for a live test view: input report `0x10` on the same interface: `[1]` flags (bit0 tip,
  bits1-2 side buttons, `0x20` in range), `[2..4]` X (24-bit), `[5..7]` Y, `[8..9]` pressure (0..8191),
  `[10]`/`[11]` tilt, `[16]` hover distance.
