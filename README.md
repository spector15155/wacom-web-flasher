# PTH-660 web flasher kit

> Unofficial, community-made firmware and tool; not affiliated with Wacom. Flashing is at your own risk:
> please read [DISCLAIMER.md](DISCLAIMER.md) first.

**Develop (no rebuilds, edits are live):** `docker compose -f docker-compose.dev.yml up -d`, open
http://127.0.0.1:8660 in Chrome or Edge, reload after edits. Tests: `docker compose -f docker-compose.dev.yml --profile test run --rm test`.
**Ship:** `docker compose up -d --build` (files baked into the image, tests run in the build). Roadmap: [PLAN.md](PLAN.md).

Everything needed to build a browser (WebHID) firmware flasher for the Wacom Intuos Pro M (PTH-660),
separated from the research repo around it.

```
pth660-webflash/
├── PLAN.md, Dockerfile, docker-compose.yml, docker/   web app plan and container setup
├── web/                      the web app (WebHID), phase 1: connect / status / verify
├── tests/                    node tests vs the Python reference
├── firmware/                 packages to offer in the UI
│   ├── manifest.json         list for the UI: title, summary, lag, recommended flag, sha256, per-slot images
│   ├── pth660_v245_best.pkg                 v2.45, recommended (~730 reports/s, lag ~7 ms est.)
│   ├── pth660_v322_2000hz.pkg               v3.22, ~2000 even reports/s (2 per USB packet), short look-ahead (lag ~6.5 ms est.)
│   ├── pth660_v321_1000hz.pkg               v3.21, 1000 even reports/s, short look-ahead (lag ~6.4 ms est.)
│   ├── pth660_v299_1000hz.pkg               v2.99, 1000 even reports/s interpolated from real scans (lag ~9 ms est.)
│   ├── pth660_v165_600hz.pkg                v1.65, stable fallback (~600 reports/s, lag ~8 ms est.)
│   └── pth660_stock_v151_v152.pkg           Wacom stock firmware, restore (~200 reports/s, lag ~13 ms est.)
├── docs/
│   ├── PROTOCOL.md           the flash protocol, step by step, with WebHID notes and safety rules
│   └── FIRMWARE_HISTORY.md   how v2.45 was reached and what didn't work
├── reference/
│   └── pth660_flash.py       working Python implementation to port (hidapi); status / info / flash / reboot
└── build/                    rebuild v2.45 / v2.99 / v3.21 / v3.22 from v1.65 (make_s2c.py + s2x/*.c need arm-none-eabi-gcc; not needed by the UI)
    ├── make_frame23.py       v1.65 image -> v2.45 image (per slot)
    ├── make_pkg.py           two slot images -> .pkg
    └── base/                 v1.65 slot A / slot B images
```

## Requirements

**To flash (end user):** nothing to install.
- Desktop **Chrome or Edge** (WebHID, version 89+). Firefox and Safari don't support WebHID.
- The tablet on a **USB cable** (not Bluetooth/wireless).
- The page opened from `http://127.0.0.1` / `localhost` or over **HTTPS** (browsers only allow USB access there).
- Windows and macOS need no driver: the browser uses the OS's built-in HID support. The Wacom driver can stay installed.
- **Linux only:** allow Chrome to open the tablet with a udev rule, then replug the tablet:
  ```
  # /etc/udev/rules.d/70-pth660.rules
  SUBSYSTEM=="hidraw", ATTRS{idVendor}=="056a", ATTRS{idProduct}=="0357", MODE="0660", TAG+="uaccess"
  ```
  `sudo udevadm control --reload-rules && sudo udevadm trigger`

**To serve the page:** Docker only (`docker compose -f docker-compose.dev.yml up -d`). Tests run in a container,
so Node isn't needed on the PC. Any static web server works too, as long as `/firmware/` serves the `firmware/` folder.

**Optional tools:** the Python reference flasher needs Python + `pip install hidapi`; rebuilding firmware images
needs `capstone` and `keystone-engine`.

## What the web UI has to do

1. Connect with WebHID to `056A:0357`, usage page `0xFF0D`.
2. Show the current firmware version and running slot ([PROTOCOL.md](docs/PROTOCOL.md#which-slot-is-running)).
3. Let the user pick a package from `firmware/manifest.json`, verify its sha256, and parse it
   ([package format](docs/PROTOCOL.md#package-format-pkg)).
4. Flash the image for the **inactive** slot: erase, program, sector checksums, commit
   ([sequence](docs/PROTOCOL.md#flashing-one-slot)), then reboot and reconnect.
5. Optionally repeat for the other slot, so both slots carry the same build.

`reference/pth660_flash.py` does exactly this and was run for real on 2026-09-28
(`flash firmware/pth660_v245_best.pkg --arm`: 82 sectors, 1310 blocks, commit, reboot, 16 s).
Try it without `--arm` first; nothing is written without it.

```
pip install hidapi
python reference/pth660_flash.py status
python reference/pth660_flash.py info firmware/pth660_v245_best.pkg
python reference/pth660_flash.py flash firmware/pth660_v245_best.pkg          # dry run
python reference/pth660_flash.py flash firmware/pth660_v245_best.pkg --arm --both
```

## Rebuilding v2.45

```
python build/make_frame23.py --slot a --in build/base/slot_a_v165_600hz.bin --out slot_a_v245.bin --version 0x0245
python build/make_frame23.py --slot b --in build/base/slot_b_v165_600hz.bin --out slot_b_v245.bin --version 0x0245
python build/make_pkg.py --a slot_a_v245.bin --b slot_b_v245.bin --name "v2.45" --out pth660_v245.pkg
```

v3.22: `python build/make_frame23.py --slot a --in build/base/slot_a_v165_600hz.bin --out slot_a_v245.bin --version 0x0245`, then `python build/make_s2c.py --slot a --in slot_a_v245.bin --out slot_a_v322.bin --version 0x0322 --output --pos output --nos2 --delay-us 0 --predict --double` (same for slot b; needs arm-none-eabi-gcc, path in `GCC_DIR`), then `make_pkg.py`. v3.21: same with `--version 0x0321` and `--pace` instead of `--double`. Both byte-identical to the shipped packages (v3.22 sha256 `f498b4e0...` / `6774c436...`, v3.21 `19784ca8...` / `505c95df...`).

v2.99: `python build/make_cycles.py --in build/base/slot_a_v165_600hz.bin --slot a --k 0 --upsample 6 --perscan --version 0x0299 --out slot_a_v299.bin` (same for slot b), then `make_pkg.py` as above. Byte-identical to the shipped v2.99 (sha256 `670d8539...` / `218d94dc...`).

`make_frame23.py` and `make_cycles.py` need `capstone` and `keystone-engine`. Both build scripts are self-contained, and the
two slot images come out byte-identical to the shipped v2.45 (sha256 `0c0743b8...` / `5a720d36...`).

The full research (scan engine, sensor link, every experiment) stays in the parent repo:
`docs/SENSOR_LINK.md`, `tools/`.
