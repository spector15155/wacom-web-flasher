# PTH-660 firmware: how the best build was reached, and what didn't work

**Best build: v2.45** — `firmware/images/pth660_v245_BEST_universal.pkg`
Flash: `tools\flash_universal.py firmware\images\pth660_v245_BEST_universal.pkg --arm --both`
~730 reports/s (stock: ~200), stock pressure, side buttons, hover and filters. The pen is still measured ~206
times/s as in stock; the extra reports are Wacom's own smoothed in-between positions (see "Input lag" below).
Two real position measurements per scan loop (~2 x 201/s): v3.62 (~400 even reports/s) and v3.78
(~1500 reports/s, Wacom's smoothing over 7 results per loop). An even 1000 / 2000 reports/s on the stock scan: v3.29 / v3.28 (see below).

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
  (wavy diagonals) until the readings were normalised per burst (v3.62, below): two real positions per
  loop, ~2 x 201/s. Higher report rates are Wacom's (or our) positions between measurements.

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

Builds up to v3.29 measure the pen ~201-206 times per second (one position scan per ~4.9 ms loop; the pen's
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
| v3.62 | ~400 (even, one per real measurement) | ~7-8 ms | Wacom's own processing: a constant 4-result average over S1 / S2 results (~2 x 201/s, so it spans ~1 loop like v2.45's) + calc -> USB (~0.9 ms) + USB (~0.5 ms); no added delay |
| v3.78 | ~1500 (2 per USB packet) | ~7-8 ms | Wacom's own processing, constant 7-result average over ~7 results per loop (~1 loop, like v2.45's 4 of 4) + calc -> USB (~0.9 ms) + USB (~0.5 ms); a result that waits for its USB partner adds up to 1 ms (~5 % of reports) |

## v3.78 (~1500 Hz, experimental): two real measurements, 7 Wacom results per loop

`firmware/pth660_v378_1500hz.pkg`, sources `build/v378/` (own copies of `make_s2c.py`, `s2x/*.c`; the v3.62 files in
`build/` are unchanged so v3.62 still rebuilds byte-identically). Base `build/v378/base/slot_?_f7_base.bin`:
`make_frame2.py --steps 29,28,27,26,25 --pstep --calc1` on the stock images, then `make_frame23.py`: Wacom's calc
gets a frame after steps 23 (only when the coil window is unchanged, as v2.45), 24 (S1), 25-27 (transmit-only steps:
the frame holds a complete S1 and the last complete pen data; v3.08 pushed mid-S2 frames and broke buttons), 28 and
29 (S2). Second measurement per loop as v3.62 (S2 engine, `--layout1`), plus:
- S2 only with a strong signal (peak >= 2000) and a same-coil match within +-10 % (`--s2gate`; offline replay on
  ~3000 logged loops: S2 error rms -30 %, 70 % of loops still injected). Tested and rejected on the same data: gain
  ratio smoothed over loops (1.6-3.6x worse), weighting by strength and pass a / b agreement (no change)
- stock S2 at the top / left edge window (the injection is always rejected there; v3.70-v3.72 tried S2 at the edge:
  never usable and 1-7 ms pressure drops = double clicks while dragging)
- S1 profile entries restored in the work area right after the step-29 push (`--restore`): the next loop's step-23
  frame carries S1 data only, not the older S2 injection (hover jitter -28 %, tip -24 % in a still test)
- Wacom's filter table: constant 7-result window drawing and hover (`--mawin 7 --mahover 7`, ~1 loop)
- calc task takes waiting frames at once (`--calcdrain`; with 7 frames per loop its 3-slot ring overflowed at the
  stock 1 ms poll: ~770 results/s)

Output (`build/v378/s2x/nat.c`, `--minimal --multi --pair --nodrop`): every calc result is reported (Wacom's own
report), up to two per 1 ms HID tick, always two per USB packet (a lone one waits a tick). Fixes found on the way:
- single-report packets were lost when the send timing drifted against the host's 1 ms polling (ST's SendReport drops
  a report while the endpoint is busy): packets now go through a 4-packet FIFO and are handed to the endpoint only
  when it is idle, retried on every 1 ms USB pass (`USBDEV` resolved from the send wrapper)
- after the pen re-entered, Wacom's calc sometimes cleared the X / Y valid bits of level-3 extra-frame results and
  its pen routine skipped them (~350 reports/s until reboot; counters `--natdiag`): `--keepvalid` sets them again
  for steps 23 / 25-28 when the loop's S1 result was valid (the mail block is the put call's r1)
- `--edgesm`: top / left strip (Wacom extrapolates in coil window 0, 3-12x the centre's jitter when slow): per-axis
  average of the last 1-16 reports by speed, full weight below 5000 counts, none above 8000, applied to the copy on
  its way to USB (smoothing Wacom's record buffer disturbed its pen routine)

Measured on one tablet: ~1470-1550 reports/s steady over 90 s with pen exits / re-entries; edge jitter p90 (4 ms
steps) hover slow 85.8 -> 17.3, tip still 24.5 -> 6.3 (v3.69 -> v3.75); still test (rms counts) tip 23.7 vs v2.45's
21.6, hover 43.8 vs 23.8 (v2.45 smooths slow hover over up to 12 results, ~16 ms).

Build (byte-identical; needs arm-none-eabi-gcc), in `build/v378/`: `python make_s2c.py --slot a --in
base/slot_a_f7_base.bin --out slot_a_v378.bin --version 0x0378 --output --minimal --multi --pair --edgesm --natdiag
--keepvalid --nodrop --calcdrain --restore --s2gate 2000 --double --s2norm --layout1 --lean --mawin 7 --mahover 7`
(same for slot b), then `../make_pkg.py`.

## v3.62 (~400 Hz): two real measurements per loop

`firmware/pth660_v362_400hz.pkg`. Base: the v1.61 frame setup (`make_frame2.py --steps 29 --calc1` on the stock
images, `build/base/slot_?_v161_f29.bin`): Wacom's calc gets a frame after step 24 (position scan S1) and after
step 29 (pen-data scan S2), nothing else. The S2 engine (`build/s2x/s2x.c`, `--s2norm --layout1`) adds the second
real position of the loop:
- the step hook rewrites only the receive coil list of the two S2 passes (transmit stays on the peak):
  pass a X P L R P P P / Y P P P L R P, pass b X P P P R L P / Y P R L P P P (P peak, L / R neighbours); only when
  both axes' neighbours are known and the last S1 was centred on the pen
- at each S2 result the peak-axis readings are copied over the moved axis's readings before Wacom decodes the pen
  word (both axes always carry the same bits), so pressure, buttons and hover stay Wacom's own
- after pass b, every neighbour reading is divided by the other axis's peak reading of the SAME burst (cancels the
  pen's signal swings) and corrected by the X/Y gain ratio of bursts 0 / 5
- the normalised left / right go into the frame's profile entries 3 / 5 before the step-29 push, so Wacom's calc
  computes a second position from them; skipped if the S2 peak doesn't match the frame's centre coil (+-20 %), at
  the top / left edge window, or when either axis lacks readings. The S2 positions have no systematic offset
  (~5 counts) and about twice S1's scatter.

Wacom's position filter table is patched to a constant 4-result moving average (`--mawin 4 --mahover 4`, drawing
and hover). With S1 / S2 alternating, any even window keeps the reports evenly spaced along the stroke; 2 results
let S2's scatter through (ruler diagonals 27 rms), and the stock hover window (4..12, +-1 per result with speed)
swings at 400 results/s so the cursor alternated half / 1.5x speed.

Output (`--minimal`, `build/s2x/nat.c`, ~2.4 KB with the S2 engine): exactly Wacom's report for each calc result,
so one per S1 and one per S2 frame, ~400/s even; when S2 is skipped the step-29 report is Wacom's result of that
frame (no hole in fast strokes). Hover signal dips, where Wacom clears the in-range bit for ~10-120 ms while it
keeps tracking the pen, are passed on as in range for up to 150 ms after its last in-range record, except records
jumping > 2500 counts (Wacom's far-hover corner glitch). No re-timing, interpolation, prediction or diagnostics.

Measured on one tablet: ~400 reports/s at any speed, no dropped reports while the tip is down; hover: 2 range
drops in 25 s (v3.51: 68); ruler diagonals ~9-13 counts rms (v2.45 14.6), short-range jitter 5.1. Remaining
gaps: at the very edge of hover range Wacom's own slower search scan (~9 ms) runs instead of the normal loop.

Tried on the way: one report per injected measurement only (v3.45, ~300/s in fast strokes: the pen crossing a
coil between S1 and S2 skips S2), a 2 kHz re-timed variant (v3.46), the v1.65 base with its duplicate step-28 S1
frame (reports alternated 1:2.5 in spacing), instant window narrowing (catch-up jumps in hover).

Build (byte-identical to the shipped package; needs arm-none-eabi-gcc):
`python build/make_s2c.py --slot a --in build/base/slot_a_v161_f29.bin --out slot_a_v362.bin --version 0x0362
--output --minimal --s2norm --layout1 --lean --mawin 4 --mahover 4` (same for slot b), then `make_pkg.py`.

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
  diagonal wobble 26 vs 15 counts rms. Shelved then; normalising each neighbour reading by the other axis's peak reading of the same burst fixed the noise, which is what v3.62 ships.
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
