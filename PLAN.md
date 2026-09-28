# Plan: PTH-660 web flasher (WebHID)

Goal: a web page that flashes the PTH-660 from the browser, with the safety of
`reference/pth660_flash.py` (tested on the tablet) and the protocol in `docs/PROTOCOL.md`.

## Architecture

```
 Browser (Chrome / Edge, desktop)                          Docker
 ┌────────────────────────────────────────────┐           ┌─────────────────────────┐
 │ index.html + app.js (UI)                   │  HTTP     │ nginx (web service)     │
 │ lib/pkg.js   package parse, checks, sums   │ ◄──────── │  /           web/       │
 │ lib/hid.js   WebHID transport + commands   │           │  /firmware/  firmware/  │
 │ lib/flash.js flash state machine (phase 2) │           └─────────────────────────┘
 └──────────────┬─────────────────────────────┘
                │ WebHID feature reports (usage page 0xFF0D)
                ▼
          PTH-660 tablet (USB)
```

- **No backend logic.** The server only serves static files: the app, `firmware/manifest.json` and the
  `.pkg` files. All device access happens in the browser through WebHID. The container never touches USB.
- **Secure context:** WebHID only works on HTTPS or `localhost`/`127.0.0.1`. The compose file binds to
  `127.0.0.1:8660` (local only). Hosting it for other people needs HTTPS (phase 5).
- **Browsers:** Chrome/Edge 89+ on Windows, macOS, Linux. Not Firefox or Safari (no WebHID). Not Android.

## Run and test

Development (`docker-compose.dev.yml`): stock nginx/node images, nothing built, source folders mounted.

```
docker compose -f docker-compose.dev.yml up -d                          # http://127.0.0.1:8660
docker compose -f docker-compose.dev.yml --profile test run --rm test   # tests on the current files
docker compose -f docker-compose.dev.yml restart web                    # only after editing docker/nginx.conf
docker compose -f docker-compose.dev.yml down
```

Edits to `web/`, `firmware/` and `tests/` are live (reload the page; nginx sends `no-store`).

Release (`docker-compose.yml`): app and firmware baked into the image, unit tests run during the build.

```
docker compose up -d --build
docker compose --profile test run --rm test
```
Note: a shell proxy (`http_proxy`) can intercept `localhost`; use `curl --noproxy '*'` for checks.

## Phases

### Phase 1: skeleton, transport, read-only (done, passed on the tablet)

- `web/lib/pkg.js`: `.pkg` parser, sha256 (SubtleCrypto), image checks (SP, reset vector, size),
  sector sum. Unit tests compare against values from the Python reference (`tests/golden.json`).
- `web/lib/hid.js`: connect (`requestDevice` with the 0xFF0D filter, `getDevices` for granted),
  GET/SET feature report helpers, status (0xD5/0xD0), running slot (RAM read through 0xD4/0xD1),
  sector map (0xD9).
- `web/index.html` + `app.js`: connect, show running slot / flash status / sector map, list the manifest,
  download and verify each package in the browser.
- Docker: nginx image, test stage in the build, compose `web` + `test`.

Tablet result (Chrome, Windows): connect, flash status `0x20/0x20`, running slot B, 256-sector map,
installed versions per slot. Findings: Chromium on Windows returns the report id as byte 0 of
`receiveFeatureReport`; declared report lengths are unreliable (`0xDB` shows as 2560), so `hid.js`
strips the id when byte 0 equals it and pads SETs to the declared length.

**Accept (on the tablet):** open `http://127.0.0.1:8660` in Chrome or Edge, click *Connect tablet*,
choose "Wacom Intuos Pro M". The page must show the same running slot and status as
`python reference/pth660_flash.py status`, and a 256-sector map (96 in each slot window, 4 KB each;
a v2.45 image uses 82 of them).
This proves report-id handling and payload lengths on Windows, the main WebHID risk (see Risks).

### Phase 2: flash one slot (done, passed on the tablet)

`web/lib/flash.js`, a port of `flash_slot()`:

1. `plan(tablet, pkg)`: running slot → target = the other slot. Image for the target, `checkImage`,
   status idle, sector map → planned sectors. Every sector inside the target window, none overlapping the
   bootloader / slot table / key / config ranges. Return the plan (sector count, block count) for the UI.
2. `erase`: `0xD3 (u16 index, u16 0)` per sector, status after each.
3. `program`: `0xD2 (u32 addr, 256 bytes)` ascending, status every 64 blocks and at the end.
4. `declare sums`: `0xDB (u16 index, u32 sum)` per sector over the 0xFF-padded image, then status.
5. `commit`: `0xD6 (u32 1)`, wait 50 ms, status (expect `0x30`).
6. Progress events (`phase`, `done`, `total`), cancel only between phases, abort on the first bad status.

UI: a firmware card gets *Flash*. A confirmation dialog shows target slot, versions, sector/block counts,
and "don't unplug". The page holds a Web Lock and `beforeunload` guard while writing. Progress bar + log.
Add a **dry-run mode** (every write logged, not sent) and use it first.

**Accept:** dry run on the tablet matches the Python dry run (`erase 82 sectors, program 1310 blocks`
for v2.45 slot B). Then a real flash of the *same* build the tablet runs, onto the inactive slot, commit
status `0x30`, and the Python `status` shows the flashed slot booting after phase 3's reboot.

### Phase 3: reboot and reconnect (built; reconnect fix pending re-test)

- `reboot()`: `0x35` with 10 fixed bytes, 150 ms, same again. The device disconnects within ~0.1 s.
- Wait for `navigator.hid` `connect` of 056A:0357 (≤ 120 s), then use `getDevices()` (already granted,
  no prompt), wait ~2 s, reopen, read the running slot.
- Tell the user if the running slot is not the committed one.

**Accept:** after a flash the page reconnects by itself and shows the new running slot and version.

Tablet result (Chrome, Windows, 2026-09-28): flashed v2.45 into slot A from the page (running B v2.49),
rebooted, tablet came back on slot A with v2.45, both slots intact (checked with the Python tool).
Found: after the reboot the tablet fires several `connect` events (one per HID interface); two concurrent
`open()` calls fail with InvalidStateError. Fixed: attach is serialized, ignores non-vendor interfaces,
only stores a device once it is open, retries the first status read, and clears the panel on disconnect.

### Phase 4: wizard UI, backup, restore (built; hardware test pending)

Built: guided flow (Choose -> Review -> Run -> Done) with a device card (both slots, running badge),
three actions, live step checklist with timers and a progress bar, unload guard and Web Lock.
- **Back up tablet**: reads both slots (read window 0xD4/0xD1, sequential, never within 4 KB of the end
  of flash), trims the erased tail, checks each image, builds one `.pkg` with versions, downloads it.
- **Restore from file**: `.pkg` (inactive slot, or both) or a single `.bin` (slot detected from the reset
  vector; if it is for the running slot, the other slot's current image is read and re-installed
  verbatim to switch slots first).
- **Install firmware**: from the manifest, one slot (current firmware kept as fallback) or both.
- **Read-back verify** before every commit (`runFlash({verify})`): a mismatch stops before the commit.
- Jobs live in `web/lib/tasks.js` and are tested end to end against the simulated tablet with real
  flash bytes (`tests/tasks.test.mjs`).

### Phase 4 (original notes): both slots, restore, UX

- "Install on both slots": flash inactive → reboot → check → flash the other → reboot. Stop if the first
  reboot didn't land on the new slot.
- "Restore stock" shortcut (stock package, both slots).
- ~~Show the firmware version~~ done in phase 1: read from each slot's image in flash (PROTOCOL.md).
- Optional live pen view (input report 0x10: X/Y/pressure/tilt/buttons, report rate) to check a build
  right after flashing.
- Error texts for the known states: not idle, status 1/4, device gone mid-flash (re-flash is safe, the
  commit never happened), device wedged (full power-down: hold the power button until the LEDs go out).

**Accept:** full hardware checklist below passes on the tablet.

### Phase 5: hosting (optional)

- Same image behind HTTPS: add a Caddy (or Traefik) service in compose with a real domain, or
  `tailscale serve` for private use. Keep `Permissions-Policy: hid=(self)`, no framing.
- Serve firmware with long cache + sha256 in the manifest (already there); the app always verifies the
  package sha256 before flashing.
- Legal/safety text: unofficial firmware, at your own risk, how to restore stock.

## Tests

| Level | What | Where |
|---|---|---|
| Unit | pkg parse, sha256, image checks, sector sums == Python reference, bad packages rejected | `tests/pkg.test.mjs`, runs in the Docker build and `test` service |
| Serving | page, JS modules (JS MIME type), manifest, packages downloadable and sha256-correct | `test` service (`WEB_URL=http://web`) |
| Flash logic (phase 2) | `flash.js` against a **mock HIDDevice** (`tests/mock-device.mjs`: id byte on receive, inflated declared lengths, status 4 outside the window, 0x30 after commit). The write sequence is **byte-identical** to the Python reference's for both target slots (sha256 over all 1473/1475 writes, `tests/golden.json`), dry run sends nothing, bad status aborts before commit, wrong-slot image refused, reboot handshake | `tests/flash.test.mjs` (done, passing) |
| Hardware | checklist below, by hand | tablet |

Hardware checklist (each step, then `reference/pth660_flash.py status` as cross-check):
1. Connect, status, running slot, sector map (phase 1).
2. Dry run of v2.45 → plan identical to Python.
3. Flash v2.45 to the inactive slot (same build as running), reboot, reconnect, verify slot.
4. Both slots with v2.45.
5. Stock package on both slots, then v2.45 again (proves restore works).
6. Unplug during *program* on purpose (inactive slot, no commit yet) → tablet still boots the old slot,
   re-flash succeeds.

## Risks and how each is handled

| Risk | Handling |
|---|---|
| **Feature-report length on Windows.** Windows HID may require the full declared report length; Chromium may or may not pad. | `hid.js` pads every SET payload to the report's length (`LEN`). Phase 1 on the tablet confirms SET 0xD4 + GET 0xD1 round-trip. If Windows rejects the length, pad to the device's max feature length instead (read from `device.collections[].featureReports`). |
| Report id in `receiveFeatureReport` (included as byte 0 or not). | `get()` strips it when the length is payload+1 and byte 0 is the id; tested in phase 1. |
| Wacom driver holding the interface | hidapi worked with the driver installed, so shared access is fine. If Chrome can't open it, close Wacom Center/tablet service and retry. |
| Reading 0xD1 past the end of flash wedges the device | The app only ever points 0xD4 into SRAM (`peekRam` enforces it); never walk 0xD1 through flash. |
| Wrong slot / wrong image | Target is always the non-running slot, image must be linked for it (reset vector check), sectors checked against the window and the never-touch list, package sha256 vs manifest. |
| Interrupted flash | Before the commit the old slot still boots; re-flash. After the commit the new slot is complete. Keep a known-good build in the other slot. |
| Tab/PC sleep or reload during a flash | Web Lock + `beforeunload` warning; flash takes ~5 s of writes (~16 s with reboot). |
| Someone else's tablet model | Filter on 056A:0357 + usage page 0xFF0D; refuse otherwise. |

## Files

```
pth660-webflash/
├── PLAN.md                this plan
├── Dockerfile             test stage (node) + web stage (nginx)
├── docker-compose.yml     release: built image, web (127.0.0.1:8660) + test (profile "test")
├── docker-compose.dev.yml development: stock images, source mounted, no rebuilds
├── docker/nginx.conf      MIME types, /firmware alias, Permissions-Policy
├── web/                   the app (index.html, app.js, style.css, lib/hid.js, lib/pkg.js)
├── firmware/              manifest.json + .pkg files
├── tests/                 pkg.test.mjs, golden.json (from the Python reference)
├── docs/                  PROTOCOL.md, FIRMWARE_HISTORY.md
├── reference/             pth660_flash.py (working reference implementation)
└── build/                 rebuild the firmware images
```
