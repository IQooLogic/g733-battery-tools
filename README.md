# Logitech G733 HID access tools

These scripts grant the local KDE desktop user access to the G733's HID
interface, read its battery, and include a KDE system-tray monitor in
[`tray/`](tray/README.md). All talking to the headset is done by
[`tray/g733_headset.py`](tray/g733_headset.py), which speaks Logitech's HID++
protocol directly and needs only Python 3.

## Prerequisites

- The G733 USB receiver is connected and the headset is on.
- Python 3.
- `sudo` access is available to install the udev rule.

The target device is deliberately restricted to Logitech vendor/product ID
`046d:0b1f`, the receiver of this G733.

## Install the permission rule

From this directory:

```bash
./install-udev-rule.sh
```

Enter your sudo password when prompted. The script writes:

```text
/etc/udev/rules.d/99-logitech-g733-hidraw.rules
```

The installer writes a direct owner rule naming the account that runs it, as
`OWNER="<your user>", MODE="0600"`. This is intentionally limited to the G733's
`046d:0b1f` HID interface and grants access to that one account. It is used
instead of `TAG+="uaccess"` because some KDE/logind setups do not apply a user
ACL to hidraw devices.

The installer then re-triggers `hidraw` events, so a receiver that is already
connected picks up the rule straight away. If the receiver was not connected at
that moment, plug it in (or reboot); udev applies rules when it receives device
events.

## Verify access and battery level

First inspect the HID devices and the ACL on the G733 node:

```bash
./check-hidraw.sh
```

For the detected G733, `getfacl` should show your username as `owner:` and
`user::rw-` (the exact node number can change after reconnecting). Then take
one battery reading:

```bash
./battery-status.sh
```

Successful output is one JSON line:

```json
{"voltage_mv": 3812, "flags": 1, "state": "discharging"}
```

`state` is `discharging`, `charging` or `full`. The headset reports a voltage,
not a percentage; the tray monitor estimates one from it, as described in
[How the battery is read](tray/README.md#how-the-battery-is-read).

If it fails with `no permission to open /dev/hidrawN`, verify that the receiver
appears in `./check-hidraw.sh`, then unplug and reconnect it so udev applies
the rule on the device event. If that does not help, log out/in or reboot and
retry. `no answer from the headset` means the receiver is present but the
headset is off or out of range. Do not run the tools with `sudo`; the rule is
intended to make normal-user access work.

`TIMEOUT_SECONDS=20` changes the one-shot command timeout.

## Polling for development

To print a reading every minute (stop with `Ctrl-C`):

```bash
POLL_SECONDS=60 ./watch-battery.sh
```

A failed reading prints its reason and the loop carries on.

## Switch the lights

```bash
tray/g733_headset.py lights off
tray/g733_headset.py lights on
```

`on` is a cyan breathing effect on both lighting zones. The tray monitor's menu
does the same, and remembers the choice.

## Install and run the KDE tray monitor

Once `./battery-status.sh` prints a reading, start the monitor:

```bash
cd tray
./start.sh
```

The tray icon displays the estimated percentage, and turns blue with a
lightning bolt while the headset is on the cable. Hover it for the percentage
and the battery voltage it came from. If Plasma hides it, open the system-tray
settings and set **G733 Battery** to **Always shown**.

To make it start automatically whenever you log in to KDE:

```bash
./install-autostart.sh
```

This creates `~/.config/autostart/g733-battery-tray.desktop`. It will run on
your next login; log out and back in to test it. Remove that autostart entry
later with:

```bash
./remove-autostart.sh
```

See [`tray/README.md`](tray/README.md) for options and troubleshooting.

## Remove the rule

```bash
./remove-udev-rule.sh
```

Like the installer, this re-triggers `hidraw` events, so a connected receiver
returns to root ownership immediately. If it was not connected, the change
applies when you plug it in.

## Credits

The HID++ messages follow [HeadsetControl][hc], which these tools used before,
and [Solaar][solaar], whose voltage curve the tray monitor uses.
HeadsetControl was replaced because it counts only one of the headset's
charging flags values as charging, so a finished charge read as a discharging
battery, and its percentage read 100% a few minutes after a half-empty headset
was plugged in.

## License

MIT. See [`LICENSE`](LICENSE).

[hc]: https://github.com/Sapd/HeadsetControl
[solaar]: https://github.com/pwr-Solaar/Solaar
