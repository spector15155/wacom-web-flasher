# Before you flash: please read

This is an **unofficial, community-made tool** and **unofficial firmware** for the Wacom Intuos Pro M
(PTH-660). It is not made, approved or supported by Wacom.

## What we did to keep your tablet safe

- New firmware is only ever written to the slot the tablet is **not** running. The firmware you are
  using is never overwritten while it runs.
- Every write is **read back and checked** before the tablet is told to boot it. If anything doesn't
  match, nothing is switched over and your tablet keeps its current firmware.
- You can **back up both slots** before any change and restore that backup later.
- Wacom's original firmware is included, so you can always go back to stock.
- Each build here was tested on a real tablet by the author.

## What we can't promise

Even so, flashing firmware always carries some risk. A USB hiccup, a power cut, a hardware
difference or a bug we haven't found could leave a tablet misbehaving or, in the worst case, not
starting at all. Custom firmware may also affect your warranty.

**By using this tool you accept that you do so at your own risk.** The author can't be held responsible
for any damage, data loss, bricked tablets or warranty issues that result from using it. If you're not
comfortable with that, please don't flash, and there's no shame in that: the stock firmware works fine.

## If something goes wrong

- A write that stopped halfway is harmless: the tablet still boots its other slot. Just run the same
  step again.
- If the tablet stops responding, unplug it, **hold the power button until the lights go out**, then
  reconnect. This clears most stuck states.
- Restore your backup, or install the **Wacom stock** firmware from the list, to get back to a known state.

## Legal

This software and the firmware files are provided "as is", without warranty of any kind, express or
implied, including but not limited to the warranties of merchantability, fitness for a particular
purpose and non-infringement. In no event shall the author be liable for any claim, damages or other
liability arising from, out of or in connection with the software or its use. Wacom and Intuos are
trademarks of Wacom Co., Ltd.; this project is not affiliated with Wacom.
