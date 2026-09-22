#!/usr/bin/env python3
"""Tests for the G733 tray monitor.

Run with `python3 test_tray.py` or `python3 -m unittest`. Only the standard
library and PyQt6 are needed; there is no test-runner dependency.

The battery cases drive the real G733Tray through a real QProcess against stub
executables, because the states that matter here are ones HeadsetControl
reports and the monitor has to interpret, not ones a mock would prove.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Must be set before the first QApplication; the suite needs no display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtCore import QEventLoop, QSize  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import g733_battery_tray as tray  # noqa: E402

APP = QApplication.instance() or QApplication([])

# Keep the monitor's own error logging out of the test report; the tests that
# care about it capture it explicitly with assertLogs.
tray.LOGGER.addHandler(logging.NullHandler())
tray.LOGGER.propagate = False

# A point inside the icon's rounded rectangle, clear of the border and the
# centred label, so it samples the state colour.
FILL_SAMPLE = (10, 32)
REQUEST_TIMEOUT_SECONDS = 15


def device_json(battery: str) -> str:
    return '{"device_count":1,"devices":[{"status":"success","battery":%s}]}' % battery


def read_once(command: str) -> str:
    """Run one refresh to completion and return the text the monitor settled on."""
    monitor = tray.G733Tray(command, 3600)
    monitor.refresh()
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    while monitor.in_flight and time.monotonic() < deadline:
        APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
    monitor.poll_timer.stop()
    if monitor.in_flight:
        raise AssertionError(f"request did not finish within {REQUEST_TIMEOUT_SECONDS}s")
    return monitor.status_action.text()


def fill_colour(**kwargs) -> str:
    image = tray.icon_for(**kwargs).pixmap(QSize(64, 64)).toImage()
    return image.pixelColor(*FILL_SAMPLE).name()


class StubCommandMixin:
    """Creates executable stubs that print a fixed payload, like HeadsetControl."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def stub(self, payload: str, name: str = "headsetcontrol") -> str:
        path = Path(self._tmp.name) / name
        path.write_text(f"#!/usr/bin/env bash\ncat <<'JSON'\n{payload}\nJSON\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def read_payload(self, payload: str) -> str:
        return read_once(self.stub(payload))


class BatteryStateTests(StubCommandMixin, unittest.TestCase):
    def test_discharging_shows_level_and_remaining_time(self) -> None:
        text = self.read_payload(
            device_json('{"status":"BATTERY_AVAILABLE","level":39,"time_to_empty_min":351}')
        )
        self.assertEqual(text, "G733 battery: 39% · about 5h 51m left")

    def test_charging_with_level_is_not_an_error(self) -> None:
        # Regression: BATTERY_CHARGING used to render as "battery unavailable".
        text = self.read_payload(
            device_json('{"status":"BATTERY_CHARGING","level":62,"time_to_full_min":95}')
        )
        self.assertEqual(text, "G733 battery: charging 62% · about 1h 35m until full")

    def test_charging_without_a_usable_level(self) -> None:
        text = self.read_payload(device_json('{"status":"BATTERY_CHARGING","level":-1}'))
        self.assertEqual(text, "G733 battery: charging")

    def test_unknown_remaining_time_is_omitted(self) -> None:
        # Regression: -1 minutes used to render as "about -1h 59m left".
        text = self.read_payload(
            device_json('{"status":"BATTERY_AVAILABLE","level":44,"time_to_empty_min":-1}')
        )
        self.assertEqual(text, "G733 battery: 44%")

    def test_headset_off_reports_the_status(self) -> None:
        text = self.read_payload(device_json('{"status":"BATTERY_UNAVAILABLE","level":-1}'))
        self.assertIn("BATTERY_UNAVAILABLE", text)
        self.assertTrue(text.startswith("G733 battery unavailable:"))

    def test_no_devices_is_reported_not_raised(self) -> None:
        text = self.read_payload('{"device_count":0,"devices":[]}')
        self.assertTrue(text.startswith("G733 battery unavailable:"))

    def test_unparseable_output_is_reported(self) -> None:
        text = self.read_payload("not json at all")
        self.assertTrue(text.startswith("G733 battery unavailable:"))


class CommandResolutionTests(StubCommandMixin, unittest.TestCase):
    PAYLOAD = device_json('{"status":"BATTERY_AVAILABLE","level":77,"time_to_empty_min":420}')

    def test_absolute_path(self) -> None:
        self.assertEqual(
            read_once(self.stub(self.PAYLOAD)), "G733 battery: 77% · about 7h 0m left"
        )

    def test_bare_name_is_looked_up_on_path(self) -> None:
        self.stub(self.PAYLOAD)
        with mock.patch.dict(os.environ, {"PATH": self._tmp.name + os.pathsep + os.environ["PATH"]}):
            text = read_once("headsetcontrol")
        self.assertEqual(text, "G733 battery: 77% · about 7h 0m left")

    def test_missing_command_is_reported(self) -> None:
        text = read_once("definitely-not-a-real-command")
        self.assertIn("not found or not executable", text)


class NotificationTests(StubCommandMixin, unittest.TestCase):
    def monitor(self) -> tray.G733Tray:
        monitor = tray.G733Tray(self.stub(device_json("{}")), 3600)
        self.addCleanup(monitor.poll_timer.stop)
        return monitor

    def test_low_battery_latches_once_then_resets_when_recharged(self) -> None:
        monitor = self.monitor()
        monitor.show_level(15, {}, is_charging=False)
        self.assertTrue(monitor.low_battery_notified)
        monitor.show_level(30, {}, is_charging=False)
        self.assertFalse(monitor.low_battery_notified)

    def test_charging_suppresses_the_low_battery_warning(self) -> None:
        monitor = self.monitor()
        monitor.show_level(15, {}, is_charging=True)
        self.assertFalse(monitor.low_battery_notified)


class ErrorLoggingTests(StubCommandMixin, unittest.TestCase):
    """Errors must reach stderr; an autostarted monitor has no other channel."""

    def monitor(self) -> tray.G733Tray:
        monitor = tray.G733Tray(self.stub(device_json("{}")), 3600)
        self.addCleanup(monitor.poll_timer.stop)
        return monitor

    def test_repeated_errors_are_logged_once(self) -> None:
        monitor = self.monitor()
        with self.assertLogs(tray.LOGGER, level="ERROR") as logged:
            monitor.show_error("receiver is unplugged")
            monitor.show_error("receiver is unplugged")
        self.assertEqual(len(logged.records), 1)

    def test_a_different_error_is_logged_again(self) -> None:
        monitor = self.monitor()
        with self.assertLogs(tray.LOGGER, level="ERROR") as logged:
            monitor.show_error("receiver is unplugged")
            monitor.show_error("request timed out")
        self.assertEqual(len(logged.records), 2)

    def test_recovery_is_logged(self) -> None:
        monitor = self.monitor()
        monitor.show_error("receiver is unplugged")
        with self.assertLogs(tray.LOGGER, level="INFO") as logged:
            monitor.show_level(80, {}, is_charging=False)
        self.assertIn("recovered", logged.output[0])


class IconTests(unittest.TestCase):
    def test_state_colours(self) -> None:
        self.assertEqual(fill_colour(level=90), "#27ae60")
        self.assertEqual(fill_colour(level=40), "#f39c12")
        self.assertEqual(fill_colour(level=8), "#e74c3c")
        self.assertEqual(fill_colour(level=None, is_error=True), "#7f8c8d")

    def test_charging_is_blue_whether_or_not_a_level_is_known(self) -> None:
        self.assertEqual(fill_colour(level=62, is_charging=True), "#2980b9")
        self.assertEqual(fill_colour(level=None, is_charging=True), "#2980b9")

    def test_charging_colour_wins_over_the_low_battery_colour(self) -> None:
        self.assertEqual(fill_colour(level=5, is_charging=True), "#2980b9")


class FormatDurationTests(unittest.TestCase):
    def test_renders_hours_and_minutes(self) -> None:
        self.assertEqual(tray.format_duration(351, "left"), " · about 5h 51m left")

    def test_unknown_values_render_as_nothing(self) -> None:
        for value in (-1, 0, None, "351", 351.0):
            with self.subTest(value=value):
                self.assertEqual(tray.format_duration(value, "left"), "")


class ArgumentTests(unittest.TestCase):
    def parse(self, argv: list[str], **env: str) -> tuple[str, int]:
        """Parse one command line, capturing whatever it printed as self.output."""
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with mock.patch.object(sys, "argv", ["g733_battery_tray.py", *argv]), \
                 mock.patch.dict(os.environ, env, clear=False), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                return tray.parse_arguments()
        finally:
            self.output = stdout.getvalue() + stderr.getvalue()

    def test_defaults(self) -> None:
        command, interval = self.parse([], HEADSETCONTROL="/bin/true", POLL_SECONDS="60")
        self.assertEqual((command, interval), ("/bin/true", 60))

    def test_options_override_the_environment(self) -> None:
        command, interval = self.parse(
            ["--command", "/bin/false", "--interval", "120"],
            HEADSETCONTROL="/bin/true",
            POLL_SECONDS="60",
        )
        self.assertEqual((command, interval), ("/bin/false", 120))

    def test_help_works_even_with_a_bad_environment(self) -> None:
        # Regression: the environment used to be parsed before --help.
        with self.assertRaises(SystemExit) as raised:
            self.parse(["--help"], POLL_SECONDS="abc")
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("Usage:", self.output)

    def test_rejected_inputs_exit_with_status_two(self) -> None:
        cases = {
            "non-numeric POLL_SECONDS": ([], {"POLL_SECONDS": "abc"}),
            "non-numeric --interval": (["--interval", "xyz"], {}),
            "--interval without a value": (["--interval"], {}),
            "interval below the minimum": (["--interval", "2"], {}),
            "unknown argument": (["--bogus"], {}),
            "empty command": (["--command", ""], {}),
        }
        for name, (argv, env) in cases.items():
            with self.subTest(name):
                with self.assertRaises(SystemExit) as raised:
                    self.parse(argv, **env)
                self.assertEqual(raised.exception.code, 2)
                # Rejections must say what was wrong, not just exit.
                self.assertTrue(self.output.strip(), "exited without explaining why")


if __name__ == "__main__":
    unittest.main(verbosity=2)
