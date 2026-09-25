#!/usr/bin/env python3
"""Talk to a Logitech G733 headset over HID++: read its battery, switch its lights.

Usage:

    g733_headset.py battery
    g733_headset.py lights on|off|status

Each command prints one JSON object on success and exits with status 0:

    {"voltage_mv": 3812, "flags": 1, "state": "discharging"}
    {"lights": "off"}

On failure it prints the reason to standard error and exits with status 1.
Only the standard library is used, so it starts quickly on every poll.

The G733 has no battery-percentage feature. Its only battery source is
ADC_MEASUREMENT (HID++ 2.0 feature 0x1F20), which reports the cell voltage and
a flags byte, and both are passed through with the state the flags encode. The
lights are the two zones of COLOR_LED_EFFECTS (0x8070).
"""

from __future__ import annotations

import argparse
import json
import os
import select
import sys
import time
from pathlib import Path

VENDOR_ID = 0x046D
PRODUCT_ID = 0x0B1F

HIDPP_LONG_REPORT = 0x11
HIDPP_LONG_LENGTH = 20
# The receiver answers for the headset at this device index.
DEVICE_INDEX = 0xFF
# Echoed back in each reply, so a reply to another program's request, which
# the hidraw node also delivers, is never taken for ours.
SOFTWARE_ID = 0x0B
# A HID++ 2.0 error reply puts this in the feature-index byte.
HIDPP20_ERROR = 0xFF
# "Report ID 0x11" in a HID report descriptor: the interface that speaks HID++.
LONG_REPORT_DESCRIPTOR_ITEM = bytes([0x85, HIDPP_LONG_REPORT])
REPLY_TIMEOUT_SECONDS = 2.0
# A receiver can remain connected while its headset is switched off or out of
# range. Keep that expected condition distinct from a receiver/tool failure.
HEADSET_UNAVAILABLE_EXIT = 3

ROOT_FEATURE_INDEX = 0x00
ADC_MEASUREMENT_FEATURE = 0x1F20
COLOR_LED_EFFECTS_FEATURE = 0x8070

# The flags values measured on this headset. Bit 0 marks a valid measurement,
# bit 1 external power, and bit 2 a finished charge: with the cable still in,
# the flags went from 0x03 to 0x07 while the voltage fell from 4210 to 4112 mV
# as the charger stopped, and off the cable they read 0x01. Any other value is
# reported as an error rather than guessed at, so a new state shows up instead
# of being shown wrongly.
FLAG_STATES = {0x01: "discharging", 0x03: "charging", 0x07: "full"}

# COLOR_LED_EFFECTS functions, and the effect ids a zone lists.
LED_GET_INFO = 0
LED_GET_ZONE_INFO = 1
LED_GET_ZONE_EFFECT_INFO = 2
LED_SET_ZONE_EFFECT = 3
# Available on COLOR_LED_EFFECTS v4 when its extended capabilities advertise
# that the current setting can be read.
LED_GET_ZONE_EFFECT = 14
LED_INFO_HAS_ZONE_EFFECT = 0x0001
EFFECT_DISABLED = 0x0000
EFFECT_BREATHE = 0x000A
# Breathe parameters: colour 0x00B6FF, a 4000 ms period, waveform 0 and
# intensity 100. "On" has always meant this effect here, as it does in
# HeadsetControl, which this tool replaced; Disabled ignores the parameters.
BREATHE_PARAMETERS = bytes([0x00, 0xB6, 0xFF, 0x0F, 0xA0, 0x00, 0x64, 0x00, 0x00, 0x00])

HIDPP20_ERRORS = {
    0x01: "unknown",
    0x02: "invalid argument",
    0x03: "out of range",
    0x04: "hardware error",
    0x05: "internal error",
    0x06: "invalid feature index",
    0x07: "invalid function",
    0x08: "busy",
    0x09: "unsupported",
}

SYSFS_HIDRAW = Path("/sys/class/hidraw")
INSTALL_RULE = Path(__file__).resolve().parent.parent / "install-udev-rule.sh"
UDEV_HINT = (
    f"run {INSTALL_RULE}, then reconnect the receiver"
    " (log in again if the rule added you to a group)"
)


class HeadsetError(Exception):
    """A request to the headset failed; the message says why."""


class HeadsetUnavailable(HeadsetError):
    """The receiver is present but the headset did not answer."""


def matches_g733(uevent: str) -> bool:
    """Return whether a hidraw uevent describes the G733 receiver."""
    for line in uevent.splitlines():
        key, _, value = line.partition("=")
        if key == "HID_ID":
            # HID_ID is "bus:vendor:product", each in hexadecimal.
            parts = value.split(":")
            if len(parts) != 3:
                return False
            try:
                _bus, vendor, product = (int(part, 16) for part in parts)
            except ValueError:
                return False
            return (vendor, product) == (VENDOR_ID, PRODUCT_ID)
    return False


def find_device(sysfs: Path = SYSFS_HIDRAW) -> Path:
    """Return the /dev node of the G733 interface that speaks HID++."""
    for node in sorted(sysfs.glob("hidraw*")):
        try:
            uevent = (node / "device" / "uevent").read_text()
            if not matches_g733(uevent):
                continue
            descriptor = (node / "device" / "report_descriptor").read_bytes()
        except OSError as exc:
            raise HeadsetError(f"could not inspect {node}: {exc}") from exc
        if LONG_REPORT_DESCRIPTOR_ITEM in descriptor:
            return Path("/dev") / node.name
    raise HeadsetError(
        f"G733 receiver {VENDOR_ID:04x}:{PRODUCT_ID:04x} not found; is it plugged in?"
    )


def request(
    fd: int,
    feature_index: int,
    function: int,
    params: bytes = b"",
    timeout: float = REPLY_TIMEOUT_SECONDS,
) -> bytes:
    """Send one HID++ 2.0 request and return the parameter bytes of its reply."""
    address = (function << 4) | SOFTWARE_ID
    packet = bytes([HIDPP_LONG_REPORT, DEVICE_INDEX, feature_index, address]) + params
    os.write(fd, packet.ljust(HIDPP_LONG_LENGTH, b"\0"))

    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        reply = os.read(fd, 64)
        # The node also delivers notifications and replies meant for other
        # programs; only a reply that echoes this request is taken.
        if len(reply) < 6:
            continue
        if reply[2] == feature_index and reply[3] == address:
            return reply[4:]
        if reply[2] == HIDPP20_ERROR and reply[3] == feature_index and reply[4] == address:
            code = reply[5]
            name = HIDPP20_ERRORS.get(code, "unrecognised error")
            raise HeadsetError(f"headset answered with HID++ error 0x{code:02x} ({name})")
    raise HeadsetUnavailable(
        f"no answer from the headset within {timeout:g}s; is it switched on?"
    )


def feature_index(fd: int, feature: int, name: str) -> int:
    """Return where the headset keeps one feature."""
    # Looked up rather than hard-coded, so a firmware that moves the feature
    # still works. ROOT function 0 is getFeature(feature id).
    index = request(fd, ROOT_FEATURE_INDEX, 0, feature.to_bytes(2, "big"))[0]
    if index == 0:
        raise HeadsetError(f"headset has no {name} feature (0x{feature:04X})")
    return index


def decode(voltage_mv: int, flags: int) -> dict[str, object]:
    """Turn one ADC measurement into the reading this program prints."""
    if not flags & 0x01:
        raise HeadsetError(f"headset reported no valid measurement (flags 0x{flags:02x})")
    state = FLAG_STATES.get(flags)
    if state is None:
        raise HeadsetError(f"unrecognised battery flags 0x{flags:02x} at {voltage_mv} mV")
    return {"voltage_mv": voltage_mv, "flags": flags, "state": state}


def read_battery(fd: int) -> dict[str, object]:
    """Take one reading from ADC_MEASUREMENT."""
    index = feature_index(fd, ADC_MEASUREMENT_FEATURE, "ADC measurement")
    reply = request(fd, index, 0)
    return decode(int.from_bytes(reply[0:2], "big"), reply[2])


def effect_slot(fd: int, index: int, zone: int, effect_id: int) -> int:
    """Return the position of one effect in a zone's list of effects."""
    # setZoneEffect takes the position, not the id, and the order of the list
    # is the firmware's, so it is looked up in each zone.
    count = request(fd, index, LED_GET_ZONE_INFO, bytes([zone, 0xFF, 0x00]))[3]
    for slot in range(count):
        info = request(fd, index, LED_GET_ZONE_EFFECT_INFO, bytes([zone, slot, 0x00]))
        if int.from_bytes(info[2:4], "big") == effect_id:
            return slot
    raise HeadsetError(f"LED zone {zone} has no effect 0x{effect_id:04x}")


def lights_info(fd: int) -> tuple[int, int, int]:
    """Return the LED feature index, zone count, and extended capabilities."""
    index = feature_index(fd, COLOR_LED_EFFECTS_FEATURE, "LED effects")
    info = request(fd, index, LED_GET_INFO)
    zones = info[0]
    if zones == 0:
        raise HeadsetError("headset reports no LED zones")
    # getInfo returns zone count, two bytes of NV capabilities, then two bytes
    # of extended capabilities.
    return index, zones, int.from_bytes(info[3:5], "big")


def read_lights(fd: int) -> dict[str, object]:
    """Return whether any LED zone is currently using an enabled effect."""
    index, zones, capabilities = lights_info(fd)
    if not capabilities & LED_INFO_HAS_ZONE_EFFECT:
        raise HeadsetError("headset cannot report its current LED effect")

    for zone in range(zones):
        setting = request(fd, index, LED_GET_ZONE_EFFECT, bytes([zone]))
        if setting[0] != zone:
            raise HeadsetError(f"LED effect reply named zone {setting[0]}, expected {zone}")
        # getZoneEffect returns the active effect id. Unlike setZoneEffect,
        # this is not the effect's position in the zone's list.
        if setting[1] != EFFECT_DISABLED:
            return {"lights": "on"}
    return {"lights": "off"}


def set_lights(fd: int, on: bool) -> dict[str, object]:
    """Set every LED zone to Breathe, or to Disabled."""
    index, zones, _capabilities = lights_info(fd)
    effect_id = EFFECT_BREATHE if on else EFFECT_DISABLED
    for zone in range(zones):
        slot = effect_slot(fd, index, zone, effect_id)
        request(fd, index, LED_SET_ZONE_EFFECT, bytes([zone, slot]) + BREATHE_PARAMETERS)
    return {"lights": "on" if on else "off"}


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Talk to a Logitech G733 headset over HID++.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("battery", help="print one battery reading")
    lights = commands.add_parser("lights", help="switch the headset lights, or print their state")
    lights.add_argument("state", choices=("on", "off", "status"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    try:
        path = find_device()
        try:
            fd = os.open(path, os.O_RDWR)
        except PermissionError as exc:
            raise HeadsetError(f"no permission to open {path}; {UDEV_HINT}") from exc
        except OSError as exc:
            raise HeadsetError(f"could not open {path}: {exc}") from exc
        try:
            if arguments.command == "battery":
                result = read_battery(fd)
            elif arguments.state == "status":
                result = read_lights(fd)
            else:
                result = set_lights(fd, arguments.state == "on")
        except OSError as exc:
            raise HeadsetError(f"could not talk to {path}: {exc}") from exc
        finally:
            os.close(fd)
    except HeadsetError as exc:
        print(exc, file=sys.stderr)
        return HEADSET_UNAVAILABLE_EXIT if isinstance(exc, HeadsetUnavailable) else 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
