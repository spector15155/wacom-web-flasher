# PTH-660 firmware: tried and not used

Everything here was built and tested on the tablet and then dropped. None of it is in the flasher. The builds that
ship and how they work are in [FIRMWARE_HISTORY.md](FIRMWARE_HISTORY.md); the full lab log is
[SENSOR_LINK.md](SENSOR_LINK.md).

## Measuring the pen more often by changing the scan

- SPI3 bus faster (/8 -> /4): no gain, the sensor free-runs on its own timing.
- Fewer or shorter pressure bursts, skipping the pressure read on alternate loops: pen lost, no pressure, ~100 Hz.
- Shorter coil bursts (-24 %): tracking broke (~10 Hz).
- Repeating step 29, forcing the alternate step path 38-41: duplicate positions, jitter, jumps.
- Extra coordinate-only cycles between pressure reads (`make_cycles.py --k N`, up to ~1000 Hz): positions fine, but
  pressure updated only every 13-27 ms (steppy, "flickering") and side buttons broke (the pen needs its read about
  every 5 ms). Many "hold" variants (raw pressure / buttons / pen state, mail holds, button-triggered switch to stock
  cadence, pen-lost protection) never made buttons fully clean. Adding a sync burst to each extra cycle broke positions.
- Pushing 8 frames per loop into Wacom's calc, including frames in the middle of S2 (v3.08): 1312 reports/s, but
  buttons, pressure and random flickers broke (those frames held half-written pen data). v3.78 pushes frames only
  after complete steps.

## Two measurements per loop: the way to v3.62 / v3.78

- Raw neighbour readings during S2 (v3.01-v3.12): a second position per loop (~410/s) with pressure, buttons and
  hover intact, but about twice as noisy (the readings ride on the data bits): diagonal wobble 26 vs 15 counts rms.
  Normalising each neighbour reading by the other axis's reading of the same burst fixed it (v3.62).
- Rewriting S2 pass b without copying the readings back (v2.84): pressure / button drops and wrong hover distance
  (Wacom also uses S2 for signal strength).
- One report only per injected measurement (v3.45): ~300/s in fast strokes, because the pen crossing to the next
  coil between S1 and S2 skips S2.
- The v1.65 frame setup as the base: its duplicate step-28 S1 frame made reports alternate 1 : 2.5 in spacing.
- A 2-result moving average: S2's scatter came through (ruler diagonals 27 rms). Letting the stock hover window swing
  (4-12, +-1 per result) at 400 results/s: the cursor alternated half / 1.5x speed (catch-up jumps).
- Narrowing Wacom's averaging window instantly instead of one step per result: catch-up jumps in hover.
- A pass-b layout with neighbours on the edge bursts (v3.49): more S2 samples, but noisier lines.
- S2 processing variants replayed offline on ~3000 logged loops: smoothing the X/Y gain ratio over loops (1.6-3.6x
  worse), weighting samples by strength and requiring pass a / b agreement (no change). The two gates that helped
  (strong signal only, same coil within +-10 %) are in v3.78.
- S2 in the top / left edge window (v3.70-v3.72): the measurement was never usable there, and the rewritten scan
  caused 1-7 ms pressure drops (double clicks while dragging).

## Higher report rates

- 1 kHz upsampling with interpolation and sample-and-hold repeats (early builds): rejected as simulated reports
  that add lag.
- Re-sending each real report 4-5 times to reach 2000/s (v3.63): copies, not new positions.
- A 2 kHz re-timed variant of the two-measurement scan (v3.46).
- v3.78 development: busy-waiting for the USB endpoint in the USB task starved the HID task (~350-400 reports/s);
  smoothing positions in Wacom's own record buffer disturbed its pen routine (~350 reports/s). The shipped v3.78 uses
  a packet queue and smooths a copy instead.

## Hover power boost (v3.80-v3.84)

The sensor program of every scan step holds the transmit burst length (u16 +0x06), the step period (u16 +0x46) and
an automatic gain (+0x0E) that rises as the pen moves away (~141-159 with the tip down, 170-218 in hover). The builds
lengthened only the power steps' transmit bursts (program 0x8B / 0x8C, transmit-only) by up to 25 % in proportion to
that gain, to charge a hovering pen more (the drive voltage itself is fixed by the hardware). Hover measured steadier
in tests, but with the boost active right after the pen came into range (v3.81), a fast entry made the cursor
jitter; holding the boost off for the first ~0.5 s after each entry (v3.84) was the fix under test when the idea was
dropped. Not shipped; sources kept in `analysis/build_v381_hoverboost/` of the main repository.

## Latency

- A short look-ahead (continue the path up to 4-6 ms past the newest position, v3.21-v3.27): lag under v2.45
  (~6.5 ms est.), but it overshot on circles and sudden stops (mean ~0.15-0.45 mm). Curve-following and adaptive
  variants were worse in an offline test.
- Fixed delays on raw arrival times: 4 ms (~10 ms, v3.17) and 2.5 ms (~9 ms, v3.20, ~5 % of reports briefly holding).
- HID / calc task wake-on-event (`--hidwake`, `--calcwake`): internal latency 0.9 -> 0.4 ms, not noticeable, felt
  unstable.
- `--usbpush` (send each report as soon as the endpoint frees) + `--pace` + `--safemail`: delivered ~100 % of frames
  but felt worse twice (v2.39, v2.46); reports then arrive in the sensor's lumpy rhythm.
- `--nomedian` (bypass the pressure median-of-3): pressure and tip flapped. Very jittery.
- The report 0x31 / 0x33 "filter" settings: not used by the pen path at all.

## Output shaping

- Output-side "unfreeze" (extrapolate frozen positions): overshoot and spikes.
- Taking positions from Wacom's internal calc state instead of its pen records (v3.16): flicks to the top-left corner
  at the limit of hover height.
- Stamping frames with their raw arrival times: the cursor pulsed ~3x faster in the bunched part of the loop.

## Drag gaps

- `--tearhold` (withhold pen-up for <= 80 ms after firm contact): no improvement. The real cause was the missing
  step-23 frame, fixed in v2.45.
- v1.62 pushed the step-23 frame in every loop: jumps where the coil window moved; v2.45 skips it in those loops.

## Side experiment: CTH-480 pen (v2.47)

The CTH pen is seen by the search scan but fails the Pro Pen 2 lock-on handshake: its hello word reads 0x28B2
instead of 0x2842, and it sends no valid ID. `tools/make_cthpen.py` corrects the word and gives it a neutral
placeholder ID. With that it tracks and draws with pressure, but drops out often and pressure is noisy. Not
installed; saved as `analysis/pth660_v247.pkg`.
