# G733 KDE tray monitor

A small PyQt6 application that displays the Logitech G733 battery in the KDE
Plasma system tray. Its icon contains the estimated percentage, coloured by
charge level, and turns blue with a lightning bolt while the headset is on the
cable. Its tooltip adds the battery voltage the estimate came from. Its context
menu can also switch the headset lights on or off, and applies the last state
you chose each time it starts, as soon as the headset answers.

Both the battery and the lights go through
[`g733_headset.py`](g733_headset.py), which talks HID++ to the headset
directly. See [How the battery is read](#how-the-battery-is-read).

## Prerequisite

The parent directory's udev setup must already work. Take one reading:

```bash
./g733_headset.py battery
```

It must print one JSON line, such as
`{"voltage_mv": 3812, "flags": 1, "state": "discharging"}`, before you start
this app. A permission error means the udev rule is not installed or not yet
applied; see the [parent README](../README.md). PyQt6 is also required; on Arch
it is packaged as `python-pyqt6`.

## Start it now

From this directory:

```bash
./start.sh
```

The tray icon starts as `?`, then updates after its first reading. The number
inside it is the estimated battery percentage; while the headset charges, the
icon shows a bolt instead. Left-click or double-click the icon for an
immediate refresh; right-click for Refresh now, Lights and Quit.

`./start.sh` runs the monitor in the foreground, so its terminal stays occupied
until you use the tray's **Quit** action. Quit returns you to the shell without
output. `Ctrl-C` also stops it immediately, without a Python traceback; the
shell reports exit status 130. To keep the shell prompt while testing, use:

```bash
./start.sh >/tmp/g733-battery-tray.log 2>&1 & disown
```

Plasma may initially hide new status items. Open the system-tray settings and
set **G733 Battery** to **Always shown** if necessary.

## Options

The normal polling interval is 60 seconds, and the minimum is 5. The following
runs it every two minutes:

```bash
./start.sh --interval 120
```

The `POLL_SECONDS` environment variable does the same thing, and the option wins
if you use both:

```bash
POLL_SECONDS=120 ./start.sh
```

`--help` prints the usage. The `--command` option and the `HEADSETCONTROL`
variable of earlier versions are gone; `--command` is now rejected as an
unknown argument.

## Tests

From this directory:

```bash
python3 -m unittest
```

The suite needs only the standard library and PyQt6, and runs headless with the
Qt `offscreen` platform, so it needs no display and no headset.
`test_tray.py` drives the real monitor against a stub headset tool, covering
the battery states, the estimate and its smoothing, the notifications, the
Lights item and the state it remembers, the icon colours and the charging
bolt, the error reporting and the command line. `test_headset.py` runs the
headset tool against a simulated headset and a simulated sysfs tree, covering
device lookup, reply matching, the battery flags and the lights zones.

Linting and formatting use [ruff](https://docs.astral.sh/ruff/), configured in
the repository's `pyproject.toml`. It is optional to run, and not needed to use
the monitor:

```bash
ruff check .      # lint
ruff format .     # format in place
```

## Start automatically with KDE

```bash
./install-autostart.sh
```

This creates a per-user XDG entry at:

```text
~/.config/autostart/g733-battery-tray.desktop
```

It refers to this exact checkout, so do not move the `tray` directory after
installing it. To stop automatic startup:

```bash
./remove-autostart.sh
```

## How the battery is read

The G733 does not report a percentage. Its only battery source is the HID++
feature `ADC_MEASUREMENT` (`0x1F20`), which gives the cell voltage and a flags
byte. `g733_headset.py battery` finds the headset's HID++ interface under
`/sys/class/hidraw`, asks the headset where that feature is, and prints one
reading:

| flags  | state         | shown as                                   |
|--------|---------------|--------------------------------------------|
| `0x01` | `discharging` | the estimated percentage                   |
| `0x03` | `charging`    | a bolt, with no percentage                 |
| `0x07` | `full`        | `100` with a bolt; announced once          |

These are the values measured on this headset. Any other flags value is
reported as an error that names it, rather than guessed at. HeadsetControl,
which this monitor used before, counts only `0x03` as charging, so it reports
a finished charge as a discharging battery.

While discharging, the percentage is estimated from the voltage with the
13-point Li-ion curve that [Solaar][solaar] uses, and that OpenLogi copies. The
voltage moves by tens of millivolts with volume and lighting load, which is
several percent on that curve, so the icon shows the median of the last five
discharging readings. At the default interval a real change shows within three
minutes. The history starts again after the headset is charged or cannot be
read.

While charging, no percentage is shown. The charger holds the voltage near
4.2 V long before the battery is full, so any estimate from it reads 100%
within minutes of plugging in. Just after the headset comes off the cable, its
voltage is still high and settles over a while, so the first readings can be
somewhat high.

## Behaviour

- Green: above 50%; amber: 21–50%; red: 20% or below.
- Blue and a lightning bolt mean the headset is on the cable. While it charges
  the bolt fills the icon; once charging has finished the icon shows `100`
  with the bolt beside it. The bolt is there so charging does not depend on
  telling blue from green.
- A low-battery notification is sent once at 20% or below; it resets after the
  charge rises to at least 25%, or as soon as charging starts. No low-battery
  notification is sent while the headset charges.
- A "fully charged" notification is sent once when the headset reports that
  charging has finished. It is sent again only after the headset has been off
  the cable, so a headset left charging overnight is announced once.
- A grey `!` means something went wrong: the headset is off or out of range, or
  the headset tool failed. Hover the icon for what happened. The `!` is
  replaced by the battery reading as soon as one succeeds again.
- A grey `?` means no reading has completed yet, which is the state the monitor
  starts in.
- Errors are also written to standard error, so an autostarted monitor can be
  diagnosed by redirecting its output to a log file. A repeated error is logged
  once, and recovery is logged when a reading succeeds again.
- **Lights** is ticked while the lights are on. Clicking it switches them to
  the other state by running `g733_headset.py lights on` or
  `g733_headset.py lights off`. Each sets both lighting zones, either to a
  cyan breathing effect or to Disabled; the effects are looked up in the
  headset's own list rather than assumed. The item is disabled while a request
  runs, and the request uses its own process, so it does not cancel the
  current battery reading.
- The state you choose is remembered. After the first successful battery
  reading, the monitor also runs `g733_headset.py lights status` and the tick
  shows the headset's actual setting. This corrects the tick when a headset is
  powered on after the monitor and starts with its lights on. The tick changes
  immediately after a successful lights request too.
- The remembered state is applied again each time the monitor starts, once a
  battery reading has succeeded, since that shows the headset is on. If the
  headset is off when the monitor starts, as it often is for an autostarted
  monitor, the state waits and is applied after the first reading that
  succeeds once you switch the headset on. A restore that fails is tried again
  after the next successful reading. A click on **Lights** replaces a restore
  that is still waiting.
- The remembered state is stored in:

  ```text
  ~/.config/g733-battery-tray/state.json
  ```

  `XDG_CONFIG_HOME` is honoured. Delete the file to stop applying a state at
  startup. An unreadable or invalid file is logged and ignored, not repaired.
- For about two seconds after a lights change the headset gave no usable
  battery reading, when HeadsetControl switched the lights. The monitor therefore
  holds the next reading back until that window has passed, and takes it then
  rather than waiting a full interval, so a lights change does not show a
  false `!`.
- A failed lights request is logged, notified, and then kept in the tooltip and
  under the Lights item until a lights request succeeds. It is deliberately not
  put on the icon: the icon reports the battery, and that reading is still
  valid. A failed restore is not notified: you pressed nothing to cause it,
  and it is tried again after the next successful reading.
- Each battery reading and lights request has a 15-second timeout. A failed
  battery reading is retried at the next interval; a failed lights request is
  not retried, so click **Lights** again. A failed startup status check is
  retried after the next successful battery reading.

[solaar]: https://github.com/pwr-Solaar/Solaar
