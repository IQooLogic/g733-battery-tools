#!/usr/bin/env python3
"""Tests for the G733 HID++ headset tool.

Run with `python3 test_headset.py` or `python3 -m unittest`. The headset
is played by the far end of a SOCK_SEQPACKET socket pair, which keeps each
report whole the way a hidraw node does, and the device lookup runs against a
throwaway sysfs tree, so neither a headset nor root is needed.
"""

from __future__ import annotations

import contextlib
import io
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import g733_headset as headset

G733_UEVENT = "DRIVER=hid-generic\nHID_ID=0003:0000046D:00000B1F\nHID_NAME=Logitech G733\n"
OTHER_UEVENT = "DRIVER=hid-generic\nHID_ID=0003:0000046D:0000C52B\nHID_NAME=Unifying\n"
# A descriptor fragment with and without the HID++ long report.
HIDPP_DESCRIPTOR = bytes([0x06, 0x43, 0xFF, 0x85, 0x11, 0x75, 0x08])
AUDIO_DESCRIPTOR = bytes([0x05, 0x0C, 0x85, 0x01, 0x75, 0x01])
ADC_INDEX = 0x08


def reply(feature_index: int, function: int, params: bytes, software_id: int = 0x0B) -> bytes:
    """Build the report the headset sends back for one request."""
    head = bytes([headset.HIDPP_LONG_REPORT, headset.DEVICE_INDEX, feature_index])
    return (head + bytes([(function << 4) | software_id]) + params).ljust(20, b"\0")


def error_reply(feature_index: int, function: int, code: int) -> bytes:
    address = (function << 4) | headset.SOFTWARE_ID
    head = bytes([headset.HIDPP_LONG_REPORT, headset.DEVICE_INDEX, headset.HIDPP20_ERROR])
    return (head + bytes([feature_index, address, code])).ljust(20, b"\0")


class FakeHeadset:
    """Answers each request it receives with the next scripted list of reports."""

    def __init__(self, test: unittest.TestCase, answers: list[list[bytes]]) -> None:
        self.host, self.device = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        test.addCleanup(self.host.close)
        test.addCleanup(self.device.close)
        self.requests: list[bytes] = []
        self.thread = threading.Thread(target=self.serve, args=(answers,), daemon=True)
        self.thread.start()

    def serve(self, answers: list[list[bytes]]) -> None:
        for reports in answers:
            self.requests.append(self.device.recv(64))
            for report in reports:
                self.device.send(report)

    @property
    def fd(self) -> int:
        return self.host.fileno()


class DecodeTests(unittest.TestCase):
    def test_measured_flag_values_map_to_their_states(self) -> None:
        for flags, state in ((0x01, "discharging"), (0x03, "charging"), (0x07, "full")):
            with self.subTest(flags=flags):
                self.assertEqual(
                    headset.decode(3900, flags),
                    {"voltage_mv": 3900, "flags": flags, "state": state},
                )

    def test_an_invalid_measurement_is_an_error(self) -> None:
        with self.assertRaisesRegex(headset.HeadsetError, "no valid measurement"):
            headset.decode(0, 0x00)

    def test_an_unseen_flag_value_is_an_error_naming_it(self) -> None:
        # Guessing would show a state the headset may not be in.
        with self.assertRaisesRegex(headset.HeadsetError, "0x05 at 3900 mV"):
            headset.decode(3900, 0x05)


class RequestTests(unittest.TestCase):
    def test_a_reading_looks_up_the_feature_then_reads_it(self) -> None:
        fake = FakeHeadset(
            self,
            [
                [reply(0x00, 0, bytes([ADC_INDEX, 0x00, 0x04]))],
                [reply(ADC_INDEX, 0, bytes([0x10, 0x10, 0x07]))],
            ],
        )
        self.assertEqual(
            headset.read_battery(fake.fd), {"voltage_mv": 4112, "flags": 7, "state": "full"}
        )
        fake.thread.join(1)
        # getFeature(0x1F20) on ROOT, then function 0 of the index it returned.
        self.assertEqual(fake.requests[0][:6], bytes([0x11, 0xFF, 0x00, 0x0B, 0x1F, 0x20]))
        self.assertEqual(fake.requests[1][:4], bytes([0x11, 0xFF, ADC_INDEX, 0x0B]))
        self.assertEqual(len(fake.requests[1]), headset.HIDPP_LONG_LENGTH)

    def test_unrelated_reports_are_skipped(self) -> None:
        # A notification, and a reply to another program's software id 0x0A.
        notification = reply(0x04, 0, b"\x01\x02")
        other_program = reply(ADC_INDEX, 0, bytes([0x0E, 0x00, 0x01]), software_id=0x0A)
        fake = FakeHeadset(
            self,
            [[notification, other_program, reply(ADC_INDEX, 0, bytes([0x0E, 0x10, 0x01]))]],
        )
        self.assertEqual(headset.request(fake.fd, ADC_INDEX, 0)[:3], bytes([0x0E, 0x10, 0x01]))

    def test_an_error_reply_is_reported_by_name(self) -> None:
        fake = FakeHeadset(self, [[error_reply(ADC_INDEX, 0, 0x08)]])
        with self.assertRaisesRegex(headset.HeadsetError, r"0x08 \(busy\)"):
            headset.request(fake.fd, ADC_INDEX, 0)

    def test_silence_is_reported_as_a_timeout(self) -> None:
        fake = FakeHeadset(self, [[]])
        with self.assertRaisesRegex(headset.HeadsetError, "no answer"):
            headset.request(fake.fd, ADC_INDEX, 0, timeout=0.2)

    def test_a_headset_without_the_feature_is_reported(self) -> None:
        fake = FakeHeadset(self, [[reply(0x00, 0, bytes([0x00, 0x00, 0x00]))]])
        with self.assertRaisesRegex(headset.HeadsetError, "0x1F20"):
            headset.read_battery(fake.fd)


LED_INDEX = 0x04


def led_zone_answers(effect_ids: tuple[int, ...]) -> list[list[bytes]]:
    """Answer getZoneInfo for one zone, then getZoneEffectInfo for each effect in turn."""
    answers = [[reply(LED_INDEX, 1, bytes([0x00, 0x00, 0x02, len(effect_ids)]))]]
    for slot, effect_id in enumerate(effect_ids):
        params = bytes([0x00, slot]) + effect_id.to_bytes(2, "big")
        answers.append([reply(LED_INDEX, 2, params)])
    return answers


# The effect list both zones of the G733 report: Disabled, Static, Breathe, Cycle.
G733_EFFECTS = (0x0000, 0x0001, 0x000A, 0x0003)


class LightsTests(unittest.TestCase):
    def lights(self, on: bool, effects: tuple[int, ...] = G733_EFFECTS) -> FakeHeadset:
        """Run set_lights against a two-zone headset and return what it was sent."""
        answers = [
            [reply(0x00, 0, bytes([LED_INDEX, 0x00, 0x00]))],
            [reply(LED_INDEX, 0, bytes([0x02]))],
        ]
        for _zone in range(2):
            wanted = headset.EFFECT_BREATHE if on else headset.EFFECT_DISABLED
            slot = effects.index(wanted)
            answers += led_zone_answers(effects)[: slot + 2]
            answers.append([reply(LED_INDEX, 3, b"")])
        fake = FakeHeadset(self, answers)
        self.assertEqual(headset.set_lights(fake.fd, on), {"lights": "on" if on else "off"})
        fake.thread.join(1)
        return fake

    def zone_writes(self, fake: FakeHeadset) -> list[bytes]:
        set_effect = (headset.LED_SET_ZONE_EFFECT << 4) | headset.SOFTWARE_ID
        return [r[4:16] for r in fake.requests if r[2] == LED_INDEX and r[3] == set_effect]

    def test_off_sets_every_zone_to_the_disabled_slot(self) -> None:
        writes = self.lights(on=False)
        self.assertEqual([w[:2] for w in self.zone_writes(writes)], [b"\x00\x00", b"\x01\x00"])

    def test_on_sets_every_zone_to_breathe_with_its_parameters(self) -> None:
        writes = self.zone_writes(self.lights(on=True))
        self.assertEqual([w[:2] for w in writes], [b"\x00\x02", b"\x01\x02"])
        self.assertEqual({w[2:] for w in writes}, {headset.BREATHE_PARAMETERS})

    def test_the_slot_is_found_by_id_not_assumed(self) -> None:
        # A firmware that lists Breathe first must still get Breathe.
        writes = self.zone_writes(self.lights(on=True, effects=(0x000A, 0x0000)))
        self.assertEqual([w[:2] for w in writes], [b"\x00\x00", b"\x01\x00"])

    def test_a_zone_without_the_effect_is_an_error(self) -> None:
        answers = [
            [reply(0x00, 0, bytes([LED_INDEX, 0x00, 0x00]))],
            [reply(LED_INDEX, 0, bytes([0x01]))],
            *led_zone_answers((0x0000, 0x0001)),
        ]
        fake = FakeHeadset(self, answers)
        with self.assertRaisesRegex(headset.HeadsetError, "no effect 0x000a"):
            headset.set_lights(fake.fd, True)


class CommandLineTests(unittest.TestCase):
    def test_the_commands_parse(self) -> None:
        self.assertEqual(headset.parse_arguments(["battery"]).command, "battery")
        self.assertEqual(headset.parse_arguments(["lights", "off"]).state, "off")

    def test_an_unknown_lights_state_is_rejected(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            headset.parse_arguments(["lights", "dim"])
        self.assertEqual(raised.exception.code, 2)


class FindDeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sysfs = Path(tmp.name)

    def node(self, name: str, uevent: str, descriptor: bytes) -> None:
        device = self.sysfs / name / "device"
        device.mkdir(parents=True)
        (device / "uevent").write_text(uevent)
        (device / "report_descriptor").write_bytes(descriptor)

    def test_the_g733_hidpp_interface_is_found(self) -> None:
        self.node("hidraw2", OTHER_UEVENT, HIDPP_DESCRIPTOR)
        self.node("hidraw7", G733_UEVENT, AUDIO_DESCRIPTOR)
        self.node("hidraw8", G733_UEVENT, HIDPP_DESCRIPTOR)
        self.assertEqual(headset.find_device(self.sysfs), Path("/dev/hidraw8"))

    def test_a_missing_receiver_is_reported(self) -> None:
        self.node("hidraw2", OTHER_UEVENT, HIDPP_DESCRIPTOR)
        with self.assertRaisesRegex(headset.HeadsetError, "046d:0b1f not found"):
            headset.find_device(self.sysfs)

    def test_a_malformed_uevent_does_not_match(self) -> None:
        for uevent in ("HID_ID=0003:046D\n", "HID_ID=zz:yy:xx\n", "DRIVER=hid-generic\n"):
            with self.subTest(uevent=uevent):
                self.assertFalse(headset.matches_g733(uevent))


if __name__ == "__main__":
    unittest.main(verbosity=2)
