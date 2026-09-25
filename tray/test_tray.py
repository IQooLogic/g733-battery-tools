#!/usr/bin/env python3
"""Tests for the G733 tray monitor.

Run with `python3 test_tray.py` or `python3 -m unittest`. Only the standard
library and PyQt6 are needed; there is no test-runner dependency.

The battery and lights cases drive the real G733Tray through a real QProcess
against a stub headset tool, because the states that matter here are ones the
tool reports and the monitor has to interpret, not ones a mock would prove.
test_headset.py covers the headset tool itself.
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
from typing import ClassVar
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


def reading_json(state: str, voltage_mv: int) -> str:
    """Return one reading in the form `g733_headset.py battery` prints."""
    return json.dumps({"voltage_mv": voltage_mv, "flags": 0, "state": state})


def voltage_for(percent: int) -> int:
    """Return a voltage on the discharge curve that estimates to one percentage."""
    for voltage_mv, curve_percent in tray.DISCHARGE_CURVE:
        if curve_percent == percent:
            return voltage_mv
    raise AssertionError(f"{percent}% is not a point on the curve")


def read_text(path: Path) -> str:
    """Return a stub's recorded arguments, which are absent until it first runs."""
    return path.read_text() if path.exists() else ""


def read_once(headset: str) -> str:
    """Run one refresh to completion and return the text the monitor settled on."""
    monitor = tray.G733Tray(3600, headset=(headset,), idle_aware=False)
    monitor.refresh()
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    while monitor.in_flight and time.monotonic() < deadline:
        APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
    monitor.poll_timer.stop()
    if monitor.in_flight:
        raise AssertionError(f"request did not finish within {REQUEST_TIMEOUT_SECONDS}s")
    return monitor.status_action.text()


def monitor_menu(monitor: tray.G733Tray) -> list:
    """Return the actions of the tray's context menu, separators left out."""
    return [action for action in monitor.menu.actions() if not action.isSeparator()]


def fill_colour(**kwargs) -> str:
    image = tray.icon_for(**kwargs).pixmap(QSize(64, 64)).toImage()
    return image.pixelColor(*FILL_SAMPLE).name()


class StubCommandMixin:
    """Creates stub headset tools that record their arguments and answer each command."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # One record per test, appended to by every stub it creates.
        self.record = Path(self._tmp.name) / "arguments"

    def tool(
        self,
        battery: str = reading_json("discharging", 3989),
        battery_exit: int = 0,
        battery_error: str = "",
        lights_exit: int = 0,
        lights_error: str = "",
        lights_state: str = "off",
        switched_on: Path | None = None,
    ) -> str:
        """Return a stub that prints `battery` for "battery" and exits for "lights".

        With `switched_on`, battery readings fail as a headset that is off does
        until that file exists.
        """
        path = Path(self._tmp.name) / "g733-headset"
        headset_off = (
            ""
            if switched_on is None
            else (
                f"  [[ -e {switched_on} ]] || {{ echo 'no answer' >&2; "
                f"exit {tray.HEADSET_UNAVAILABLE_EXIT}; }}\n"
            )
        )
        path.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> {self.record}\n"
            'if [[ "$1" == battery ]]; then\n'
            f"{headset_off}"
            f"  cat <<'JSON'\n{battery}\nJSON\n"
            f"  [[ -n {battery_error!r} ]] && echo {battery_error!r} >&2\n"
            f"  exit {battery_exit}\n"
            "fi\n"
            'if [[ "$2" == status ]]; then\n'
            f"  printf '%s\\n' '{json.dumps({'lights': lights_state})}'\n"
            "fi\n"
            f"[[ -n {lights_error!r} ]] && echo {lights_error!r} >&2\n"
            f"exit {lights_exit}\n"
        )
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def ran(self) -> list[str]:
        """Return the commands the stubs were run with, in order."""
        return read_text(self.record).splitlines()

    def read_payload(self, payload: str, exit_code: int = 0, error: str = "") -> str:
        return read_once(self.tool(battery=payload, battery_exit=exit_code, battery_error=error))


class BatteryStateTests(StubCommandMixin, unittest.TestCase):
    def test_discharging_shows_the_estimated_level_and_the_voltage(self) -> None:
        text = self.read_payload(reading_json("discharging", 3778))
        self.assertEqual(text, "G733 battery: 40% · 3778 mV")

    def test_charging_shows_no_percentage(self) -> None:
        # A charger holds the voltage up, so a percentage from it would be wrong:
        # this is how a 44% battery used to read 100% minutes after plugging in.
        text = self.read_payload(reading_json("charging", 4210))
        self.assertEqual(text, "G733 battery: charging")

    def test_a_finished_charge_is_reported(self) -> None:
        text = self.read_payload(reading_json("full", 4112))
        self.assertEqual(text, "G733 battery: fully charged")

    def test_a_failure_reports_its_message(self) -> None:
        text = self.read_payload("", exit_code=1, error="no answer from the headset within 2s")
        self.assertEqual(text, "G733 battery unavailable: no answer from the headset within 2s")

    def test_an_offline_headset_is_not_shown_as_an_error(self) -> None:
        text = self.read_payload(
            "", exit_code=tray.HEADSET_UNAVAILABLE_EXIT, error="no answer from the headset"
        )
        self.assertEqual(text, "G733 headset is off or out of range")

    def test_a_failure_without_a_message_reports_its_status(self) -> None:
        text = self.read_payload("", exit_code=4)
        self.assertEqual(text, "G733 battery unavailable: headset tool exited with 4")

    def test_unparseable_output_is_reported(self) -> None:
        cases = {
            "not json": "not json at all",
            "unknown state": reading_json("exploding", 3800),
            "no voltage": json.dumps({"state": "discharging"}),
            "zero voltage": reading_json("discharging", 0),
        }
        for name, payload in cases.items():
            with self.subTest(name):
                self.assertTrue(self.read_payload(payload).startswith("G733 battery unavailable:"))

    def test_the_battery_command_is_requested(self) -> None:
        self.read_payload(reading_json("discharging", 3989))
        self.assertEqual(self.ran(), ["battery"])

    def test_a_tool_that_cannot_start_is_reported(self) -> None:
        text = read_once("/nonexistent/g733-headset")
        self.assertTrue(text.startswith("Could not start the headset tool:"))


class EstimateTests(unittest.TestCase):
    def test_curve_points_estimate_to_their_own_percentage(self) -> None:
        for voltage_mv, percent in tray.DISCHARGE_CURVE:
            with self.subTest(voltage_mv=voltage_mv):
                self.assertEqual(tray.estimate_percent(voltage_mv), percent)

    def test_between_points_is_interpolated(self) -> None:
        # Halfway from 3811 mV (50%) to 3859 mV (60%).
        self.assertEqual(tray.estimate_percent(3835), 55)

    def test_the_ends_are_clamped(self) -> None:
        self.assertEqual(tray.estimate_percent(4400), 100)
        self.assertEqual(tray.estimate_percent(3000), 0)


class IdleAwarePollingTests(StubCommandMixin, unittest.TestCase):
    """Automatic HID requests stop while PipeWire has no G733 playback."""

    SINK: ClassVar[dict[str, object]] = {
        "id": 55,
        "type": "PipeWire:Interface:Node",
        "info": {
            "props": {
                "media.class": "Audio/Sink",
                "device.vendor.id": "0x046d",
                "device.product.id": "0x0b1f",
            }
        },
    }

    def setUp(self) -> None:
        super().setUp()
        config = tempfile.TemporaryDirectory()
        self.addCleanup(config.cleanup)
        patched = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config.name})
        patched.start()
        self.addCleanup(patched.stop)

    def pw_dump(self, snapshot: list[dict]) -> str:
        path = Path(self._tmp.name) / "pw-dump"
        path.write_text(f"#!/usr/bin/env python3\nprint({json.dumps(snapshot)!r})\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def monitor(self, snapshot: list[dict]) -> tray.G733Tray:
        command = self.pw_dump(snapshot)
        patched = mock.patch.object(tray, "PIPEWIRE_DUMP_COMMAND", command)
        patched.start()
        self.addCleanup(patched.stop)
        monitor = tray.G733Tray(3600, headset=(self.tool(),))
        self.addCleanup(monitor.poll_timer.stop)
        self.addCleanup(monitor.audio_timeout.stop)
        self.addCleanup(monitor.lights_timeout.stop)
        return monitor

    def wait_for(self, done, message: str) -> None:
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            if done():
                return
        raise AssertionError(message)

    def test_pipewire_snapshot_requires_an_active_link_to_the_g733_sink(self) -> None:
        active_link = {
            "type": "PipeWire:Interface:Link",
            "info": {"state": "active", "input-node-id": 55},
        }
        self.assertTrue(tray.g733_playback_is_active([self.SINK, active_link]))
        inactive_link = {
            "type": "PipeWire:Interface:Link",
            "info": {"state": "init", "input-node-id": 55},
        }
        self.assertFalse(tray.g733_playback_is_active([self.SINK, inactive_link]))

    def test_no_active_audio_pauses_later_automatic_hid_polling(self) -> None:
        monitor = self.monitor([self.SINK])
        monitor.start()
        # Startup always obtains one reading, so the icon does not remain on
        # "waiting for first reading" forever when no audio is playing.
        self.wait_for(lambda: "battery" in self.ran(), "initial battery request was not started")
        self.wait_for(lambda: not monitor.in_flight, "initial battery request did not finish")
        self.assertEqual(self.ran().count("battery"), 1)

        # The next ordinary automatic poll checks PipeWire and pauses without
        # another HID request while there is no G733 playback.
        monitor.refresh(manual=False)
        self.wait_for(lambda: monitor.polling_paused, "PipeWire activity check did not finish")
        self.assertEqual(self.ran().count("battery"), 1)
        self.assertIn("polling paused", monitor.tray.toolTip())
        self.assertGreater(monitor.poll_timer.remainingTime(), 0)
        self.assertLessEqual(monitor.poll_timer.remainingTime(), tray.IDLE_ACTIVITY_CHECK_MS * 1.5)

    def test_active_audio_starts_an_automatic_battery_request(self) -> None:
        link = {"type": "PipeWire:Interface:Link", "info": {"state": "active", "input-node-id": 55}}
        monitor = self.monitor([self.SINK, link])
        monitor.start()
        self.wait_for(lambda: "battery" in self.ran(), "battery request was not started")
        self.assertFalse(monitor.polling_paused)

    def test_manual_refresh_bypasses_the_paused_policy(self) -> None:
        monitor = self.monitor([self.SINK])
        monitor.start()
        self.wait_for(lambda: "battery" in self.ran(), "initial battery request was not started")
        self.wait_for(lambda: not monitor.in_flight, "initial battery request did not finish")
        monitor.refresh(manual=False)
        self.wait_for(lambda: monitor.polling_paused, "PipeWire activity check did not finish")
        monitor.refresh()
        self.wait_for(lambda: self.ran().count("battery") == 2, "manual refresh did not start")


class SmoothingTests(StubCommandMixin, unittest.TestCase):
    def monitor(self) -> tray.G733Tray:
        monitor = tray.G733Tray(3600, headset=(self.tool(),), idle_aware=False)
        self.addCleanup(monitor.poll_timer.stop)
        return monitor

    def test_one_swing_does_not_move_the_level(self) -> None:
        monitor = self.monitor()
        for voltage_mv in (3811, 3811, 3859, 3811):
            monitor.show_reading("discharging", voltage_mv)
        self.assertEqual(monitor.status_text, "G733 battery: 50% · 3811 mV")

    def test_a_lasting_change_shows_once_it_is_the_majority(self) -> None:
        monitor = self.monitor()
        for voltage_mv in (3859, 3859, 3811, 3811):
            monitor.show_reading("discharging", voltage_mv)
        self.assertEqual(monitor.status_text, "G733 battery: 55% · 3835 mV")
        monitor.show_reading("discharging", 3811)
        self.assertEqual(monitor.status_text, "G733 battery: 50% · 3811 mV")

    def test_only_the_last_readings_count(self) -> None:
        monitor = self.monitor()
        monitor.show_reading("discharging", 3500)
        for _ in range(tray.SMOOTHING_READINGS):
            monitor.show_reading("discharging", 3811)
        self.assertEqual(list(monitor.voltages), [3811] * tray.SMOOTHING_READINGS)

    def test_charging_and_errors_discard_the_history(self) -> None:
        # Voltages from before a charge, or before the headset was off, no
        # longer describe the battery.
        for interruption in ("charging", "full", "error"):
            with self.subTest(interruption):
                monitor = self.monitor()
                monitor.show_reading("discharging", 3500)
                if interruption == "error":
                    with self.assertLogs(tray.LOGGER, level="ERROR"):
                        monitor.show_error("headset is off")
                else:
                    monitor.show_reading(interruption, 4200)
                monitor.show_reading("discharging", 3989)
                self.assertEqual(monitor.status_text, "G733 battery: 80% · 3989 mV")


class LightsTests(StubCommandMixin, unittest.TestCase):
    """The lights action drives a real command, like the battery cases do."""

    def monitor(self, tool: str) -> tray.G733Tray:
        monitor = tray.G733Tray(3600, headset=(tool,), idle_aware=False)
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
        self.wait_for_lights(monitor)

    def wait_for_lights(self, monitor: tray.G733Tray) -> None:
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        while (
            monitor.lights_process.state() != tray.QProcess.ProcessState.NotRunning
            and time.monotonic() < deadline
        ):
            APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)

    def test_each_item_runs_the_lights_command_with_its_own_state(self) -> None:
        for on, expected in ((False, "lights off"), (True, "lights on")):
            with self.subTest(on=on):
                self.record.unlink(missing_ok=True)
                monitor = self.monitor(self.tool())
                self.run_lights(monitor, on=on)
                self.assertEqual(self.ran(), [expected])

    def test_the_menu_has_one_lights_item(self) -> None:
        labels = [action.text() for action in monitor_menu(self.monitor(self.tool()))]
        self.assertEqual(labels.count("Lights"), 1)
        self.assertFalse(any(label.startswith("Turn lights") for label in labels))

    def test_a_click_asks_for_the_state_the_item_is_not_showing(self) -> None:
        cases = {
            "nothing remembered": (None, "lights on"),
            "on": (True, "lights off"),
            "off": (False, "lights on"),
        }
        for name, (remembered, expected) in cases.items():
            with self.subTest(name):
                self.record.unlink(missing_ok=True)
                monitor = self.monitor(self.tool())
                monitor.lights_preference = remembered
                monitor.lights_on = remembered
                monitor.lights_action.trigger()
                self.wait_for_lights(monitor)
                self.assertEqual(self.ran(), [expected])

    def test_the_item_is_usable_again_afterwards(self) -> None:
        monitor = self.monitor(self.tool())
        self.run_lights(monitor, on=True)
        self.assertTrue(monitor.lights_action.isEnabled())
        self.assertEqual(monitor.lights_action.text(), "Lights")

    def test_the_item_is_disabled_while_a_request_runs(self) -> None:
        # One process serves the item, so it may not start a second request.
        monitor = self.monitor(self.tool())
        monitor.set_lights(True)
        self.assertFalse(monitor.lights_action.isEnabled())
        self.assertEqual(monitor.lights_action.text(), "Turning lights on…")
        self.wait_for_lights(monitor)

    def test_success_is_logged_with_the_state_it_set(self) -> None:
        monitor = self.monitor(self.tool())
        with self.assertLogs(tray.LOGGER, level="INFO") as logged:
            self.run_lights(monitor, on=True)
        self.assertIn("lights turned on", logged.output[0])

    def test_a_failing_command_is_logged_with_its_output_and_state(self) -> None:
        monitor = self.monitor(self.tool(lights_exit=1, lights_error="headset is off"))
        with self.assertLogs(tray.LOGGER, level="ERROR") as logged:
            self.run_lights(monitor, on=True)
        self.assertIn("headset is off", logged.output[0])
        self.assertIn("lights on", logged.output[0])

    def test_a_tool_that_cannot_start_is_logged(self) -> None:
        monitor = self.monitor("/nonexistent/g733-headset")
        with self.assertLogs(tray.LOGGER, level="ERROR") as logged:
            self.run_lights(monitor)
        self.assertIn("Could not start the headset tool", logged.output[0])

    def test_a_failure_leaves_the_battery_reading_alone(self) -> None:
        # The icon and tooltip report the battery; a lights fault must not
        # overwrite them with a "?" that claims the reading was lost.
        monitor = self.monitor(self.tool(lights_exit=1, lights_error="headset is off"))
        monitor.show_reading("discharging", 3989)
        with self.assertLogs(tray.LOGGER, level="ERROR"):
            self.run_lights(monitor)
        self.assertEqual(monitor.status_action.text(), "G733 battery: 80% · 3989 mV")


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

    def monitor(self, tool: str) -> tray.G733Tray:
        monitor = tray.G733Tray(3600, headset=(tool,), idle_aware=False)
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

    def run_startup(self, monitor: tray.G733Tray) -> list[str]:
        """Start the monitor and wait for the first battery reading to complete."""
        # Shortened so the suite does not wait out the real settle window.
        with mock.patch.object(tray, "LIGHTS_SETTLE_MS", 50):
            monitor.start()
            self.wait_for(
                lambda: not self.busy(monitor) and "battery" in self.ran(),
                f"no battery reading was taken; ran: {self.ran()!r}",
            )
        return self.ran()

    def test_a_successful_choice_is_stored(self) -> None:
        monitor = self.monitor(self.tool())
        monitor.set_lights(True)
        self.settle(monitor)
        self.assertEqual(json.loads(self.state_file.read_text()), {"lights": True})
        monitor.set_lights(False)
        self.settle(monitor)
        self.assertEqual(json.loads(self.state_file.read_text()), {"lights": False})

    def test_a_failed_choice_is_not_stored(self) -> None:
        # Remembering a state the headset never took would re-apply a lie.
        monitor = self.monitor(self.tool(lights_exit=1))
        with self.assertLogs(tray.LOGGER, level="ERROR"):
            monitor.set_lights(True)
            self.settle(monitor)
        self.assertFalse(self.state_file.exists())

    def test_the_item_is_ticked_while_the_remembered_state_is_on(self) -> None:
        monitor = self.monitor(self.tool())
        self.assertFalse(monitor.lights_action.isChecked())
        monitor.set_lights(True)
        self.settle(monitor)
        self.assertTrue(monitor.lights_action.isChecked())
        monitor.set_lights(False)
        self.settle(monitor)
        self.assertFalse(monitor.lights_action.isChecked())

    def test_a_failed_click_leaves_the_tick_alone(self) -> None:
        # Qt toggles the tick on click; it must go back to what the headset has.
        monitor = self.monitor(self.tool(lights_exit=1))
        with self.assertLogs(tray.LOGGER, level="ERROR"):
            monitor.lights_action.trigger()
            self.assertFalse(monitor.lights_action.isChecked())
            self.settle(monitor)
        self.assertFalse(monitor.lights_action.isChecked())

    def test_startup_applies_the_remembered_state_after_the_first_reading(self) -> None:
        # A reading that succeeds shows the headset is on; the lights follow it.
        tray.save_lights_preference(False)
        monitor = self.monitor(self.tool())
        self.assertEqual(self.run_startup(monitor)[:2], ["battery", "lights off"])

    def test_a_headset_that_is_off_at_startup_gets_its_lights_once_it_answers(self) -> None:
        tray.save_lights_preference(False)
        switched_on = Path(self._tmp.name) / "switched-on"
        monitor = self.monitor(self.tool(switched_on=switched_on))
        self.run_startup(monitor)
        monitor.refresh()
        self.settle(monitor)
        # Nothing is sent to a headset that has not answered.
        self.assertEqual(self.ran(), ["battery", "battery"])
        self.assertTrue(monitor.lights_restore_pending)

        switched_on.touch()
        monitor.refresh()
        self.settle(monitor)
        self.assertEqual(self.ran()[2:], ["battery", "lights off"])
        self.assertFalse(monitor.lights_restore_pending)

    def test_a_restore_is_applied_once(self) -> None:
        tray.save_lights_preference(False)
        monitor = self.monitor(self.tool())
        self.run_startup(monitor)
        monitor.refresh()
        self.settle(monitor)
        self.assertEqual(self.ran().count("lights off"), 1)

    def test_startup_without_a_remembered_state_reads_the_headset_lights_state(self) -> None:
        monitor = self.monitor(self.tool(lights_state="on"))
        monitor.start()
        self.settle(monitor)
        self.assertEqual(self.ran(), ["battery", "lights status"])
        self.assertTrue(monitor.lights_action.isChecked())

    def test_a_lights_write_holds_the_next_reading_back_then_brings_it_forward(self) -> None:
        monitor = self.monitor(self.tool())
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
        # The user pressed nothing, and the restore is tried again after the
        # next reading, so it must not open a notification.
        tray.save_lights_preference(True)
        monitor = self.monitor(self.tool(lights_exit=1))
        with (
            mock.patch.object(monitor.tray, "showMessage") as notified,
            self.assertLogs(tray.LOGGER, level="ERROR") as logged,
        ):
            monitor.start()
            self.settle(monitor)
        self.assertIn("lights on", logged.output[0])
        notified.assert_not_called()

    def test_a_failed_restore_is_tried_again_after_the_next_reading(self) -> None:
        tray.save_lights_preference(True)
        monitor = self.monitor(self.tool(lights_exit=1))
        with self.assertLogs(tray.LOGGER, level="ERROR"):
            self.run_startup(monitor)
            self.assertTrue(monitor.lights_restore_pending)
            monitor.refresh()
            self.settle(monitor)
        self.assertEqual(self.ran(), ["battery", "lights on", "battery", "lights on"])

    def test_a_choice_replaces_a_restore_that_is_still_waiting(self) -> None:
        # The headset got what the user chose; the older state must not follow.
        tray.save_lights_preference(True)
        switched_on = Path(self._tmp.name) / "switched-on"
        monitor = self.monitor(self.tool(switched_on=switched_on))
        self.run_startup(monitor)
        monitor.set_lights(False)
        self.settle(monitor)
        switched_on.touch()
        monitor.refresh()
        self.settle(monitor)
        self.assertEqual(self.ran(), ["battery", "lights off", "battery", "lights status"])

    def test_a_tool_that_cannot_start_still_schedules_the_next_reading(self) -> None:
        # Nothing runs, so the poll must still be scheduled rather than lost,
        # and the remembered state keeps waiting for a reading to succeed.
        tray.save_lights_preference(True)
        monitor = self.monitor("/nonexistent/g733-headset")
        with self.assertLogs(tray.LOGGER, level="ERROR") as logged:
            monitor.start()
            self.settle(monitor)
        self.assertTrue(any("Could not start the headset tool" in line for line in logged.output))
        self.assertTrue(monitor.poll_timer.isActive())
        self.assertTrue(monitor.lights_restore_pending)

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
        monitor = tray.G733Tray(3600, headset=(self.tool(),), idle_aware=False)
        self.addCleanup(monitor.poll_timer.stop)
        return monitor

    def test_low_battery_latches_once_then_resets_when_recharged(self) -> None:
        monitor = self.monitor()
        with mock.patch.object(monitor.tray, "showMessage") as notified:
            monitor.show_reading("discharging", voltage_for(10))
            monitor.show_reading("discharging", voltage_for(10))
        notified.assert_called_once()
        self.assertTrue(monitor.low_battery_notified)
        monitor.show_reading("charging", 4100)
        self.assertFalse(monitor.low_battery_notified)

    def test_charging_suppresses_the_low_battery_warning(self) -> None:
        monitor = self.monitor()
        with mock.patch.object(monitor.tray, "showMessage") as notified:
            monitor.show_reading("charging", voltage_for(10))
        notified.assert_not_called()

    def test_a_high_charging_voltage_is_not_announced_as_full(self) -> None:
        # Regression: a charging reading that estimated to 100% was announced as
        # a full charge minutes after a half-empty headset was plugged in.
        monitor = self.monitor()
        with mock.patch.object(monitor.tray, "showMessage") as notified:
            monitor.show_reading("charging", 4210)
        notified.assert_not_called()

    def test_a_finished_charge_is_announced_once(self) -> None:
        monitor = self.monitor()
        with mock.patch.object(monitor.tray, "showMessage") as notified:
            monitor.show_reading("full", 4112)
            monitor.show_reading("full", 4104)
        notified.assert_called_once()
        self.assertIn("fully charged", notified.call_args.args[0])

    def test_a_top_up_on_the_cable_is_not_announced_again(self) -> None:
        # Left on the cable, the charger may restart and finish again.
        monitor = self.monitor()
        monitor.show_reading("full", 4112)
        with mock.patch.object(monitor.tray, "showMessage") as notified:
            monitor.show_reading("charging", 4200)
            monitor.show_reading("full", 4112)
        notified.assert_not_called()

    def test_coming_off_the_cable_re_arms_the_announcement(self) -> None:
        monitor = self.monitor()
        monitor.show_reading("full", 4112)
        monitor.show_reading("discharging", 4100)
        self.assertFalse(monitor.full_battery_notified)
        with mock.patch.object(monitor.tray, "showMessage") as notified:
            monitor.show_reading("full", 4112)
        notified.assert_called_once()


class ErrorLoggingTests(StubCommandMixin, unittest.TestCase):
    """Errors must reach stderr; an autostarted monitor has no other channel."""

    def monitor(self) -> tray.G733Tray:
        monitor = tray.G733Tray(3600, headset=(self.tool(),), idle_aware=False)
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
            monitor.show_reading("discharging", 3989)
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

    def monitor(self) -> tray.G733Tray:
        monitor = tray.G733Tray(3600, headset=(self.tool(),), idle_aware=False)
        self.addCleanup(monitor.poll_timer.stop)
        self.addCleanup(monitor.lights_timeout.stop)
        return monitor

    def test_a_battery_error_is_shown_and_cleared_on_recovery(self) -> None:
        monitor = self.monitor()
        monitor.show_error("receiver is unplugged")
        self.assertEqual(monitor.tray.toolTip(), "receiver is unplugged")
        monitor.show_reading("discharging", 3989)
        self.assertEqual(monitor.tray.toolTip(), "G733 battery: 80% · 3989 mV")

    def test_a_lights_fault_joins_the_battery_line_until_it_is_resolved(self) -> None:
        monitor = self.monitor()
        monitor.show_reading("discharging", 3989)
        with self.assertLogs(tray.LOGGER, level="ERROR"):
            monitor.report_lights_failure("Could not turn the G733 lights on: headset is off")
        # The icon keeps reporting the battery, which is still known; the
        # explanation lives in the tooltip and the menu.
        self.assertIn("G733 battery: 80% · 3989 mV", monitor.tray.toolTip())
        self.assertIn("headset is off", monitor.tray.toolTip())
        self.assertTrue(monitor.lights_status_action.isVisible())

        monitor.lights_fault = None
        monitor.apply_status()
        self.assertEqual(monitor.tray.toolTip(), "G733 battery: 80% · 3989 mV")
        self.assertFalse(monitor.lights_status_action.isVisible())

    def test_a_successful_lights_request_clears_an_earlier_fault(self) -> None:
        monitor = self.monitor()
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
        monitor = tray.G733Tray(3600, headset=(str(slow),), idle_aware=False)
        self.addCleanup(monitor.poll_timer.stop)
        monitor.refresh()
        monitor.set_lights(True)
        with mock.patch.object(tray.LOGGER, "error") as logged:
            monitor.quit()
            APP.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        logged.assert_not_called()


class ArgumentTests(unittest.TestCase):
    def parse(self, argv: list[str], **env: str) -> int:
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
        with mock.patch.dict(os.environ):
            os.environ.pop("POLL_SECONDS", None)
            self.assertEqual(self.parse([]), 60)

    def test_the_environment_sets_the_interval(self) -> None:
        self.assertEqual(self.parse([], POLL_SECONDS="90"), 90)

    def test_the_option_overrides_the_environment(self) -> None:
        self.assertEqual(self.parse(["--interval", "120"], POLL_SECONDS="60"), 120)

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
            # The HeadsetControl option is gone; a command line that still
            # passes it must fail loudly rather than be ignored.
            "removed --command": (["--command", "/bin/true"], {}),
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
