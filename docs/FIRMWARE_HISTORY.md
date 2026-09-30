# PTH-660 firmware: how each build works

This page describes only the builds in the flasher and how they work. Methods that were tried and not used are
collected separately in [EXPERIMENTS.md](EXPERIMENTS.md); the full lab log is [SENSOR_LINK.md](SENSOR_LINK.md).
v1.65 and v2.99 are older steps kept in the flasher under "Older builds".

| Build | Method | Real measurements/s | Reports/s | Lag (est.) |
|---|---|---|---|---|
| Stock v1.51 / v1.52 | factory | ~200 | ~200 | ~13 ms |
| v1.65 (older, replaced by v2.45) | more Wacom runs | ~200 | ~600 | ~8 ms |
| **v2.45** (recommended) | more Wacom runs | ~200 | ~730 | ~7 ms |
| v2.99 (older, replaced by v3.29) | even output | ~200 | 1000 | ~9 ms |
| v3.29 / v3.28 | even output | ~200 | 1000 / 2000 | ~8.5 / ~9 ms |
| **v3.62** (recommended) | two measurements | ~400 | ~400 | ~7-8 ms |
| v3.85 | two measurements + more Wacom runs | ~400 | ~750 | ~7 ms |
| v3.78 (experimental) | two measurements + more Wacom runs | ~400 | ~1500 | ~7-8 ms |

## The stock scan

- USB is not the limit: the tablet already polls at 1 kHz (`bInterval=1`).
- The sensor runs a ~4.85 ms scan loop: one position scan S1 (two passes, read at steps 23 / 24, ~1.7 ms),
  16 transmit-only bursts P (steps 24-27, ~2.3 ms: nothing is received there, they power / sync the pen) and the
  data scan S2 (read at steps 28 / 29, ~1 ms), where the pen sends pressure (13 bits) and side buttons (3 bits) on
  the peak coil. So the pen's position is measured once per loop, ~206 times/s, and stock sends one report per loop.
- The burst pattern itself stays as it is in every build: the pen needs its ~5 ms rhythm for pressure, buttons and
  hover.

## The three methods

1. **More Wacom runs** (v1.65, v2.45, and part of v3.78): Wacom's position calculation is handed the frame more
   than once per loop. Each run moves its moving average one step towards the newest measurement, so the cursor
   moves in several smaller steps instead of one jump. No new measurements.
2. **Even output** (v2.99, v3.29, v3.28): a new output stage places reports at exact 1 ms (0.5 ms) intervals on the
   path between positions Wacom has already delivered. No prediction.
3. **Two measurements** (v3.62, v3.78): during S2 one axis listens on the pen's neighbour coils while the other keeps
   reading the bits, which gives a second real position per loop.

## v2.45 (recommended): more Wacom runs

`firmware/pth660_v245_best.pkg`, built by `make_frame23.py` on the v1.65 images.

| Build | Frames handed to Wacom's calc per loop | Reports/s |
|---|---|---|
| v1.65 | after steps 24, 28, 29 (calc task polls every 1 ms instead of 3 ms) | ~600 |
| **v2.45** | as v1.65, plus step 23 **only when the sensor's coil window is unchanged** since the last loop | ~730 |

All frames carry the loop's single position measurement; Wacom's calc turns each into a new output by moving its
4-result average one step, so the extra reports are smoothed in-between positions. The step-23 frame combines a new
half-scan with the previous loop's other half; when the pen crosses to other coils the two halves don't match, so
v2.45 compares the coil lists in the sensor register image (0x2001D358) and skips that frame then (~14 % of loops).

## v3.29 (1000 Hz) and v3.28 (2000 Hz): even output

`firmware/pth660_v329_1000hz.pkg`, `firmware/pth660_v328_2000hz.pkg`. The pen scan, Wacom's calc and its report
builder run exactly as in v2.45, so pressure, side buttons, hover and Wacom's filtering are untouched and straight
lines are as clean as v2.45. A new output stage (`build/s2x/out.c`, built by `build/make_s2c.py`) replaces the timing:
- positions come only from the in-range pen records Wacom itself builds, so the output can't contain a position stock
  wouldn't send
- each frame is time-stamped when it is handed to Wacom's calc and the stamp follows its result to the report; a
  loop's four frames are stamped at 0, 1/4, 1/2 and 3/4 of the measured loop period, so the cursor moves at a steady
  speed within the loop
- every report lies on the straight line between two positions Wacom has already delivered, 3.25 ms behind on that
  timeline (~2.5 ms behind real arrival); no prediction, so no overshoot
- extra smoothing only where Wacom's output is rough: the top / left edge strip (coil window 0, where Wacom
  extrapolates; up to 8 positions at slow speed, none when fast; edge jitter at slow speed 59 -> 12 counts p90) and a
  median-of-3 spike filter in far hover
- the HID and USB task loops are paced to exactly 1 ms (stock loses a tick whenever a pass overruns: ~900/s)
- v3.28 only: two reports per tick, sent together in one 64-byte USB packet (the PC splits them): ~1900-2000/s

Build: `make_frame23.py` on the v1.65 bases gives v2.45, then `python build/make_s2c.py --slot a --in
slot_a_v245.bin --out slot_a_v328.bin --version 0x0328 --output --pos output --nos2 --delay-us 3250 --double`
(v3.29: `--version 0x0329` and `--pace` instead of `--double`; same for slot b), then `make_pkg.py`. The shipped
packages were built from an earlier revision of `out.c`.

## v2.99 (1000 Hz, older): even output, first version

`firmware/pth660_v299_1000hz.pkg`. Stock scan (v1.65 base). The calc's mail copy gets Wacom's per-scan position (before
its 4-result moving average); the HID task sends one report on every 1 ms tick, placed on the straight line between
the two newest real positions at "now - 6 ms" (never past the newest point). Measured 997 reports/s in range. v3.29
does the same job with better timing. Build: `make_cycles.py --k 0 --upsample 6 --perscan`.

## v3.62 (~400 Hz, recommended): two measurements

`firmware/pth660_v362_400hz.pkg`. Base: the v1.61 frame setup (`make_frame2.py --steps 29 --calc1` on the stock
images, `build/base/slot_?_v161_f29.bin`): Wacom's calc gets a frame after step 24 (S1) and after step 29 (S2),
nothing else. The S2 engine (`build/s2x/s2x.c`, `--s2norm --layout1`) adds the second real position of the loop:
- the step hook rewrites only the receive coil list of the two S2 passes (transmit stays on the peak):
  pass a X P L R P P P / Y P P P L R P, pass b X P P P R L P / Y P R L P P P (P peak, L / R neighbours); only when
  both axes' neighbours are known and the last S1 was centred on the pen
- at each S2 result the peak-axis readings are copied over the moved axis's readings before Wacom decodes the pen
  word (both axes always carry the same bits), so pressure, buttons and hover stay Wacom's own
- after pass b, every neighbour reading is divided by the other axis's peak reading of the same burst (cancels the
  pen's signal swings) and corrected by the X/Y gain ratio of bursts 0 / 5
- the normalised left / right go into the frame's profile entries 3 / 5 before the step-29 push, so Wacom's calc
  computes a second position from them; skipped if the S2 peak doesn't match the frame's centre coil (+-20 %), at
  the top / left edge window, or when either axis lacks readings

Wacom's position filter table is set to a constant 4-result moving average for drawing and hover (`--mawin 4
--mahover 4`); with S1 / S2 alternating it keeps the reports evenly spaced along the stroke.

Output (`--minimal`, `build/s2x/nat.c`): exactly Wacom's report for each calc result, one per S1 and one per S2
frame, ~400/s; when S2 is skipped the step-29 report is Wacom's result of that frame (no hole in fast strokes). Hover
signal dips (Wacom clears the in-range bit for ~10-120 ms while it keeps tracking the pen) are passed on as in range
for up to 150 ms, except records jumping > 2500 counts (Wacom's far-hover corner glitch).

Measured on one tablet: ~400 reports/s at any speed, no dropped reports while the tip is down; ruler diagonals
~9-13 counts rms (v2.45 14.6).

Build (byte-identical; needs arm-none-eabi-gcc): `python build/make_s2c.py --slot a --in
build/base/slot_a_v161_f29.bin --out slot_a_v362.bin --version 0x0362 --output --minimal --s2norm --layout1 --lean
--mawin 4 --mahover 4` (same for slot b), then `make_pkg.py`.

## v3.85 (~750 Hz): v2.45 + the second measurement

`firmware/pth660_v385_750hz.pkg`, sources `build/v385/`. v2.45 exactly (frames to Wacom's calc after steps 23 when
the coil window is unchanged, 24, 28 and 29; Wacom's own reports through the stock HID path, ~750/s, one per USB poll;
Wacom's stock filter table, so drawing averages 4 results = one loop and hover keeps v2.45's 4-12), plus the S2
second measurement as in v3.62 (`--s2norm --layout1`) with v3.78's quality checks (`--s2gate 2000`: strong signal,
same coil within +-10 %, stock S2 in the top / left edge window). With a 4-result window spanning exactly one loop,
every step compares like with like one loop apart, so the steps stay even whatever mix of S1 / S2 results the loop
has. `--restore-ev`: S1's own profile entries go back into the work area at the next sensor event, before the next
loop's step-23 frame reuses them. No output hook, no logs, no diagnostic counters (`--lean`).

Measured on one tablet: ~730-790 reports/s; still test hover 9.5 / tip 1.4 counts rms (v2.45 23.8 / 21.6); ruler
diagonals 8.9 rms (v2.45 14.6), short-range jitter 3.5; second measurement used in ~130-180 of ~200 loops/s.

Build (byte-identical; needs arm-none-eabi-gcc): `python build/make_frame23.py --slot a --in
build/base/slot_a_v165_600hz.bin --out slot_a_v245.bin --version 0x0245`, then in `build/v385/`: `python make_s2c.py
--slot a --in slot_a_v245.bin --out slot_a_v385.bin --version 0x0385 --s2norm --layout1 --s2gate 2000 --restore-ev
--lean` (same for slot b), then `../make_pkg.py`.

## v3.78 (~1500 Hz, experimental): two measurements + more Wacom runs

`firmware/pth660_v378_1500hz.pkg`, sources `build/v378/`. Base `build/v378/base/slot_?_f7_base.bin`
(`make_frame2.py --steps 29,28,27,26,25 --pstep --calc1` on the stock images, then `make_frame23.py`): Wacom's calc
gets a frame after steps 23 (only when the coil window is unchanged, as v2.45), 24 (S1), 25-27 (transmit-only steps:
the frame holds a complete S1 and the last complete pen data), 28 and 29 (S2). Second measurement as v3.62, plus:
- S2 only with a strong signal (peak >= 2000) and a same-coil match within +-10 % (`--s2gate`; S2 error rms -30 %,
  70 % of loops still get the second position)
- stock S2 in the top / left edge window
- S1 profile entries restored right after the step-29 push (`--restore`): later frames carry S1 data only
- Wacom's filter table: constant 7-result window, drawing and hover (`--mawin 7 --mahover 7`, ~1 loop)
- the calc task takes waiting frames at once (`--calcdrain`), so none are lost from its 3-slot queue

Output (`build/v378/s2x/nat.c`, `--minimal --multi --pair --nodrop`): every calc result is reported (Wacom's own
report), up to two per 1 ms HID tick, always two per USB packet (a lone one waits a tick):
- packets go through a 4-packet queue and are handed to the endpoint only when it is idle (ST's SendReport drops a
  report while the endpoint is busy)
- `--keepvalid`: Wacom's calc sometimes clears the X / Y valid bits of extra-frame results after the pen re-enters;
  they are set again when the loop's S1 result was valid
- `--edgesm`: top / left strip (Wacom extrapolates in coil window 0): per-axis average of the last 1-16 reports by
  speed, full weight below 5000 counts, none above 8000, applied to the copy on its way to USB

Measured on one tablet: ~1470-1550 reports/s steady with pen exits / re-entries; edge jitter p90 (4 ms steps) hover
slow 85.8 -> 17.3, tip still 24.5 -> 6.3; still test (rms counts) tip 23.7 vs v2.45's 21.6, hover 43.8 vs 23.8
(v2.45 smooths slow hover over up to 12 results).

Build (byte-identical; needs arm-none-eabi-gcc), in `build/v378/`: `python make_s2c.py --slot a --in
base/slot_a_f7_base.bin --out slot_a_v378.bin --version 0x0378 --output --minimal --multi --pair --edgesm --natdiag
--keepvalid --nodrop --calcdrain --restore --s2gate 2000 --double --s2norm --layout1 --lean --mawin 7 --mahover 7`
(same for slot b), then `../make_pkg.py`.

## Input lag (estimated)

"Lag" is the estimated average delay from pen movement to the tablet's USB report, excluding the PC, built from
measured pieces rather than an end-to-end measurement. Every build shares Wacom's own processing (~5.6 ms, itself
estimated), so absolute values may be off by a couple of ms; differences between builds are more reliable. Even
timing without prediction costs lag (an even stream of real positions has to run behind the newest one), and a
higher report rate doesn't lower lag by itself.

| Firmware | Reports/s | Lag (est.) | Where the delay comes from |
|---|---|---|---|
| Stock v1.51/v1.52 | ~200 | ~13 ms | 1 report per loop; the 4-report moving average spans ~4 loops (~7 ms), plus Wacom's 2-scan per-scan average (~2.4 ms), waiting for the next report (~2.4 ms), calc -> USB (~1 ms) |
| v1.65 | ~600 | ~8 ms | measured: output trails the raw scan by 3-6 results (~5-10 ms), plus calc -> USB (0.9 ms measured) and USB (~0.5 ms) |
| v2.45 | ~730 | ~7 ms | as v1.65, but 4 results per loop, so the 4-report average spans one loop |
| v2.99 | 1000 (even) | ~9 ms | fixed 6 ms interpolation delay from frame hand-over + Wacom's 2-scan per-scan average (~2.4 ms) + USB (~0.5 ms) |
| v3.29 | 1000 (even) | ~8.5 ms | Wacom's own output at frame hand-over (~5.6 ms) + ~2.5 ms fixed delay behind it + USB (~0.5 ms) |
| v3.28 | ~1900-2000 (even, 2 per packet) | ~9 ms | as v3.29; the first report of each packet is half a tick older (+0.25 ms avg) |
| v3.62 | ~400 (one per real measurement) | ~7-8 ms | Wacom's own processing: constant 4-result average over S1 / S2 results (~1 loop, like v2.45) + calc -> USB (~0.9 ms) + USB (~0.5 ms) |
| v3.78 | ~1500 (2 per USB packet) | ~7-8 ms | Wacom's own processing: constant 7-result average over ~7 results per loop (~1 loop) + calc -> USB (~0.9 ms) + USB (~0.5 ms); a result that waits for its USB partner adds up to 1 ms (~5 % of reports) |

## Useful tools

- `build/unpack_pkg.py`: split a `.pkg` into its slot images; `build/make_pkg.py`: the reverse.
- `build/make_howto.py`: the "How the pen works" pictures in the flasher.
- `tools/make_frame23.py`: builds v2.45 from the v1.65 images.
- `tools/flash_universal.py`: A/B flashing (dry run without `--arm`); `tools/verify_slots.py`.
- `tools/pen_test.py`, `tools/ruler_test.py`, `tools/still_test.py`, `tools/edge_noise.py`: measurements used above.
- `web/coils.html`: the Coil Viewer (live coil signals and pen position, record / replay).
