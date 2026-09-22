# G733 KDE tray monitor

A small PyQt6 application that displays the Logitech G733 battery in the KDE
Plasma system tray. Its icon contains the current percentage, coloured by charge
level and blue while charging, and its tooltip adds the estimated time remaining
or until full when HeadsetControl reports one. Its context menu can also switch
the headset lights on or off, and applies the last state you chose each time it
starts.

## Prerequisite

The parent directory's udev setup must already work:

```bash
cd ..
./battery-status.sh
```

It must show `"status": "success"` and a battery level before starting this
app. PyQt6 is also required; on Arch it is packaged as `python-pyqt6`.

## Start it now

From this directory:

```bash
./start.sh
```

The tray icon starts as `?`, then updates after its first reading. The number
inside it is the battery percentage. Left-click or double-click the icon for an
immediate refresh; right-click for Refresh now, Turn lights on, Turn lights off
and Quit.

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

With no option or variable set, the monitor tries two locations in order:

```text
~/Downloads/headsetcontrol-x86_64.AppImage
headsetcontrol            (any command of that name on PATH)
```

The AppImage comes first because it is usually the newer build. The second entry
means a distribution package works with no configuration; install one with, for
example, `sudo pacman -S headsetcontrol`. Both come from
[HeadsetControl][hc], whose [releases][hc-releases] page publishes the AppImage.

Override both when starting the monitor with either an option or environment
variable:

```bash
./start.sh --command /path/to/headsetcontrol
HEADSETCONTROL=/path/to/headsetcontrol ./start.sh
```

The value can also be a bare command name, which is looked up on `PATH`:

```bash
HEADSETCONTROL=headsetcontrol ./start.sh
```

An explicit choice disables the fallback, so only that command is tried and a
typo is reported rather than quietly replaced by another build. `--help` prints
the defaults in use.

The location is resolved before each reading, not once at startup, so an
AppImage on a volume that is mounted later starts working without a restart.

The app never invokes HeadsetControl with `sudo`.

## Tests

From this directory:

```bash
python3 test_tray.py
```

The suite needs only the standard library and PyQt6, and runs headless with the
Qt `offscreen` platform, so it needs no display and no headset. It drives the
real monitor against stub commands that print recorded HeadsetControl output,
covering the battery states, the command lookup, the lights items and the state
they remember, the icon colours and the charging bolt, the error reporting and
the command line.

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

## Behaviour

- Green: above 50%; amber: 21–50%; red: 20% or below.
- Blue and a lightning bolt mean the headset is charging. The icon keeps the
  percentage when HeadsetControl reports one and draws the bolt beside it; when
  no percentage is reported, the bolt fills the icon. The bolt is there so
  charging does not depend on telling blue from green. The tooltip then gives
  the estimated time until full.
- A low-battery notification is sent once at 20% or below; it resets after the
  charge rises to at least 25%, or as soon as charging starts. No low-battery
  notification is sent while the headset charges.
- A "fully charged" notification is sent once when a charging reading reaches
  100%. HeadsetControl reports no "full" status, so that reading is what full
  means here. It is sent again only after the headset comes off the cable or
  drops below 95%, so a headset left charging overnight is announced once.
- A grey `!` means something went wrong: the headset is off or out of range, or
  the command failed. Hover the icon for what happened. The `!` is replaced by
  the battery reading as soon as one succeeds again.
- A grey `?` means no reading has completed yet, which is the state the monitor
  starts in.
- Errors are also written to standard error, so an autostarted monitor can be
  diagnosed by redirecting its output to a log file. A repeated error is logged
  once, and recovery is logged when a reading succeeds again.
- **Turn lights on** and **Turn lights off** run `headsetcontrol -l 1` and
  `headsetcontrol -l 0`. Both items are disabled while a request runs, and the
  request uses its own process, so it does not cancel the current battery
  reading.
- The state you choose is remembered and ticked in the menu. It is applied again
  each time the monitor starts, which restores it after the headset has been
  powered off and on. Only a request that succeeded is remembered.
- The remembered state is stored in:

  ```text
  ~/.config/g733-battery-tray/state.json
  ```

  `XDG_CONFIG_HOME` is honoured. Delete the file to stop applying a state at
  startup. An unreadable or invalid file is logged and ignored, not repaired.
- For about two seconds after a lights change the headset answers battery
  requests with `BATTERY_UNAVAILABLE`. The monitor therefore holds the next
  reading back until that window has passed, and takes it then rather than
  waiting a full interval, so a lights change does not show a false `?`.
- A failed lights request is logged, notified, and then kept in the tooltip and
  under the lights items until a lights request succeeds. It is deliberately not
  put on the icon: the icon reports the battery, and that reading is still
  valid. The restore at startup is not notified — the headset is commonly off
  when an autostarted monitor begins, and you pressed nothing to cause it.
- Each command request has a 15-second timeout, and failures are retried on the
  next interval.

[hc]: https://github.com/Sapd/HeadsetControl
[hc-releases]: https://github.com/Sapd/HeadsetControl/releases
