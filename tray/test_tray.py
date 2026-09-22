#!/usr/bin/env python3
"""Tests for the G733 tray monitor.

Run with `python3 test_tray.py` or `python3 -m unittest`. Only the standard
library and PyQt6 are needed; there is no test-runner dependency.

The battery cases drive the real G733Tray through a real QProcess against stub
executables, because the states that matter here are ones HeadsetControl
reports and the monitor has to interpret, not ones a mock would prove.
"""

from __future__ import annotations

import atexit
import contextlib
import io
import json
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

from PyQt6.QtCore import QEventLoop, QRect, QSize
from PyQt6.QtWidgets import QApplication

import g733_battery_tray as tray

APP = QApplication.instance() or QApplication([])

# Every monitor reads the remembered lighting state when it is constructed, so
# the whole suite is pointed at a throwaway directory instead of the real one.
_CONFIG_HOME = tempfile.TemporaryDirectory()
atexit.register(_CONFIG_HOME.cleanup)
os.environ["XDG_CONFIG_HOME"] = _CONFIG_HOME.name

# Keep the monitor's own error logging out of the test report; the tests that
# care about it capture it explicitly with assertLogs.
tray.LOGGER.addHandler(logging.NullHandler())
tray.LOGGER.propagate = False

# A point inside the icon's rounded rectangle, clear of the border, the label
# and the charging bolt, so it samples the state colour in every state.
FILL_SAMPLE = (32, 10)
REQUEST_TIMEOUT_SECONDS = 15


def device_json(battery: dict[str, object]) -> str:
    """Wrap one battery object in the envelope HeadsetControl prints."""
    return json.dumps({"device_count": 1, "devices": [{"status": "success", "battery": battery}]})


def read_text(path: Path) -> str:
    """Return a stub's recorded arguments, which are absent until it first runs."""
    return path.read_text() if path.exists() else ""


def read_once(command: str) -> str:
    """Run one refresh to completion and return the text the monitor settled on."""
    monitor = tray.G733Tray((command,), 3600)
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
            device_json({"status": "BATTERY_AVAILABLE", "level": 39, "time_to_empty_min": 351})
        )
        self.assertEqual(text, "G733 battery: 39% · about 5h 51m left")

    def test_charging_with_level_is_not_an_error(self) -> None:
        # Regression: BATTERY_CHARGING used to render as "battery unavailable".
        text = self.read_payload(
            device_json({"status": "BATTERY_CHARGING", "level": 62, "time_to_full_min": 95})
        )
        self.assertEqual(text, "G733 battery: charging 62% · about 1h 35m until full")

    def test_charging_without_a_usable_level(self) -> None:
        text = self.read_payload(device_json({"status": "BATTERY_CHARGING", "level": -1}))
        self.assertEqual(text, "G733 battery: charging")

    def test_unknown_remaining_time_is_omitted(self) -> None:
        # Regression: -1 minutes used to render as "about -1h 59m left".
        text = self.read_payload(
            device_json({"status": "BATTERY_AVAILABLE", "level": 44, "time_to_empty_min": -1})
        )
        self.assertEqual(text, "G733 battery: 44%")

    def test_headset_off_reports_the_status(self) -> None:
        text = self.read_payload(device_json({"status": "BATTERY_UNAVAILABLE", "level": -1}))
        self.assertIn("BATTERY_UNAVAILABLE", text)
        self.assertTrue(text.startswith("G733 battery unavailable:"))

    def test_no_devices_is_reported_not_raised(self) -> None:
        text = self.read_payload('{"device_count":0,"devices":[]}')
        self.assertTrue(text.startswith("G733 battery unavailable:"))

    def test_unparseable_output_is_reported(self) -> None:
        text = self.read_payload("not json at all")
        self.assertTrue(text.startswith("G733 battery unavailable:"))


class CommandResolutionTests(StubCommandMixin, unittest.TestCase):
    PAYLOAD = device_json({"status": "BATTERY_AVAILABLE", "level": 77, "time_to_empty_min": 420})

    def test_absolute_path(self) -> None:
        self.assertEqual(read_once(self.stub(self.PAYLOAD)), "G733 battery: 77% · about 7h 0m left")

    def test_bare_name_is_looked_up_on_path(self) -> None:
        self.stub(self.PAYLOAD)
        with mock.patch.dict(
            os.environ, {"PATH": self._tmp.name + os.pathsep + os.environ["PATH"]}
        ):
            text = read_once("headsetcontrol")
        self.assertEqual(text, "G733 battery: 77% · about 7h 0m left")

    def test_missing_command_is_reported(self) -> None:
        text = read_once("definitely-not-a-real-command")
        self.assertIn("not found or not executable", text)

    def test_falls_back_to_the_next_candidate(self) -> None:
        packaged = self.stub(self.PAYLOAD)
        monitor = tray.G733Tray(("/nonexistent/headsetcontrol.AppImage", packaged), 3600)
        self.addCleanup(monitor.poll_timer.stop)
        monitor.refresh()
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        while monitor.in_flight and time.monotonic() < deadline:
            APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        self.assertEqual(monitor.status_action.text(), "G733 battery: 77% · about 7h 0m left")

    def test_the_first_usable_candidate_wins(self) -> None:
        preferred = self.stub(self.PAYLOAD, name="preferred")
        other = self.stub(device_json({"status": "BATTERY_AVAILABLE", "level": 5}), name="other")
        self.assertEqual(tray.resolve_command((preferred, other)), preferred)

    def test_error_suggests_the_package(self) -> None:
        monitor = tray.G733Tray(("/nope/one",), 3600)
        self.addCleanup(monitor.poll_timer.stop)
        monitor.refresh()
        self.assertIn("headsetcontrol package", monitor.status_action.text())

    def test_error_names_every_candidate_tried(self) -> None:
        monitor = tray.G733Tray(("/nope/one", "/nope/two"), 3600)
        self.addCleanup(monitor.poll_timer.stop)
        monitor.refresh()
        self.assertIn("/nope/one or /nope/two", monitor.status_action.text())


class LightsTests(StubCommandMixin, unittest.TestCase):
    """The lights action drives a real command, like the battery cases do."""

    def recording_stub(self, exit_code: int = 0, message: str = "") -> tuple[str, Path]:
        """Return a stub that records its arguments, plus the file it writes."""
        record = Path(self._tmp.name) / "arguments"
        path = Path(self._tmp.name) / "headsetcontrol-lights"
        path.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" > {record}\n"
            f"[[ -n {message!r} ]] && echo {message!r} >&2\n"
            f"exit {exit_code}\n"
        )
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path), record

    def monitor(self, command: str) -> tray.G733Tray:
        monitor = tray.G733Tray((command,), 3600)
        self.addCleanup(monitor.poll_timer.stop)
        self.addCleanup(monitor.lights_timeout.stop)
        return monitor

    def setUp(self) -> None:
        super().setUp()
        # A config directory per test, so a remembered state never leaks.
        config = tempfile.TemporaryDirectory()
        self.addCleanup(config.cleanup)
        patched = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config.name})
        patched.start()
        self.addCleanup(patched.stop)
        self.state_file = tray.state_file()

    def run_lights(self, monitor: tray.G733Tray, on: bool = False) -> None:
        monitor.set_lights(on)
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        while (
            monitor.lights_process.state() != tray.QProcess.ProcessState.NotRunning
            and time.monotonic() < deadline
        ):
            APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)

    def test_each_item_runs_headsetcontrol_with_its_own_flag(self) -> None:
        for on, expected in ((False, "-l 0"), (True, "-l 1")):
            with self.subTest(on=on):
                command, record = self.recording_stub()
                monitor = self.monitor(command)
                self.run_lights(monitor, on=on)
                self.assertEqual(record.read_text().strip(), expected)

    def test_both_items_are_usable_again_afterwards(self) -> None:
        command, _ = self.recording_stub()
        monitor = self.monitor(command)
        self.run_lights(monitor, on=True)
        for on, label in ((True, "Turn lights on"), (False, "Turn lights off")):
            with self.subTest(on=on):
                self.assertTrue(monitor.lights_actions[on].isEnabled())
                self.assertEqual(monitor.lights_actions[on].text(), label)

    def test_the_other_item_is_disabled_while_a_request_runs(self) -> None:
        # One process serves both items, so neither may start a second request.
        command, _ = self.recording_stub()
        monitor = self.monitor(command)
        monitor.set_lights(True)
        self.assertFalse(monitor.lights_actions[False].isEnabled())
        self.assertEqual(monitor.lights_actions[True].text(), "Turning lights on…")
        self.run_lights(monitor, on=True)

    def test_success_is_logged_with_the_state_it_set(self) -> None:
        command, _ = self.recording_stub()
        monitor = self.monitor(command)
        with self.assertLogs(tray.LOGGER, level="INFO") as logged:
            self.run_lights(monitor, on=True)
        self.assertIn("lights turned on", logged.output[0])

    def test_a_failing_command_is_logged_with_its_output_and_state(self) -> None:
        command, _ = self.recording_stub(exit_code=1, message="headset is off")
        monitor = self.monitor(command)
        with self.assertLogs(tray.LOGGER, level="ERROR") as logged:
            self.run_lights(monitor, on=True)
        self.assertIn("headset is off", logged.output[0])
        self.assertIn("lights on", logged.output[0])

    def test_a_missing_command_is_logged(self) -> None:
        monitor = self.monitor("definitely-not-a-real-command")
        with self.assertLogs(tray.LOGGER, level="ERROR") as logged:
            monitor.set_lights(False)
        self.assertIn("not found or not executable", logged.output[0])

    def test_a_failure_leaves_the_battery_reading_alone(self) -> None:
        # The icon and tooltip report the battery; a lights fault must not
        # overwrite them with a "?" that claims the reading was lost.
        command, _ = self.recording_stub(exit_code=1, message="headset is off")
        monitor = self.monitor(command)
        monitor.show_level(80, {}, is_charging=False)
        with self.assertLogs(tray.LOGGER, level="ERROR"):
            self.run_lights(monitor)
        self.assertEqual(monitor.status_action.text(), "G733 battery: 80%")


class LightsMemoryTests(StubCommandMixin, unittest.TestCase):
    """The chosen state is stored, shown in the menu, and applied at startup."""

    def setUp(self) -> None:
        super().setUp()
        config = tempfile.TemporaryDirectory()
        self.addCleanup(config.cleanup)
        patched = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config.name})
        patched.start()
        self.addCleanup(patched.stop)
        self.state_file = tray.state_file()

    def recording_stub(self, exit_code: int = 0) -> tuple[str, Path]:
        record = Path(self._tmp.name) / "arguments"
        path = Path(self._tmp.name) / "headsetcontrol-lights"
        path.write_text(
            f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {record}\nexit {exit_code}\n"
        )
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path), record

    def monitor(self, command: str) -> tray.G733Tray:
        monitor = tray.G733Tray((command,), 3600)
        self.addCleanup(monitor.poll_timer.stop)
        self.addCleanup(monitor.lights_timeout.stop)
        return monitor

    def settle(self, monitor: tray.G733Tray) -> None:
        """Run the event loop until no lights or battery request is left."""
        self.wait_for(lambda: not self.busy(monitor), "requests did not finish in time")

    def busy(self, monitor: tray.G733Tray) -> bool:
        return (
            monitor.in_flight
            or monitor.restoring_lights
            or monitor.lights_process.state() != tray.QProcess.ProcessState.NotRunning
        )

    def wait_for(self, done, message: str) -> None:
        """Run the event loop until a condition holds, or fail saying what did not."""
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            if done():
                return
        raise AssertionError(message)

    def run_startup(self, monitor: tray.G733Tray, record: Path) -> str:
        """Start the monitor and wait for the first battery reading to complete."""
        # Shortened so the suite does not wait out the real settle window.
        with mock.patch.object(tray, "LIGHTS_SETTLE_MS", 50):
            monitor.start()
            self.wait_for(
                lambda: not self.busy(monitor) and "-b -o json" in read_text(record),
                f"no battery reading was taken; ran: {read_text(record)!r}",
            )
        return read_text(record)

    def test_a_successful_choice_is_stored(self) -> None:
        command, _ = self.recording_stub()
        monitor = self.monitor(command)
        monitor.set_lights(True)
        self.settle(monitor)
        self.assertEqual(json.loads(self.state_file.read_text()), {"lights": True})
        monitor.set_lights(False)
        self.settle(monitor)
        self.assertEqual(json.loads(self.state_file.read_text()), {"lights": False})

    def test_a_failed_choice_is_not_stored(self) -> None:
        # Remembering a state the headset never took would re-apply a lie.
        command, _ = self.recording_stub(exit_code=1)
        monitor = self.monitor(command)
        with self.assertLogs(tray.LOGGER, level="ERROR"):
            monitor.set_lights(True)
            self.settle(monitor)
        self.assertFalse(self.state_file.exists())

    def test_the_menu_ticks_the_remembered_state(self) -> None:
        command, _ = self.recording_stub()
        monitor = self.monitor(command)
        self.assertEqual([a.isChecked() for a in monitor.lights_actions.values()], [False, False])
        monitor.set_lights(False)
        self.settle(monitor)
        self.assertFalse(monitor.lights_actions[True].isChecked())
        self.assertTrue(monitor.lights_actions[False].isChecked())

    def test_startup_applies_the_remembered_state_before_the_first_reading(self) -> None:
        tray.save_lights_preference(False)
        command, record = self.recording_stub()
        monitor = self.monitor(command)
        ran = self.run_startup(monitor, record)
        # The lights request runs first; the battery reading follows it rather
        # than competing with it for the HID device, which the headset answers
        # with BATTERY_UNAVAILABLE for about two seconds afterwards.
        self.assertEqual(ran.split("\n")[:2], ["-l 0", "-b -o json"])

    def test_startup_without_a_remembered_state_only_reads_the_battery(self) -> None:
        command, record = self.recording_stub()
        monitor = self.monitor(command)
        monitor.start()
        self.settle(monitor)
        self.assertEqual(read_text(record).strip(), "-b -o json")

    def test_a_lights_write_holds_the_next_reading_back_then_brings_it_forward(self) -> None:
        command, _ = self.recording_stub()
        monitor = tray.G733Tray((command,), 3600)
        self.addCleanup(monitor.poll_timer.stop)
        self.addCleanup(monitor.lights_timeout.stop)
        monitor.schedule_next_poll()
        monitor.set_lights(True)
        self.settle(monitor)
        # Sooner than the hour-long interval it was counting down, but not
        # immediately: the headset reports no battery just after the write.
        remaining = monitor.poll_timer.remainingTime()
        self.assertGreater(remaining, 0)
        # Not exactly the window: Qt's coarse timers round a deadline up.
        self.assertLess(remaining, tray.LIGHTS_SETTLE_MS * 1.5)
        self.assertLess(remaining, monitor.interval_ms)

    def test_a_failed_restore_is_logged_but_not_notified(self) -> None:
        # The headset is commonly off when an autostarted monitor begins; that
        # must not open a notification the user did nothing to cause.
        tray.save_lights_preference(True)
        command, _ = self.recording_stub(exit_code=1)
        monitor = self.monitor(command)
        with (
            mock.patch.object(monitor.tray, "showMessage") as notified,
            self.assertLogs(tray.LOGGER, level="ERROR") as logged,
        ):
            monitor.start()
            self.settle(monitor)
        self.assertIn("lights on", logged.output[0])
        notified.assert_not_called()

    def test_a_failed_restore_still_reads_the_battery(self) -> None:
        tray.save_lights_preference(True)
        command, record = self.recording_stub(exit_code=1)
        monitor = self.monitor(command)
        with self.assertLogs(tray.LOGGER, level="ERROR"):
            self.assertIn("-b -o json", self.run_startup(monitor, record))

    def test_a_restore_with_no_command_still_reads_the_battery(self) -> None:
        # Nothing runs, so the poll must still be scheduled rather than lost.
        tray.save_lights_preference(True)
        monitor = tray.G733Tray(("definitely-not-a-real-command",), 3600)
        self.addCleanup(monitor.poll_timer.stop)
        with self.assertLogs(tray.LOGGER, level="ERROR") as logged:
            monitor.start()
            self.settle(monitor)
        self.assertTrue(any("not found or not executable" in line for line in logged.output))
        self.assertTrue(monitor.poll_timer.isActive())

    def test_an_absent_state_file_is_not_an_error(self) -> None:
        self.assertIsNone(tray.load_lights_preference())

    def test_an_unreadable_state_file_is_logged_and_ignored(self) -> None:
        cases = {"invalid json": "{not json", "wrong type": '{"lights": "off"}', "no key": "{}"}
        for name, contents in cases.items():
            with self.subTest(name):
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(contents)
                with self.assertLogs(tray.LOGGER, level="ERROR"):
                    self.assertIsNone(tray.load_lights_preference())

    def test_an_unwritable_location_is_logged_not_raised(self) -> None:
        with (
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/proc/nonexistent"}),
            self.assertLogs(tray.LOGGER, level="ERROR") as logged,
        ):
            tray.save_lights_preference(True)
        self.assertIn("could not save", logged.output[0])


class NotificationTests(StubCommandMixin, unittest.TestCase):
    def monitor(self) -> tray.G733Tray:
        monitor = tray.G733Tray((self.stub(device_json({})),), 3600)
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

    def test_a_full_charge_is_announced_once(self) -> None:
        monitor = self.monitor()
        with mock.patch.object(monitor.tray, "showMessage") as notified:
            monitor.show_level(100, {}, is_charging=True)
            monitor.show_level(100, {}, is_charging=True)
        notified.assert_called_once()
        self.assertIn("fully charged", notified.call_args.args[0])

    def test_a_full_battery_that_is_not_charging_is_not_announced(self) -> None:
        # A headset that simply reads 100% was not just charged to it.
        monitor = self.monitor()
        with mock.patch.object(monitor.tray, "showMessage") as notified:
            monitor.show_level(100, {}, is_charging=False)
        notified.assert_not_called()

    def test_coming_off_the_cable_re_arms_the_announcement(self) -> None:
        monitor = self.monitor()
        monitor.show_level(100, {}, is_charging=True)
        monitor.show_level(100, {}, is_charging=False)
        self.assertFalse(monitor.full_battery_notified)
        with mock.patch.object(monitor.tray, "showMessage") as notified:
            monitor.show_level(100, {}, is_charging=True)
        notified.assert_called_once()

    def test_a_drop_below_full_re_arms_the_announcement(self) -> None:
        monitor = self.monitor()
        monitor.show_level(100, {}, is_charging=True)
        monitor.show_level(94, {}, is_charging=True)
        self.assertFalse(monitor.full_battery_notified)

    def test_staying_just_below_full_keeps_the_announcement_latched(self) -> None:
        # A reading that wobbles between 100 and 99 must not announce twice.
        monitor = self.monitor()
        monitor.show_level(100, {}, is_charging=True)
        with mock.patch.object(monitor.tray, "showMessage") as notified:
            monitor.show_level(99, {}, is_charging=True)
            monitor.show_level(100, {}, is_charging=True)
        notified.assert_not_called()

    def test_charging_without_a_level_is_not_treated_as_full(self) -> None:
        monitor = self.monitor()
        with mock.patch.object(monitor.tray, "showMessage") as notified:
            monitor.show_level(None, {}, is_charging=True)
        notified.assert_not_called()
        self.assertFalse(monitor.full_battery_notified)


class ErrorLoggingTests(StubCommandMixin, unittest.TestCase):
    """Errors must reach stderr; an autostarted monitor has no other channel."""

    def monitor(self) -> tray.G733Tray:
        monitor = tray.G733Tray((self.stub(device_json({})),), 3600)
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


class ChargingIndicatorTests(unittest.TestCase):
    """Charging must be readable without comparing one colour against another."""

    def white_pixels(self, area: QRect, **kwargs) -> int:
        image = tray.icon_for(**kwargs).pixmap(QSize(64, 64)).toImage()
        return sum(
            image.pixelColor(x, y).name() == "#ffffff"
            for x in range(area.left(), area.right() + 1)
            for y in range(area.top(), area.bottom() + 1)
        )

    # The right-hand strip the bolt badge reaches into and no label does.
    BADGE_STRIP = QRect(50, 18, 9, 32)
    # The middle of the icon, where the full-size bolt is drawn instead.
    CENTRE = QRect(26, 20, 12, 24)

    def test_a_charging_icon_with_a_level_still_draws_the_bolt(self) -> None:
        for level in (5, 62, 100):
            with self.subTest(level=level):
                self.assertGreater(
                    self.white_pixels(self.BADGE_STRIP, level=level, is_charging=True), 0
                )

    def test_a_charging_icon_without_a_level_draws_the_full_size_bolt(self) -> None:
        self.assertGreater(self.white_pixels(self.CENTRE, level=None, is_charging=True), 0)

    def test_no_other_state_draws_a_bolt(self) -> None:
        cases = {
            "discharging": {"level": 62},
            "three digits": {"level": 100},
            "low": {"level": 5},
            "error": {"level": None, "is_error": True},
            "waiting": {"level": None},
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                self.assertEqual(self.white_pixels(self.BADGE_STRIP, **kwargs), 0)

    def test_an_error_icon_differs_from_a_waiting_icon(self) -> None:
        # "!" reports a fault; "?" only that no reading has completed yet.
        waiting = tray.icon_for(None).pixmap(QSize(64, 64)).toImage()
        error = tray.icon_for(None, is_error=True).pixmap(QSize(64, 64)).toImage()
        self.assertNotEqual(waiting, error)


class FaultReportingTests(StubCommandMixin, unittest.TestCase):
    """A fault has to stay explained until it is actually over."""

    def monitor(self, command: str) -> tray.G733Tray:
        monitor = tray.G733Tray((command,), 3600)
        self.addCleanup(monitor.poll_timer.stop)
        self.addCleanup(monitor.lights_timeout.stop)
        return monitor

    def test_a_battery_error_is_shown_and_cleared_on_recovery(self) -> None:
        monitor = self.monitor(self.stub(device_json({})))
        monitor.show_error("receiver is unplugged")
        self.assertEqual(monitor.tray.toolTip(), "receiver is unplugged")
        monitor.show_level(80, {}, is_charging=False)
        self.assertEqual(monitor.tray.toolTip(), "G733 battery: 80%")

    def test_a_lights_fault_joins_the_battery_line_until_it_is_resolved(self) -> None:
        monitor = self.monitor(self.stub(device_json({})))
        monitor.show_level(80, {}, is_charging=False)
        with self.assertLogs(tray.LOGGER, level="ERROR"):
            monitor.report_lights_failure("Could not turn the G733 lights on: headset is off")
        # The icon keeps reporting the battery, which is still known; the
        # explanation lives in the tooltip and the menu.
        self.assertIn("G733 battery: 80%", monitor.tray.toolTip())
        self.assertIn("headset is off", monitor.tray.toolTip())
        self.assertTrue(monitor.lights_status_action.isVisible())

        monitor.lights_fault = None
        monitor.apply_status()
        self.assertEqual(monitor.tray.toolTip(), "G733 battery: 80%")
        self.assertFalse(monitor.lights_status_action.isVisible())

    def test_a_successful_lights_request_clears_an_earlier_fault(self) -> None:
        record = Path(self._tmp.name) / "arguments"
        working = Path(self._tmp.name) / "works"
        working.write_text(f"#!/usr/bin/env bash\nprintf '%s' \"$*\" > {record}\nexit 0\n")
        working.chmod(working.stat().st_mode | stat.S_IEXEC)
        monitor = self.monitor(str(working))
        with self.assertLogs(tray.LOGGER, level="ERROR"):
            monitor.report_lights_failure("Could not turn the G733 lights on: headset is off")
        monitor.set_lights(True)
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        while (
            monitor.lights_process.state() != tray.QProcess.ProcessState.NotRunning
            and time.monotonic() < deadline
        ):
            APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        self.assertIsNone(monitor.lights_fault)
        self.assertFalse(monitor.lights_status_action.isVisible())


class ShutdownTests(StubCommandMixin, unittest.TestCase):
    def test_quitting_mid_request_reports_nothing(self) -> None:
        # Killing a request on the way out is not a fault to warn about.
        slow = Path(self._tmp.name) / "slow"
        slow.write_text("#!/usr/bin/env bash\nsleep 30\n")
        slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
        monitor = tray.G733Tray((str(slow),), 3600)
        self.addCleanup(monitor.poll_timer.stop)
        monitor.refresh()
        monitor.set_lights(True)
        with mock.patch.object(tray.LOGGER, "error") as logged:
            monitor.quit()
            APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        logged.assert_not_called()


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
            with (
                mock.patch.object(sys, "argv", ["g733_battery_tray.py", *argv]),
                mock.patch.dict(os.environ, env, clear=False),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                return tray.parse_arguments()
        finally:
            self.output = stdout.getvalue() + stderr.getvalue()

    def test_defaults(self) -> None:
        commands, interval = self.parse([], HEADSETCONTROL="/bin/true", POLL_SECONDS="60")
        self.assertEqual((commands, interval), (("/bin/true",), 60))

    def test_options_override_the_environment(self) -> None:
        commands, interval = self.parse(
            ["--command", "/bin/false", "--interval", "120"],
            HEADSETCONTROL="/bin/true",
            POLL_SECONDS="60",
        )
        self.assertEqual((commands, interval), (("/bin/false",), 120))

    def test_no_choice_falls_back_from_the_appimage_to_the_package(self) -> None:
        commands, _ = self.parse([], HEADSETCONTROL="")
        self.assertEqual(commands, (str(tray.DEFAULT_APPIMAGE), "headsetcontrol"))

    def test_an_explicit_choice_does_not_fall_back(self) -> None:
        # Falling back here would silently run a different binary than asked for.
        commands, _ = self.parse(["--command", "/bin/true"])
        self.assertEqual(commands, ("/bin/true",))

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
