# Logitech G733 HID access tools

These scripts grant the local KDE desktop user access to the G733's HID
interface, verify that `headsetcontrol` can read its battery, and include a KDE
system-tray monitor in [`tray/`](tray/README.md).

## Prerequisites

- The G733 USB receiver is connected and the headset is on.
- HeadsetControl is executable at
  `~/Downloads/headsetcontrol-x86_64.AppImage`, or the `HEADSETCONTROL`
  environment variable points to it.
- `sudo` access is available to install the udev rule.

The target device is deliberately restricted to Logitech vendor/product ID
`046d:0b1f`, reported by HeadsetControl for this G733.

## Install the permission rule

From this directory:

```bash
./install-udev-rule.sh
```

Enter your sudo password when prompted. The script writes:

```text
/etc/udev/rules.d/99-logitech-g733-hidraw.rules
```

The installer writes a direct owner rule for the account that runs it (currently
`milos`): `OWNER="milos", MODE="0600"`. This is intentionally limited to the
G733's `046d:0b1f` HID interface and gives access to that account only. It is
used instead of `TAG+="uaccess"` because this KDE/logind setup did not apply a
user ACL to the hidraw device.

**Unplug and reconnect the G733 receiver after installation** (or reboot).
This is required because udev applies rules when it receives device events.

## Verify access and battery level

First inspect the HID devices and the ACL on the G733 node:

```bash
./check-hidraw.sh
```

For the detected G733, `getfacl` should show `owner: milos` and
`user::rw-` (the exact node number can change after reconnecting). Then retrieve
one battery reading:

```bash
./battery-status.sh
```

Successful output is JSON whose `devices[0].battery.level` is between `0` and
`100`. For example, a battery level of `73` means 73%.

If it still contains `"level": -1` and `"Could not open device"`, make sure
you reconnected the receiver after installing the rule, verify that the
receiver appears in `./check-hidraw.sh`, then log out/in or reboot and retry.
Do not run HeadsetControl with `sudo`; the rule is intended to make normal-user
access work.

## Polling for development

To see continuous JSON updates (stop with `Ctrl-C`):

```bash
POLL_SECONDS=60 ./watch-battery.sh
```

`headsetcontrol -f` intentionally runs forever. The included tray monitor uses
the one-shot query once per minute instead of retaining a long-running command.

## Install and run the KDE tray monitor

After `./battery-status.sh` reports a successful level, start the monitor:

```bash
cd ~/WORK/g733-battery-tools/tray
./start.sh
```

The tray icon displays the percentage; hover it for the percentage and estimated
remaining time. If Plasma hides it, open the system-tray settings and set
**G733 Battery** to **Always shown**.

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

## Custom HeadsetControl location

```bash
HEADSETCONTROL=/opt/headsetcontrol/headsetcontrol ./battery-status.sh
```

`TIMEOUT_SECONDS=20` changes the one-shot command timeout.

## Remove the rule

```bash
./remove-udev-rule.sh
```

Reconnect the receiver after removal to apply it.

## License

MIT. See [`LICENSE`](LICENSE).
