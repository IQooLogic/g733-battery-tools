# G733 KDE tray monitor

A small PyQt6 application that displays the Logitech G733 battery in the KDE
Plasma system tray. Its icon contains the current percentage, coloured by charge
level and blue while charging, and its tooltip adds the estimated time remaining
or until full when HeadsetControl reports one.

## Prerequisite

The parent directory's udev setup must already work:

```bash
cd ..
./battery-status.sh
```

It must show `"status": "success"` and a battery level before starting this
app. PyQt6 is already installed on this machine (`python-pyqt6`).

## Start it now

```bash
cd ~/WORK/g733-battery-tools/tray
./start.sh
```

The tray icon starts as `?`, then updates after its first reading. The number
inside it is the battery percentage. Left-click or double-click the icon for an
immediate refresh; right-click for Refresh now and Quit.

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

The default HeadsetControl location is:

```text
~/Downloads/headsetcontrol-x86_64.AppImage
```

Override it when starting the monitor with either an option or environment
variable:

```bash
./start.sh --command /path/to/headsetcontrol
HEADSETCONTROL=/path/to/headsetcontrol ./start.sh
```

The value can also be a bare command name, which is looked up on `PATH`. Use
this if HeadsetControl comes from your distribution instead of the AppImage:

```bash
HEADSETCONTROL=headsetcontrol ./start.sh
```

The location is resolved before each reading, not once at startup, so an
AppImage on a volume that is mounted later starts working without a restart.

The app never invokes HeadsetControl with `sudo`.

## Tests

```bash
cd ~/WORK/g733-battery-tools/tray
python3 test_tray.py
```

The suite needs only the standard library and PyQt6, and runs headless with the
Qt `offscreen` platform, so it needs no display and no headset. It drives the
real monitor against stub commands that print recorded HeadsetControl output,
covering the battery states, the command lookup, the icon colours, the error
logging and the command line.

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
- Blue means the headset is charging. The icon keeps the percentage when
  HeadsetControl reports one and shows a lightning bolt when it does not. The
  tooltip then gives the estimated time until full.
- A low-battery notification is sent once at 20% or below; it resets after the
  charge rises to at least 25%, or as soon as charging starts. No low-battery
  notification is sent while the headset charges.
- A grey `?` means the headset is off or out of range, the command failed, or no
  reading has completed. Hover the icon for the error detail.
- Errors are also written to standard error, so an autostarted monitor can be
  diagnosed by redirecting its output to a log file. A repeated error is logged
  once, and recovery is logged when a reading succeeds again.
- Each command request has a 15-second timeout, and failures are retried on the
  next interval.
