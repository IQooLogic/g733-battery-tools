#!/usr/bin/env python3
"""KDE system-tray battery monitor for a Logitech G733 headset."""

from __future__ import annotations

import json
import logging
import os
import signal
import statistics
import sys
import time
from collections import deque
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

from PyQt6.QtCore import QProcess, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTransform,
)
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from g733_headset import HeadsetError, ReceiverNotFound, find_device

# The script beside this one talks HID++ to the headset, for both the battery
# and the lights. It runs as its own process so a headset that does not answer
# can be timed out and killed without blocking the tray.
DEFAULT_HEADSET = (sys.executable, str(Path(__file__).resolve().with_name("g733_headset.py")))
LIGHTS_LABEL = "Lights"
LOW_BATTERY_PERCENT = 20
LOW_BATTERY_RESET_PERCENT = 25
MINIMUM_INTERVAL_SECONDS = 5
RECEIVER_WAIT_SECONDS = 30
RECEIVER_POLL_SECONDS = 1
REQUEST_TIMEOUT_MS = 15_000
# Measured on the G733 when HeadsetControl switched the lights: for about two
# seconds afterwards it got no usable battery reading. A reading taken inside
# that window would show a false "!", so readings are held off and then retried.
LIGHTS_SETTLE_MS = 3_000
# While no application is playing through the G733, check PipeWire frequently
# enough to notice playback starting, but never contact the headset. This lets
# its own inactivity timer switch it off.
IDLE_ACTIVITY_CHECK_MS = 5_000
ACTIVITY_QUERY_TIMEOUT_MS = 2_000
PIPEWIRE_DUMP_COMMAND = "pw-dump"
# These match the IDs used by g733_headset.py. PipeWire may expose them as
# device.vendor.id/device.product.id, or together in alsa.components.
G733_VENDOR_ID = "0x046d"
G733_PRODUCT_ID = "0x0b1f"
G733_ALSA_COMPONENT = f"usb{G733_VENDOR_ID[2:]}:{G733_PRODUCT_ID[2:]}"
# g733_headset.py uses this exit status when its receiver is present but the
# headset does not answer. That normally means it is off or out of range, not
# that the monitor has failed.
HEADSET_UNAVAILABLE_EXIT = 3

# The battery states the headset tool reports. It exits with an error for
# anything else.
STATE_DISCHARGING = "discharging"
STATE_CHARGING = "charging"
STATE_FULL = "full"
BATTERY_STATES = {STATE_DISCHARGING, STATE_CHARGING, STATE_FULL}

# The headset reports only a voltage, so the percentage is an estimate from a
# Li-ion discharge curve: Solaar's measured (millivolt, percent) table, which
# OpenLogi also uses. It holds only while discharging; a charger raises the
# voltage, so no percentage is estimated while one is connected.
DISCHARGE_CURVE = (
    (4186, 100),
    (4067, 90),
    (3989, 80),
    (3922, 70),
    (3859, 60),
    (3811, 50),
    (3778, 40),
    (3751, 30),
    (3717, 20),
    (3671, 10),
    (3646, 5),
    (3579, 2),
    (3500, 0),
)
# The voltage moves by tens of millivolts with volume and lighting load, which
# is several percent on the curve. The median of the last few discharging
# readings keeps one such swing off the icon; at the default one-minute
# interval a real change shows within three readings.
SMOOTHING_READINGS = 5

USAGE = f"""Usage: g733_battery_tray.py [--interval SECONDS]

Polls the G733 battery every SECONDS seconds (default 60, minimum
{MINIMUM_INTERVAL_SECONDS}). POLL_SECONDS sets the same value; the option wins."""

LOGGER = logging.getLogger("g733-battery-tray")

# A lightning bolt drawn inside the 64x64 icon to mark charging. Drawn as a path
# rather than a glyph so it does not depend on the font having one. It fills the
# icon when no percentage is reported, and sits in the corner beside one when a
# percentage is, so charging never depends on telling the colours apart.
BOLT_POINTS = ((38, 10), (23, 37), (32, 37), (27, 55), (43, 27), (34, 27))
BOLT_AREA = QRectF(22, 8, 22, 48)
BOLT_BADGE_AREA = QRectF(39, 13, 20, 41)


def parse_interval(value: str, source: str) -> int:
    """Convert one interval value, reporting the option or variable it came from."""
    try:
        return int(value)
    except ValueError:
        print(f"{source} requires a positive whole number; got: {value!r}", file=sys.stderr)
        raise SystemExit(2) from None


def parse_arguments() -> int:
    """Return the polling interval from a small CLI."""
    arguments = sys.argv[1:]
    # Handled before anything else so --help still works with a bad environment.
    if {"-h", "--help"}.intersection(arguments):
        print(USAGE)
        raise SystemExit(0)

    interval = parse_interval(os.environ.get("POLL_SECONDS", "60"), "POLL_SECONDS")

    remaining = iter(arguments)
    for argument in remaining:
        if argument == "--interval":
            interval = parse_interval(next(remaining, ""), "--interval")
        else:
            print(f"Unknown argument: {argument}", file=sys.stderr)
            raise SystemExit(2)

    if interval < MINIMUM_INTERVAL_SECONDS:
        print(
            f"Polling interval must be at least {MINIMUM_INTERVAL_SECONDS} seconds",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return interval


def wait_for_receiver(
    wait_seconds: float = RECEIVER_WAIT_SECONDS,
    poll_seconds: float = RECEIVER_POLL_SECONDS,
    finder: Callable[[], Path] = find_device,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait briefly for the receiver's HID++ interface without opening it.

    USB devices can appear after the desktop autostarts. Once this bounded wait
    expires, a later receiver connection should launch the monitor via a
    device-triggered service rather than leave an idle tray process running.
    """
    deadline = monotonic() + wait_seconds
    while True:
        try:
            finder()
            return True
        except ReceiverNotFound:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            sleep(min(poll_seconds, remaining))


def g733_playback_is_active(snapshot: object) -> bool:
    """Return whether PipeWire has an active playback link into the G733.

    ``pw-dump`` is PipeWire's JSON registry snapshot. Looking for active links
    rather than merely a running G733 sink matters: a sink can remain running
    after a client has stopped playing, and must not keep the headset awake.
    """
    if not isinstance(snapshot, list):
        raise ValueError("PipeWire registry is not a list")
    g733_sinks: set[int] = set()
    for item in snapshot:
        if not isinstance(item, dict) or item.get("type") != "PipeWire:Interface:Node":
            continue
        info = item.get("info")
        if not isinstance(info, dict):
            continue
        props = info.get("props")
        if not isinstance(props, dict):
            continue
        vendor_id = props.get("device.vendor.id")
        product_id = props.get("device.product.id")
        alsa_components = props.get("alsa.components")
        matches_device_ids = (
            isinstance(vendor_id, str)
            and isinstance(product_id, str)
            and vendor_id.lower() == G733_VENDOR_ID
            and product_id.lower() == G733_PRODUCT_ID
        )
        # WirePlumber commonly leaves the USB IDs out of a sink's properties
        # and puts them only in alsa.components, for example "USB046d:0b1f".
        matches_alsa_component = (
            isinstance(alsa_components, str) and G733_ALSA_COMPONENT in alsa_components.lower()
        )
        if (
            props.get("media.class") == "Audio/Sink"
            and (matches_device_ids or matches_alsa_component)
            and isinstance(item.get("id"), int)
        ):
            g733_sinks.add(item["id"])

    for item in snapshot:
        if not isinstance(item, dict) or item.get("type") != "PipeWire:Interface:Link":
            continue
        info = item.get("info")
        if (
            isinstance(info, dict)
            and info.get("state") == "active"
            and info.get("input-node-id") in g733_sinks
        ):
            return True
    return False


def estimate_percent(voltage_mv: float) -> int:
    """Estimate the charge of a discharging battery from its voltage."""
    top_mv, top_percent = DISCHARGE_CURVE[0]
    if voltage_mv >= top_mv:
        return top_percent
    for (high_mv, high_percent), (low_mv, low_percent) in pairwise(DISCHARGE_CURVE):
        if voltage_mv >= low_mv:
            fraction = (voltage_mv - low_mv) / (high_mv - low_mv)
            return round(low_percent + fraction * (high_percent - low_percent))
    return 0


def bolt_path(area: QRectF) -> QPainterPath:
    """Return the lightning bolt scaled to fill one area of the icon."""
    bolt = QPainterPath()
    bolt.moveTo(*BOLT_POINTS[0])
    for point in BOLT_POINTS[1:]:
        bolt.lineTo(*point)
    bolt.closeSubpath()

    drawn = bolt.boundingRect()
    transform = QTransform()
    transform.translate(area.x(), area.y())
    transform.scale(area.width() / drawn.width(), area.height() / drawn.height())
    transform.translate(-drawn.x(), -drawn.y())
    return transform.map(bolt)


def state_file() -> Path:
    """Locate the file that remembers the chosen lighting state."""
    # Read on each call rather than at import, so the environment a test or a
    # session sets is the one that is used.
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "g733-battery-tray" / "state.json"


def load_lights_preference() -> bool | None:
    """Return the remembered lighting state, or None when there is none to apply."""
    path = state_file()
    try:
        stored = json.loads(path.read_text())["lights"]
    except FileNotFoundError:
        return None
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        LOGGER.error("ignoring unreadable state file %s: %s", path, exc)
        return None
    if not isinstance(stored, bool):
        LOGGER.error("ignoring invalid lights state in %s: %r", path, stored)
        return None
    return stored


def save_lights_preference(on: bool) -> None:
    """Record the lighting state so the next startup can apply it again."""
    path = state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"lights": on}) + "\n")
    except OSError as exc:
        # Reported rather than raised: the headset was still switched, and the
        # monitor has to keep running.
        LOGGER.error("could not save the lights state to %s: %s", path, exc)


def icon_for(level: int | None, *, is_error: bool = False, is_charging: bool = False) -> QIcon:
    """Create a compact numeric icon that remains legible in Plasma's tray."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    if is_error:
        # "!" reports a fault and "?" only that nothing has been read yet, so a
        # glance separates a headset that is off from a monitor that just began.
        color, label = QColor("#7f8c8d"), "!"
    elif is_charging:
        # Blue means charging regardless of level, so a low charging battery
        # does not read as an alarm.
        color, label = QColor("#2980b9"), "" if level is None else str(level)
    elif level is None:
        color, label = QColor("#7f8c8d"), "?"
    elif level <= LOW_BATTERY_PERCENT:
        color, label = QColor("#e74c3c"), str(level)
    elif level <= 50:
        color, label = QColor("#f39c12"), str(level)
    else:
        color, label = QColor("#27ae60"), str(level)

    painter.setPen(QPen(color.darker(120), 3))
    painter.setBrush(color)
    painter.drawRoundedRect(3, 5, 58, 54, 12, 12)

    if label:
        painter.setPen(QColor("white"))
        font = QFont()
        font.setBold(True)
        # A charging label shares the icon with the bolt badge, so it is set one
        # step smaller and left of centre to keep both legible.
        if is_charging:
            # The label shares the icon with the bolt, so it is set smaller and
            # left of centre to keep both legible at tray size.
            font.setPointSize(19 if len(label) < 3 else 14)
        else:
            font.setPointSize(23 if len(label) < 3 else 17)
        painter.setFont(font)
        area = pixmap.rect().adjusted(0, 0, -28, 0) if is_charging else pixmap.rect()
        painter.drawText(area, Qt.AlignmentFlag.AlignCenter, label)

    if is_charging:
        # Outlined in the icon's own colour so the bolt stays separate from the
        # digits beside it.
        # Drawn without an outline: at tray size an outline in the icon's own
        # colour eats most of a bolt this small.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("white"))
        painter.drawPath(bolt_path(BOLT_BADGE_AREA if label else BOLT_AREA))

    painter.end()
    return QIcon(pixmap)


class G733Tray:
    def __init__(
        self,
        interval_seconds: int,
        headset: tuple[str, ...] = DEFAULT_HEADSET,
        *,
        idle_aware: bool = True,
    ) -> None:
        # The command line that runs the headset tool; its subcommand is added.
        self.headset = tuple(headset)
        self.idle_aware = idle_aware
        self.interval_ms = interval_seconds * 1000
        # Recent discharging voltages, for the median shown as the level.
        self.voltages: deque[int] = deque(maxlen=SMOOTHING_READINGS)
        self.in_flight = False
        # Set while shutting down, so a request killed on the way out is not
        # reported as a fault the user should act on.
        self.quitting = False
        self.low_battery_notified = False
        self.full_battery_notified = False
        self.last_error: str | None = None
        self.status_text = "G733 battery: waiting for first reading"
        # The last lights fault, kept until a lights request succeeds, so the
        # tooltip keeps explaining a failure whose notification has gone.
        self.lights_fault: str | None = None
        # The state the running lights request asks for, so its result can name it.
        self.lights_request = False
        self.lights_operation: str | None = None
        self.lights_preference = load_lights_preference()
        # The preference is only a fallback until the headset reports its
        # actual setting. A headset powered on after an autostart can reset it.
        self.lights_on: bool | None = self.lights_preference
        self.lights_status_pending = True
        # The remembered state still has to be applied to the headset. It waits
        # for a battery reading to succeed, which shows the headset is on: an
        # autostarted monitor commonly begins before the headset is switched on.
        self.lights_restore_pending = self.lights_preference is not None
        # True only while that restore runs; the user did not ask for it.
        self.restoring_lights = False
        # When the headset is expected to answer battery requests again.
        self.lights_settle_until = 0.0
        # Set only after PipeWire positively reports that nothing is playing
        # into the G733. A missing/broken PipeWire command falls back to the
        # old polling behaviour rather than making the battery monitor silent.
        self.polling_paused = False
        self.audio_check_pending = False

        self.tray = QSystemTrayIcon(icon_for(None), QApplication.instance())
        self.tray.setToolTip(self.status_text)
        self.tray.activated.connect(self.on_activated)

        self.menu = QMenu()
        self.status_action = QAction(self.status_text, self.menu)
        self.status_action.setEnabled(False)
        self.lights_status_action = QAction("", self.menu)
        self.lights_status_action.setEnabled(False)
        self.lights_status_action.setVisible(False)
        self.refresh_action = QAction("Refresh now", self.menu)
        self.refresh_action.triggered.connect(lambda: self.refresh(manual=True))
        # One item for both states: ticked while the lights are on, and a click
        # asks for the other state. The tick follows the remembered state, which
        # is also what the next startup applies.
        self.lights_action = QAction(LIGHTS_LABEL, self.menu)
        self.lights_action.setCheckable(True)
        self.lights_action.triggered.connect(self.toggle_lights)
        self.update_lights_check()
        self.quit_action = QAction("Quit", self.menu)
        self.quit_action.triggered.connect(self.quit)
        self.menu.addAction(self.status_action)
        self.menu.addSeparator()
        self.menu.addAction(self.refresh_action)
        self.menu.addAction(self.lights_action)
        self.menu.addAction(self.lights_status_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.tray.setContextMenu(self.menu)

        self.process = QProcess()
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)

        # PipeWire is queried separately from the HID process. A failed query
        # is harmless and falls back to normal polling; it must never block the
        # Qt event loop or the tray menu.
        self.audio_process = QProcess()
        self.audio_process.finished.connect(self.audio_check_finished)
        self.audio_process.errorOccurred.connect(self.audio_check_error)
        self.audio_timeout = QTimer()
        self.audio_timeout.setSingleShot(True)
        self.audio_timeout.timeout.connect(self.audio_check_timed_out)

        # The lights command runs on its own process and timer, so pressing the
        # menu item neither cancels a battery reading nor delays the next one.
        self.lights_process = QProcess()
        self.lights_process.finished.connect(self.lights_finished)
        self.lights_process.errorOccurred.connect(self.lights_error)
        self.lights_timeout = QTimer()
        self.lights_timeout.setSingleShot(True)
        self.lights_timeout.timeout.connect(self.lights_timed_out)

        self.timeout = QTimer()
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self.process_timed_out)
        self.poll_timer = QTimer()
        self.poll_timer.setSingleShot(True)
        self.poll_timer.timeout.connect(lambda: self.refresh(manual=False))

    def start(self) -> None:
        self.tray.show()
        # Always take one initial reading.  Otherwise an autostarted monitor
        # with no current audio link remains at "waiting for first reading"
        # indefinitely, until the user clicks it.  Idle-aware policy applies
        # to later automatic polls, after there is something to display.
        QTimer.singleShot(0, lambda: self.refresh(manual=True))

    def schedule_next_poll(self) -> None:
        if self.polling_paused:
            # This only starts pw-dump, not a HID request, so it cannot wake
            # the headset while it is waiting for its firmware timeout.
            self.poll_timer.start(IDLE_ACTIVITY_CHECK_MS)
            return
        # A recent lights write both holds the next reading back and brings it
        # forward, so a reading it spoiled is corrected in seconds, not minutes.
        settling_ms = int((self.lights_settle_until - time.monotonic()) * 1000)
        delay_ms = min(self.interval_ms, settling_ms) if settling_ms > 0 else self.interval_ms
        self.poll_timer.start(delay_ms)

    def refresh(self, manual: bool = True) -> None:
        """Refresh now, or first check audio activity for an automatic poll."""
        if self.in_flight:
            return
        if manual or not self.idle_aware:
            # A user who explicitly asks for a refresh accepts that it wakes
            # the headset, even when automatic polling is paused.
            self.polling_paused = False
            self.start_battery_request()
            return
        if self.audio_check_pending:
            return
        self.audio_check_pending = True
        self.audio_process.start(PIPEWIRE_DUMP_COMMAND)
        self.audio_timeout.start(ACTIVITY_QUERY_TIMEOUT_MS)

    def start_battery_request(self) -> None:
        """Start the HID battery request after the polling policy permits it."""
        self.in_flight = True
        self.refresh_action.setEnabled(False)
        self.status_action.setText("G733: refreshing…")
        self.run_headset(self.process, "battery")
        self.timeout.start(REQUEST_TIMEOUT_MS)

    def audio_check_finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        if not self.audio_check_pending or self.quitting:
            return
        self.audio_check_pending = False
        self.audio_timeout.stop()
        output = bytes(self.audio_process.readAllStandardOutput()).decode(errors="replace")
        if exit_code != 0:
            self.audio_activity_unavailable()
            return
        try:
            active = g733_playback_is_active(json.loads(output))
        except (ValueError, json.JSONDecodeError):
            self.audio_activity_unavailable()
            return
        self.polling_paused = not active
        if active:
            self.start_battery_request()
        else:
            self.apply_status()
            self.schedule_next_poll()

    def audio_check_error(self, _error: QProcess.ProcessError) -> None:
        if not self.audio_check_pending or self.quitting:
            return
        self.audio_check_pending = False
        self.audio_timeout.stop()
        self.audio_activity_unavailable()

    def audio_check_timed_out(self) -> None:
        if not self.audio_check_pending:
            return
        self.audio_check_pending = False
        self.audio_process.kill()
        self.audio_activity_unavailable()

    def audio_activity_unavailable(self) -> None:
        """Keep the established polling behaviour without a usable PipeWire API."""
        self.polling_paused = False
        self.start_battery_request()

    def run_headset(self, process: QProcess, *arguments: str) -> None:
        """Start one headset tool command on one of the two processes."""
        program, *base = self.headset
        process.start(program, [*base, *arguments])

    def toggle_lights(self) -> None:
        """Ask for the state the Lights item is not showing."""
        # With no headset state yet, an unticked item still means a click asks
        # for lights on.
        self.set_lights(self.lights_on is not True)

    def set_lights(self, on: bool) -> None:
        """Ask the headset to switch its RGB lighting on or off."""
        # One process serves the item, so a second press while a request runs
        # is ignored rather than queued behind it.
        if self.lights_process.state() != QProcess.ProcessState.NotRunning:
            return
        self.lights_request = on
        self.lights_operation = "set"
        self.lights_action.setEnabled(False)
        # Clicking a checkable item toggles its tick; the tick must keep showing
        # the remembered state until this request succeeds.
        self.update_lights_check()
        self.lights_action.setText(f"Turning lights {self.lights_state()}…")
        self.run_headset(self.lights_process, "lights", self.lights_state())
        self.lights_timeout.start(REQUEST_TIMEOUT_MS)

    def lights_state(self) -> str:
        """Return "on" or "off" for the request that is running."""
        return "on" if self.lights_request else "off"

    def read_lights_state_if_pending(self) -> None:
        """Read the headset setting after it has answered a battery request."""
        if not self.lights_status_pending:
            return
        if self.lights_process.state() != QProcess.ProcessState.NotRunning:
            return
        self.lights_operation = "status"
        self.lights_action.setEnabled(False)
        self.lights_action.setText("Checking lights…")
        self.run_headset(self.lights_process, "lights", "status")
        self.lights_timeout.start(REQUEST_TIMEOUT_MS)

    def finish_lights_status_request(self) -> None:
        self.lights_timeout.stop()
        self.lights_operation = None
        self.lights_action.setEnabled(True)
        self.lights_action.setText(LIGHTS_LABEL)
        self.update_lights_check()

    def finish_lights_request(self) -> None:
        self.lights_timeout.stop()
        self.lights_operation = None
        self.lights_settle_until = time.monotonic() + LIGHTS_SETTLE_MS / 1000
        # A poll already counting down could otherwise land inside the window
        # the write just opened and report a battery that is only unreachable.
        if self.poll_timer.isActive():
            self.schedule_next_poll()
        self.lights_action.setEnabled(True)
        self.lights_action.setText(LIGHTS_LABEL)
        self.update_lights_check()

    def restore_lights_if_pending(self) -> None:
        """Apply the remembered state, once the headset has answered a reading."""
        if not self.lights_restore_pending:
            return
        # A request the user started is running; it decides the state instead.
        if self.lights_process.state() != QProcess.ProcessState.NotRunning:
            return
        self.lights_restore_pending = False
        self.restoring_lights = True
        self.set_lights(self.lights_preference)

    def end_lights_restore(self, succeeded: bool) -> None:
        """Close a restore; one that failed waits for the next good reading."""
        if not self.restoring_lights:
            return
        self.restoring_lights = False
        self.lights_restore_pending = not succeeded

    def update_lights_check(self) -> None:
        """Tick the Lights item when the headset reports that it is on."""
        self.lights_action.setChecked(self.lights_on is True)

    def remember_lights(self, on: bool) -> None:
        self.lights_preference = on
        self.lights_on = on
        # The headset now has the state the user chose, so a restore still
        # waiting would only apply an older one.
        self.lights_restore_pending = False
        save_lights_preference(on)
        self.update_lights_check()

    def lights_finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        if self.quitting:
            return
        error_output = (
            bytes(self.lights_process.readAllStandardError()).decode(errors="replace").strip()
        )
        output = bytes(self.lights_process.readAllStandardOutput()).decode(errors="replace").strip()
        if self.lights_operation is None:
            # The process was killed after a timeout; its late finished signal
            # must not be mistaken for a completed lights write.
            return
        if self.lights_operation == "status":
            self.finish_lights_status_request()
            if exit_code != 0:
                details = error_output or output or f"headset tool exited with {exit_code}"
                LOGGER.error("Could not read the G733 lights state: %s", details)
                return
            try:
                state = json.loads(output)["lights"]
                if state not in {"on", "off"}:
                    raise ValueError(f"invalid state {state!r}")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                LOGGER.error(
                    "Could not read the G733 lights state: unreadable headset tool output: %s", exc
                )
                return
            self.lights_on = state == "on"
            self.lights_status_pending = False
            self.update_lights_check()
            return

        state = self.lights_state()
        self.finish_lights_request()
        succeeded = exit_code == 0
        if not succeeded:
            details = error_output or output or f"headset tool exited with {exit_code}"
            self.report_lights_failure(f"Could not turn the G733 lights {state}: {details}")
        else:
            LOGGER.info("lights turned %s", state)
            # The fault is over, so its explanation goes with it.
            self.lights_fault = None
            self.apply_status()
            # A restore applies what is already stored; only a choice is saved.
            if not self.restoring_lights:
                self.remember_lights(self.lights_request)
            else:
                self.lights_on = self.lights_request
                self.update_lights_check()
        self.end_lights_restore(succeeded)

    def lights_error(self, error: QProcess.ProcessError) -> None:
        if self.quitting:
            return
        if error == QProcess.ProcessError.FailedToStart:
            if self.lights_operation == "status":
                self.finish_lights_status_request()
                LOGGER.error(
                    "Could not read the G733 lights state: %s", self.lights_process.errorString()
                )
                return
            self.finish_lights_request()
            self.report_lights_failure(
                f"Could not start the headset tool: {self.lights_process.errorString()}"
            )
            self.end_lights_restore(succeeded=False)

    def lights_timed_out(self) -> None:
        if self.lights_process.state() == QProcess.ProcessState.NotRunning:
            return
        operation = self.lights_operation
        state = self.lights_state()
        self.lights_process.kill()
        if operation == "status":
            self.finish_lights_status_request()
            LOGGER.error("Reading the G733 lights state timed out")
            return
        self.finish_lights_request()
        self.report_lights_failure(f"Turning the G733 lights {state} timed out")
        self.end_lights_restore(succeeded=False)

    def report_lights_failure(self, message: str) -> None:
        # Notified rather than shown on the icon: the battery reading is still
        # valid, so the tooltip and icon must keep reporting it.
        LOGGER.error("%s", message)
        # Kept in the tooltip and the menu, not on the icon: the icon reports
        # the battery, and that reading is still valid.
        self.lights_fault = message
        self.apply_status()
        # A restore raises no notification: the user pressed nothing, and it is
        # tried again after the next reading that succeeds.
        if not self.restoring_lights:
            self.tray.showMessage("G733 lights", message, QSystemTrayIcon.MessageIcon.Warning)

    def finish_request(self) -> None:
        self.timeout.stop()
        self.in_flight = False
        self.refresh_action.setEnabled(True)
        self.schedule_next_poll()

    def process_finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        if not self.in_flight or self.quitting:
            return
        output = bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        error_output = bytes(self.process.readAllStandardError()).decode(errors="replace").strip()
        self.finish_request()
        if exit_code == HEADSET_UNAVAILABLE_EXIT:
            self.show_headset_unavailable()
            return
        if exit_code != 0:
            details = error_output or f"headset tool exited with {exit_code}"
            self.show_error(f"G733 battery unavailable: {details}")
            return
        try:
            reading = json.loads(output)
            state = reading["state"]
            voltage_mv = reading["voltage_mv"]
            if state not in BATTERY_STATES:
                raise ValueError(f"unknown state {state!r}")
            if not isinstance(voltage_mv, int) or voltage_mv <= 0:
                raise ValueError(f"invalid voltage {voltage_mv!r}")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.show_error(f"G733 battery unavailable: unreadable headset tool output: {exc}")
            return
        self.show_reading(state, voltage_mv)
        # The headset has just answered, so a remembered lights state that could
        # not be applied yet can be now. Otherwise read its setting so a headset
        # switched on after the monitor has the right menu tick.
        if self.lights_restore_pending:
            self.restore_lights_if_pending()
        else:
            self.read_lights_state_if_pending()

    def process_error(self, error: QProcess.ProcessError) -> None:
        if self.quitting:
            return
        if error == QProcess.ProcessError.FailedToStart and self.in_flight:
            self.finish_request()
            self.show_error(f"Could not start the headset tool: {self.process.errorString()}")

    def process_timed_out(self) -> None:
        if not self.in_flight:
            return
        self.process.kill()
        self.finish_request()
        self.show_error("G733 battery request timed out")

    def show_reading(self, state: str, voltage_mv: int) -> None:
        """Display one battery reading, and raise any notification it calls for."""
        if state == STATE_DISCHARGING:
            self.voltages.append(voltage_mv)
            smoothed_mv = round(statistics.median(self.voltages))
            level = estimate_percent(smoothed_mv)
            text = f"G733 battery: {level}% · {smoothed_mv} mV"
            icon = icon_for(level)
        else:
            # A charger holds the voltage up, so neither the history nor a
            # percentage estimated from it would mean anything.
            self.voltages.clear()
            level = None
            if state == STATE_FULL:
                text = "G733 battery: fully charged"
                icon = icon_for(100, is_charging=True)
            else:
                text = "G733 battery: charging"
                icon = icon_for(None, is_charging=True)

        if self.last_error is not None:
            LOGGER.info("recovered: %s", text)
            self.last_error = None

        self.tray.setIcon(icon)
        self.status_text = text
        self.apply_status()

        if level is None:
            self.low_battery_notified = False
        elif level <= LOW_BATTERY_PERCENT and not self.low_battery_notified:
            self.tray.showMessage(
                "G733 battery low", f"Battery is at {level}%.", QSystemTrayIcon.MessageIcon.Warning
            )
            self.low_battery_notified = True
        elif level >= LOW_BATTERY_RESET_PERCENT:
            self.low_battery_notified = False

        if state == STATE_FULL:
            if not self.full_battery_notified:
                self.tray.showMessage(
                    "G733 fully charged",
                    "The headset has finished charging and can come off the cable.",
                    QSystemTrayIcon.MessageIcon.Information,
                )
                self.full_battery_notified = True
        elif state == STATE_DISCHARGING:
            # Re-armed only once the headset leaves the cable, so a headset left
            # on it, which may top up and finish again, is announced once.
            self.full_battery_notified = False

    def show_headset_unavailable(self) -> None:
        """Show an offline headset as an expected idle state, not a fault."""
        self.voltages.clear()
        self.last_error = None
        self.tray.setIcon(icon_for(None))
        self.status_text = "G733 headset is off or out of range"
        self.apply_status()

    def show_error(self, message: str) -> None:
        # Deduplicated because a persistent fault would otherwise write one line
        # per poll; the tooltip always shows the current message regardless.
        if message != self.last_error:
            LOGGER.error("%s", message)
            self.last_error = message
        # The headset may have been off for hours; its old voltages no longer
        # describe the battery.
        self.voltages.clear()
        self.tray.setIcon(icon_for(None, is_error=True))
        self.status_text = message
        self.apply_status()

    def apply_status(self) -> None:
        """Show the current battery line, and any lights fault below it."""
        self.status_action.setText(self.status_text)
        self.lights_status_action.setText(self.lights_fault or "")
        self.lights_status_action.setVisible(self.lights_fault is not None)
        lines = [self.status_text]
        if self.polling_paused:
            lines.append("Battery polling paused while no audio is playing through the G733")
        if self.lights_fault is not None:
            lines.append(self.lights_fault)
        self.tray.setToolTip("\n".join(lines))

    def on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.refresh(manual=True)

    def quit(self) -> None:
        """Stop timers and any in-progress request before exiting cleanly."""
        self.quitting = True
        self.poll_timer.stop()
        self.timeout.stop()
        self.audio_timeout.stop()
        self.lights_timeout.stop()
        for process in (self.process, self.audio_process, self.lights_process):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                # Reaped before the object goes with the event loop; Qt aborts
                # when a QProcess is destroyed while its child still runs.
                process.waitForFinished(1000)
        self.tray.hide()
        QApplication.quit()


def main() -> int:
    interval = parse_arguments()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Qt's event loop blocks in C++ with the GIL released, so Python never sees
    # SIGINT while idle; PyQt6 then aborts once the exception escapes a slot.
    # The default handler makes Ctrl-C terminate immediately and quietly.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    try:
        receiver_found = wait_for_receiver()
    except HeadsetError as exc:
        print(f"Could not inspect the G733 receiver: {exc}", file=sys.stderr)
        return 1
    if not receiver_found:
        print("G733 receiver did not appear within 30 seconds; exiting.", file=sys.stderr)
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName("G733 Battery")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray is available in this desktop session.", file=sys.stderr)
        return 1
    monitor = G733Tray(interval)
    monitor.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
