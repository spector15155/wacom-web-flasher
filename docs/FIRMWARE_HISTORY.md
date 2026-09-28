# PTH-660 firmware: how the best build was reached, and what didn't work

**Best build: v2.45** — `firmware/images/pth660_v245_BEST_universal.pkg`
Flash: `tools\flash_universal.py firmware\images\pth660_v245_BEST_universal.pkg --arm --both`
~730 real reports/s (stock: ~200), stock pressure, side buttons, hover and filters. No simulated reports.

Full experiment log: [SENSOR_LINK.md](SENSOR_LINK.md).

## Why stock is ~200 Hz

- USB is not the limit: the tablet already polls at 1 kHz (`bInterval=1`).
- The sensor runs a ~4.85 ms scan loop: 4 coordinate passes (steps 23, 24, 28, 29) plus a ~2.3 ms
  "pen read" (steps 24–27). The pen sends pressure (13 bits) and side buttons (3 bits) as one 16-bit word
  during that read. Stock sends only one report per loop.
- The pen read can't be shortened or skipped, and the pen needs it about every 5 ms or side buttons
  break. So ~800 real positions/s (4 per loop) is the ceiling for correct pressure and buttons.

## Steps that led to v2.45 (all kept)

| Build | Change | Result |
|---|---|---|
| v1.61 | Also push a frame after set 2 (step 29) | ~409 Hz, clean |
| v1.62 | Push after steps 23, 24, 28, 29 | ~735 Hz, but jumps in some areas |
| v1.65 | Push after 24, 28, 29 only (step 23 was the bad one), calc task 1 ms | ~600 Hz, clean; the user's stable favourite |
| **v2.45** | v1.65 + push the step-23 frame **only when the sensor's coil window is unchanged** since the last loop (`tools/make_frame23.py`) | ~730 Hz; fixes "every 4th report missing" in fast drags |

The v1.62 jumps happened because the step-23 frame combines a new half-scan with the previous loop's
other half. When the pen crosses to other coils, the two halves don't match. v2.45 compares the coil
lists in the sensor register image (0x2001D358) and skips the frame in those loops (~14% of loops).

## Tried, didn't work (reverted)

**More scans / higher rate**
- SPI3 bus faster (/8 → /4): no gain, the sensor free-runs on its own timing.
- Fewer or shorter pressure bursts, skipping the pressure read on alternate loops: pen lost, no pressure, ~100 Hz.
- Shorter coil bursts (−24%): tracking broke (~10 Hz).
- Repeating step 29, forcing the alternate step path 38–41: duplicate positions, jitter, jumps.
- Extra coordinate-only cycles between pressure reads (`make_cycles.py --k N`, up to ~1000 Hz): positions
  fine, but pressure updates only every 13–27 ms (steppy, "flickering") and **side buttons break** (the
  pen needs its read about every 5 ms). Many "hold" variants (raw pressure / buttons / pen state, mail
  holds, button-triggered switch to stock cadence, pen-lost protection) never made buttons fully clean.

**Filling or smoothing the output**
- 1 kHz upsampling with interpolation, and sample-and-hold repeats: rejected as simulated reports that add lag.
- Output-side "unfreeze" (extrapolate frozen positions): overshoot and spikes.

**Latency**
- HID/calc task wake-on-event (`--hidwake`, `--calcwake`): internal latency 0.9 → 0.4 ms, but not
  noticeable and felt unstable.
- `--usbpush` (send each report as soon as the endpoint frees) + `--pace` + `--safemail`: delivered
  ~100% of frames, but felt worse twice (v2.39, v2.46). Reports then arrive in the sensor's lumpy
  rhythm instead of one per 1 ms tick.
- `--nomedian` (bypass the pressure median-of-3): pressure and tip flapped (the filter removes bad
  pen reads). Very jittery.
- The report 0x31/0x33 "filter" settings: not used by the pen path at all.

**Drag gaps**
- `--tearhold` (withhold pen-up for ≤ 80 ms after firm contact, for mid-stroke pen losses): no improvement.
  The real cause of the gaps was the missing step-23 frame, fixed in v2.45.

## Experimental: v2.49 (~1000 Hz, side buttons off)

`firmware/images/pth660_v249_1000hz_nobuttons_universal.pkg`. v2.11 (8 extra position cycles per pen
read, raw pressure hold) plus two changes:
- `--nobuttons`: the calc's output stage copies side-button bits with `ldrb r0, [r7, #4]` (0x080B850C in
  slot B); it now loads 0, so no report carries a button.
- `--safemail`: frees mail blocks when a hand-off fails. v2.11 and v2.48 (buttons off without it) stopped
  reporting within minutes: the calc mail queue fails often at ~1000 reports/s and the stock code
  leaks one block per failure.

Tested: ~890-900 reports/s while tracking, 0 button bits in 20,940 reports (buttons held), pressure works,
pen still alive after the queue failure counter saturated. Holding a side button still makes the pen
drop out briefly (pen timing needs a read every ~5 ms). Build:
`make_cycles.py --k 8 --s2 1 --hold --rawhold --rawsimple --drain --nobuttons --safemail`.

## Side experiment: CTH-480 pen (v2.47, not installed)

The CTH pen is seen by the search scan but fails the Pro Pen 2 lock-on handshake. Its hello word reads
0x28B2 instead of 0x2842, and it sends no valid ID. `tools/make_cthpen.py` corrects the word and gives
it a neutral placeholder ID. With that, it tracks and draws with pressure, but drops out often and
pressure is noisy. Saved as `analysis/pth660_v247.pkg`.

## Useful tools

- `tools/make_frame23.py`: builds v2.45 from the v1.65 images.
- `tools/gap_probe.py`: records reports plus the firmware's queue and pen-lost counters (read-only).
- `tools/pen_test.py`: guided hover/draw/button/pressure scoring.
- `tools/flash_universal.py`: A/B flashing (dry run without `--arm`); `tools/verify_slots.py`.
