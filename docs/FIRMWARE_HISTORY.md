# PTH-660 firmware: how the best build was reached, and what didn't work

**Best build: v2.45** — `firmware/images/pth660_v245_BEST_universal.pkg`
Flash: `tools\flash_universal.py firmware\images\pth660_v245_BEST_universal.pkg --arm --both`
~730 reports/s (stock: ~200), stock pressure, side buttons, hover and filters. The pen is still measured ~206
times/s as in stock; the extra reports are Wacom's own smoothed in-between positions (see "Input lag" below).
An even 1000 / 2000 reports/s with real positions only: v3.29 / v3.28 (see below).

Full experiment log: [SENSOR_LINK.md](SENSOR_LINK.md).

## Why stock is ~200 Hz

- USB is not the limit: the tablet already polls at 1 kHz (`bInterval=1`).
- The sensor runs a ~4.85 ms scan loop (step log, v3.00): one position scan S1 (two passes, read at steps
  23 / 24, ~1.7 ms), 16 transmit-only bursts P (steps 24–27, ~2.3 ms: nothing is received there, they power /
  sync the pen), and the data scan S2 (read at steps 28 / 29, ~1 ms), where the pen sends pressure (13 bits)
  and side buttons (3 bits) on the peak coil. So the pen's position is measured once per loop, ~206 times/s.
  Stock sends one report per loop.
- The burst pattern can't be shortened or rearranged: every attempt (skipping or shortening P, shorter
  position bursts, extra position scans between data scans, moving S2's coils) broke pressure, side buttons
  or hover. Listening on neighbour coils during S2 did give a second real position per loop, but a noisier one
  (wavy diagonals), so ~206 real positions/s is what ships. Higher report rates are Wacom's (or our) positions
  between measurements.

## Steps that led to v2.45 (all kept)

| Build | Change | Result |
|---|---|---|
| v1.61 | Also push a frame after set 2 (step 29) | ~409 Hz, clean |
| v1.62 | Push after steps 23, 24, 28, 29 | ~735 Hz, but jumps in some areas |
| v1.65 | Push after 24, 28, 29 only (step 23 was the bad one), calc task 1 ms | ~600 Hz, clean; the user's stable favourite |
| **v2.45** | v1.65 + push the step-23 frame **only when the sensor's coil window is unchanged** since the last loop (`tools/make_frame23.py`) | ~730 Hz; fixes "every 4th report missing" in fast drags |

All these extra frames carry the loop's single position measurement; Wacom's calc turns each into a new
output by moving its 4-result average one step, so the extra reports are smoothed in-between positions.

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

## Input lag (estimated) and report rate

All builds measure the pen the same ~206 times per second (one position scan per ~4.85 ms loop; the pen's
burst pattern can't be changed without breaking pressure / buttons / hover). "Lag" is the estimated average
delay from pen movement to the tablet's USB report, excluding the PC. Built from measured pieces, not an
end-to-end measurement. Every custom build shares Wacom's own processing (~5.6 ms, itself estimated from "output
trails the raw scan by 3-6 results"), so the absolute values may be off by a couple of ms either way; the
differences between builds come from measured parts and are more reliable. Even timing without prediction costs
lag: Wacom's newest position is usually 1.5-4 ms old when a report is built, so an even stream of real positions
has to run that far behind (v3.28 / v3.29), while v2.45 sends each position as soon as it is ready (unevenly).
A higher report rate doesn't lower lag: v3.28's two reports per packet reach the PC together, and the first one is
half a tick older (+0.25 ms).

| Firmware | Reports/s | Lag (est.) | Where the delay comes from |
|---|---|---|---|
| Stock v1.51/v1.52 | ~200 | ~13 ms | 1 report per loop; the 4-report moving average spans ~4 loops (~7 ms), plus Wacom's 2-scan per-scan average (~2.4 ms), waiting for the next report (~2.4 ms), calc -> USB (~1 ms) |
| v1.65 | ~600 | ~8 ms | measured: output trails the raw scan by 3-6 results (~5-10 ms), plus calc -> USB (0.9 ms measured) and USB (~0.5 ms) |
| v2.45 | ~730 | ~7 ms | as v1.65, but 4 results per loop, so the 4-report average spans one loop |
| v2.99 | 1000 (even) | ~9 ms | fixed 6 ms interpolation delay from frame hand-over + Wacom's 2-scan per-scan average (~2.4 ms) + USB (~0.5 ms) |
| v3.29 | 1000 (even) | ~8.5 ms | Wacom's own output at frame hand-over (~5.6 ms: v2.45's 7 ms minus its calc -> USB and USB parts) + ~2.5 ms fixed delay behind it (3.25 ms on the even loop timeline, whose stamps are ~0.75 ms earlier than arrival on average) + USB (~0.5 ms) |
| v3.28 | ~1900-2000 (even, 2 per packet) | ~9 ms | as v3.29; the first report of each packet is half a tick older (+0.25 ms avg) |

## v3.28 (2000 Hz) and v3.29 (1000 Hz): even output, real positions only

`firmware/pth660_v328_2000hz.pkg`, `firmware/pth660_v329_1000hz.pkg`. The pen scan, Wacom's calc and its report
builder run exactly as in v2.45 (same frames, same cadence), so pressure, side buttons, hover and Wacom's filtering
are untouched and straight lines are as clean as v2.45 (ruler test). A new output stage, written in C
(`build/s2x/out.c`, built by `build/make_s2c.py`), replaces only the timing:
- positions come only from the in-range pen records Wacom itself builds (never from its internal state), so the
  output can't contain a position stock wouldn't send (an earlier version read the calc's internal state and
  flicked to the top-left corner at the limit of hover height)
- each frame is time-stamped when it is handed to Wacom's calc, and the stamp follows its result to the report.
  Wacom moves its output one equal step per frame, but a loop's four frames reach it at ~0 / 2.65 / 3.5 / 4.3 ms;
  they are stamped at 0, 1/4, 1/2 and 3/4 of the measured loop period instead, so the cursor moves at a steady
  speed within the loop (with the raw times it pulsed ~3x faster in the bunched part)
- every report lies on the straight line between two positions Wacom has already delivered, 3.25 ms behind on that
  timeline (~2.5 ms behind real arrival). No prediction, so no overshoot; at this delay a report practically never
  has to wait for the next position (0 holds/s measured while drawing)
- extra smoothing only where Wacom's output is rough: the top / left edge strip (coil window 0, where Wacom
  extrapolates past the last coil; up to 8 positions at slow speed, none when fast; edge jitter at slow speed
  59 -> 12 counts p90) and a median-of-3 spike filter in far hover
- the HID and USB task loops are paced to exactly 1 ms (stock loses a tick whenever a pass overruns: ~900/s)
- v3.28 only: two reports per tick (half a tick apart), sent together in one 64-byte USB packet (the PC splits
  them). Measured ~1900-2000 in-range reports/s

Tried and dropped on the way: a short look-ahead (continue the path up to 4-6 ms past the newest position,
v3.21-v3.27) brought the lag under v2.45 (~6.5 ms est.), but overshot on circles and sudden stops (graded by the
firmware itself: mean ~0.15-0.45 mm depending on the motion). Curve-following and adaptive variants were worse in
an offline test on recorded strokes. Earlier fixed delays on raw arrival times: 4 ms (~10 ms, v3.17) and 2.5 ms
(~9 ms, v3.20, ~5 % of reports briefly holding).

Build (reproduces the shipped v3.28 / v3.29 byte for byte; needs arm-none-eabi-gcc): `make_frame23.py` on the
v1.65 bases gives v2.45, then `python build/make_s2c.py --slot a --in slot_a_v245.bin --out slot_a_v328.bin
--version 0x0328 --output --pos output --nos2 --delay-us 3250 --double` (v3.29: `--version 0x0329` and `--pace`
instead of `--double`; same for slot b), then `make_pkg.py`.

### Tried on the way (not shipped)
- Reading the pen more often: the pen-data steps (P) only transmit, nothing is received there; the S2 data scan
  reads the pen's bits on the peak coil with both axes. Listening on the neighbour coils with one axis during S2
  (the other keeps reading the bits) gave a second real position per loop (~410/s) with pressure / buttons /
  hover intact, but those positions are about twice as noisy as the normal scan (they ride on the data bits):
  diagonal wobble 26 vs 15 counts rms. Shelved (`build/s2x/s2x.c`, not hooked with `--nos2`).
- Pushing 8 frames per loop into Wacom's calc (+ USB packing): 1312 reports/s, but Wacom's filters count frames:
  buttons, pressure and random flickers broke.

## v2.99 (1000 Hz, interpolated output)

`firmware/pth660_v299_1000hz.pkg`. The scan is left exactly as stock (v1.65 base): every attempt to measure
more often (extra position cycles, shorter position bursts, other coils in the pen-data bursts) broke
pressure, side buttons or hover, because the pen needs its stock ~4.85 ms burst pattern. So the pen still
delivers ~206 real positions/s. What changes is the output stage:
- The calc's mail copy gets Wacom's **per-scan** position (calc +0xFA4 / +0x10D8, before its 4-result moving
  average). The calc's own state is never written.
- The HID task sends one report on **every 1 ms tick** (fixed-rate schedule), each placed on the straight
  line between the two newest real positions at "now - 6 ms". No prediction: it never goes past the newest
  real point. New points enter the history only when the measured position changes.
- `--safemail` (mail-pool leak fix) and `--usbpush` are included, as in v2.32.

Measured: 997 reports/s in range, gaps 1.00 ms median / 1.01 ms p90. Pressure, buttons and hover are the stock
path. Input lag is about the same as v2.45 (the 6 ms interpolation delay replaces Wacom's smoothing).
Build: `make_cycles.py --k 0 --upsample 6 --perscan` (v2.49, ~1000 Hz with side buttons off, was removed).

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
