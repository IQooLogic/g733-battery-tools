#!/usr/bin/env python3
"""KDE system-tray battery monitor for a Logitech G733 headset."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sys
import time
from functools import partial
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

DEFAULT_APPIMAGE = Path.home() / "Downloads" / "headsetcontrol-x86_64.AppImage"
# Distributions package HeadsetControl as "headsetcontrol". The AppImage is
# tried first because it is usually the newer build; the package is the
# fallback, so an installed package works with no configuration.
PACKAGED_COMMAND = "headsetcontrol"
DEFAULT_COMMANDS = (str(DEFAULT_APPIMAGE), PACKAGED_COMMAND)
LOW_BATTERY_PERCENT = 20
LOW_BATTERY_RESET_PERCENT = 25
# HeadsetControl has no "full" status, so a full charge is a charging reading
# that has reached 100%. The reset level re-arms the notification only after a
# real discharge, so a headset left on the cable is announced once.
FULL_BATTERY_PERCENT = 100
FULL_BATTERY_RESET_PERCENT = 95
MINIMUM_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_MS = 15_000
# Measured on the G733: for about two seconds after a lights write the headset
# answers battery requests with BATTERY_UNAVAILABLE. A reading taken inside that
# window would show a false "?", so readings are held off and then retried.
LIGHTS_SETTLE_MS = 3_000

# HeadsetControl battery states this monitor can display. Anything else is an
# error: the headset is off, out of range, or the receiver failed to answer.
STATUS_AVAILABLE = "BATTERY_AVAILABLE"
STATUS_CHARGING = "BATTERY_CHARGING"

USAGE = f"""Usage: g733_battery_tray.py [--command PATH] [--interval SECONDS]

PATH may be a path or a command name found on PATH. Without --command or the
HEADSETCONTROL variable, {DEFAULT_APPIMAGE}
is tried first, then the packaged "{PACKAGED_COMMAND}" command, which most
distributions provide (for example: sudo pacman -S headsetcontrol)."""

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


def parse_arguments() -> tuple[tuple[str, ...], int]:
    """Return the HeadsetControl candidates and polling interval from a small CLI."""
    arguments = sys.argv[1:]
    # Handled before anything else so --help still works with a bad environment.
    if {"-h", "--help"}.intersection(arguments):
        print(USAGE)
        raise SystemExit(0)

    # An empty or unset variable means "use the defaults", as in the shell.
    command = os.environ.get("HEADSETCONTROL", "")
    interval = parse_interval(os.environ.get("POLL_SECONDS", "60"), "POLL_SECONDS")

    remaining = iter(arguments)
    for argument in remaining:
        if argument == "--command":
            command = next(remaining, "")
            # An explicit empty value is a mistake, not a request for the default.
            if not command:
                print("--command requires a path or a command name", file=sys.stderr)
                raise SystemExit(2)
        elif argument == "--interval":
            interval = parse_interval(next(remaining, ""), "--interval")
        else:
            print(f"Unknown argument: {argument}", file=sys.stderr)
            raise SystemExit(2)

    # An explicit choice is used on its own; only the default falls back.
    commands = (command,) if command else DEFAULT_COMMANDS
    if interval < MINIMUM_INTERVAL_SECONDS:
        print(
            f"Polling interval must be at least {MINIMUM_INTERVAL_SECONDS} seconds",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return commands, interval


def resolve_command(candidates: tuple[str, ...]) -> str | None:
    """Return the first usable candidate, accepting a bare name found on PATH."""
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved is not None:
            return resolved
    return None


def format_duration(minutes: object, suffix: str) -> str:
    """Render a HeadsetControl minute count, which is absent or -1 when unknown."""
    if not isinstance(minutes, int) or minutes <= 0:
        return ""
    return f" · about {minutes // 60}h {minutes % 60}m {suffix}"


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


def lights_label(on: bool) -> str:
    """Name one lighting state the same way in menu items and messages."""
    return f"Turn lights {'on' if on else 'off'}"


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
    def __init__(self, commands: tuple[str, ...], interval_seconds: int) -> None:
        self.commands = tuple(commands)
        self.interval_ms = interval_seconds * 1000
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
        self.lights_preference = load_lights_preference()
        # True only while the startup restore runs, which the user did not ask
        # for and which must not interleave with the first battery reading.
        self.restoring_lights = False
        # When the headset is expected to answer battery requests again.
        self.lights_settle_until = 0.0

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
        self.refresh_action.triggered.connect(self.refresh)
        # Keyed by the state each item requests, so one code path serves both.
        self.lights_actions = {}
        for on in (True, False):
            action = QAction(lights_label(on), self.menu)
            # Checkable so the menu shows which state is remembered and will be
            # applied at the next startup.
            action.setCheckable(True)
            action.triggered.connect(partial(self.set_lights, on))
            self.lights_actions[on] = action
        self.update_lights_checks()
        self.quit_action = QAction("Quit", self.menu)
        self.quit_action.triggered.connect(self.quit)
        self.menu.addAction(self.status_action)
        self.menu.addSeparator()
        self.menu.addAction(self.refresh_action)
        self.menu.addAction(self.lights_actions[True])
        self.menu.addAction(self.lights_actions[False])
        self.menu.addAction(self.lights_status_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.tray.setContextMenu(self.menu)

        self.process = QProcess()
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)

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
        self.poll_timer.timeout.connect(self.refresh)

    def start(self) -> None:
        self.tray.show()
        if self.lights_preference is None:
            QTimer.singleShot(0, self.refresh)
            return
        # Applied before the first reading rather than beside it: two concurrent
        # HeadsetControl processes would contend for the same HID device. The
        # first reading follows as soon as the restore settles.
        self.restoring_lights = True
        QTimer.singleShot(0, partial(self.set_lights, self.lights_preference))

    def schedule_next_poll(self) -> None:
        # A recent lights write both holds the next reading back and brings it
        # forward, so a reading it spoiled is corrected in seconds, not minutes.
        settling_ms = int((self.lights_settle_until - time.monotonic()) * 1000)
        delay_ms = min(self.interval_ms, settling_ms) if settling_ms > 0 else self.interval_ms
        self.poll_timer.start(delay_ms)

    def refresh(self) -> None:
        if self.in_flight:
            return
        # Resolved on every poll rather than once at startup, so an AppImage on
        # a volume that is mounted later starts working without a restart.
        executable = resolve_command(self.commands)
        if executable is None:
            self.show_error(self.missing_command_message())
            self.schedule_next_poll()
            return

        self.in_flight = True
        self.refresh_action.setEnabled(False)
        self.status_action.setText("G733: refreshing…")
        self.process.start(executable, ["-b", "-o", "json"])
        self.timeout.start(REQUEST_TIMEOUT_MS)

    def missing_command_message(self) -> str:
        """Describe every candidate that was tried, so a typo is visible."""
        tried = " or ".join(self.commands)
        return (
            f"HeadsetControl not found or not executable: {tried}"
            " — install the headsetcontrol package, or set HEADSETCONTROL."
        )

    def set_lights(self, on: bool) -> None:
        """Ask HeadsetControl to switch the headset RGB lighting on or off."""
        # Both items drive one process, so a second press while the first
        # request runs is ignored rather than queued behind it.
        if self.lights_process.state() != QProcess.ProcessState.NotRunning:
            return
        executable = resolve_command(self.commands)
        if executable is None:
            self.report_lights_failure(self.missing_command_message())
            self.settle_lights_request()
            return
        self.lights_request = on
        for action in self.lights_actions.values():
            action.setEnabled(False)
        # Clicking a checkable item ticks it; the tick must keep showing the
        # remembered state until this request succeeds.
        self.update_lights_checks()
        self.lights_actions[on].setText(f"Turning lights {self.lights_state()}…")
        self.lights_process.start(executable, ["-l", "1" if on else "0"])
        self.lights_timeout.start(REQUEST_TIMEOUT_MS)

    def lights_state(self) -> str:
        """Return "on" or "off" for the request that is running."""
        return "on" if self.lights_request else "off"

    def finish_lights_request(self) -> None:
        self.lights_timeout.stop()
        self.lights_settle_until = time.monotonic() + LIGHTS_SETTLE_MS / 1000
        # A poll already counting down could otherwise land inside the window
        # the write just opened and report a battery that is only unreachable.
        if self.poll_timer.isActive():
            self.schedule_next_poll()
        for on, action in self.lights_actions.items():
            action.setEnabled(True)
            action.setText(lights_label(on))
        self.update_lights_checks()

    def settle_lights_request(self) -> None:
        """Start the first battery reading once a startup restore has ended."""
        if not self.restoring_lights:
            return
        self.restoring_lights = False
        self.schedule_next_poll()

    def update_lights_checks(self) -> None:
        """Tick the item whose state is remembered, and only that one."""
        for on, action in self.lights_actions.items():
            action.setChecked(self.lights_preference == on)

    def remember_lights(self, on: bool) -> None:
        self.lights_preference = on
        save_lights_preference(on)
        self.update_lights_checks()

    def lights_finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        if self.quitting:
            return
        error_output = (
            bytes(self.lights_process.readAllStandardError()).decode(errors="replace").strip()
        )
        output = bytes(self.lights_process.readAllStandardOutput()).decode(errors="replace").strip()
        state = self.lights_state()
        self.finish_lights_request()
        if exit_code != 0:
            details = error_output or output or f"command exited with {exit_code}"
            self.report_lights_failure(f"Could not turn the G733 lights {state}: {details}")
        else:
            LOGGER.info("lights turned %s", state)
            # The fault is over, so its explanation goes with it.
            self.lights_fault = None
            self.apply_status()
            # A restore applies what is already stored; only a choice is saved.
            if not self.restoring_lights:
                self.remember_lights(self.lights_request)
        self.settle_lights_request()

    def lights_error(self, error: QProcess.ProcessError) -> None:
        if self.quitting:
            return
        if error == QProcess.ProcessError.FailedToStart:
            self.finish_lights_request()
            self.report_lights_failure(
                f"Could not start HeadsetControl: {self.lights_process.errorString()}"
            )
            self.settle_lights_request()

    def lights_timed_out(self) -> None:
        if self.lights_process.state() == QProcess.ProcessState.NotRunning:
            return
        state = self.lights_state()
        self.lights_process.kill()
        self.finish_lights_request()
        self.report_lights_failure(f"Turning the G733 lights {state} timed out")
        self.settle_lights_request()

    def report_lights_failure(self, message: str) -> None:
        # Notified rather than shown on the icon: the battery reading is still
        # valid, so the tooltip and icon must keep reporting it.
        LOGGER.error("%s", message)
        # Kept in the tooltip and the menu, not on the icon: the icon reports
        # the battery, and that reading is still valid.
        self.lights_fault = message
        self.apply_status()
        # A startup restore raises no notification. The user pressed nothing,
        # and an autostarted monitor commonly begins with the headset off.
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
        try:
            payload = json.loads(output)
            battery = payload["devices"][0]["battery"]
            status = battery.get("status")
            if status not in {STATUS_AVAILABLE, STATUS_CHARGING}:
                raise ValueError(status or "battery unavailable")
            level = battery.get("level")
            if not isinstance(level, int) or not 0 <= level <= 100:
                # A charging headset may report no usable percentage; that is a
                # state to display, not a failure. Any other status must have one.
                if status != STATUS_CHARGING:
                    raise ValueError(status or "battery unavailable")
                level = None
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            details = error_output or str(exc) or f"command exited with {exit_code}"
            self.show_error(f"G733 battery unavailable: {details}")
            return
        self.show_level(level, battery, is_charging=status == STATUS_CHARGING)

    def process_error(self, error: QProcess.ProcessError) -> None:
        if self.quitting:
            return
        if error == QProcess.ProcessError.FailedToStart and self.in_flight:
            self.finish_request()
            self.show_error(f"Could not start HeadsetControl: {self.process.errorString()}")

    def process_timed_out(self) -> None:
        if not self.in_flight:
            return
        self.process.kill()
        self.finish_request()
        self.show_error("G733 battery request timed out")

    def show_level(self, level: int | None, battery: dict, *, is_charging: bool) -> None:
        if is_charging:
            percent = "" if level is None else f" {level}%"
            text = f"G733 battery: charging{percent}"
            text += format_duration(battery.get("time_to_full_min"), "until full")
        else:
            text = f"G733 battery: {level}%"
            text += format_duration(battery.get("time_to_empty_min"), "left")

        if self.last_error is not None:
            LOGGER.info("recovered: %s", text)
            self.last_error = None

        self.tray.setIcon(icon_for(level, is_charging=is_charging))
        self.status_text = text
        self.apply_status()

        if is_charging:
            self.low_battery_notified = False
        elif level <= LOW_BATTERY_PERCENT and not self.low_battery_notified:
            self.tray.showMessage(
                "G733 battery low", f"Battery is at {level}%.", QSystemTrayIcon.MessageIcon.Warning
            )
            self.low_battery_notified = True
        elif level >= LOW_BATTERY_RESET_PERCENT:
            self.low_battery_notified = False

        if is_charging and level is not None and level >= FULL_BATTERY_PERCENT:
            if not self.full_battery_notified:
                self.tray.showMessage(
                    "G733 fully charged",
                    "Battery is at 100%. The headset can come off the cable.",
                    QSystemTrayIcon.MessageIcon.Information,
                )
                self.full_battery_notified = True
        elif not is_charging or (level is not None and level <= FULL_BATTERY_RESET_PERCENT):
            # Re-armed once the headset leaves the cable or drops below full, so
            # the next full charge is announced and a steady 100% is not.
            self.full_battery_notified = False

    def show_error(self, message: str) -> None:
        # Deduplicated because a persistent fault would otherwise write one line
        # per poll; the tooltip always shows the current message regardless.
        if message != self.last_error:
            LOGGER.error("%s", message)
            self.last_error = message
        self.tray.setIcon(icon_for(None, is_error=True))
        self.status_text = message
        self.apply_status()

    def apply_status(self) -> None:
        """Show the current battery line, and any lights fault below it."""
        self.status_action.setText(self.status_text)
        self.lights_status_action.setText(self.lights_fault or "")
        self.lights_status_action.setVisible(self.lights_fault is not None)
        lines = [self.status_text]
        if self.lights_fault is not None:
            lines.append(self.lights_fault)
        self.tray.setToolTip("\n".join(lines))

    def on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.refresh()

    def quit(self) -> None:
        """Stop timers and any in-progress request before exiting cleanly."""
        self.quitting = True
        self.poll_timer.stop()
        self.timeout.stop()
        self.lights_timeout.stop()
        for process in (self.process, self.lights_process):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                # Reaped before the object goes with the event loop; Qt aborts
                # when a QProcess is destroyed while its child still runs.
                process.waitForFinished(1000)
        self.tray.hide()
        QApplication.quit()


def main() -> int:
    commands, interval = parse_arguments()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Qt's event loop blocks in C++ with the GIL released, so Python never sees
    # SIGINT while idle; PyQt6 then aborts once the exception escapes a slot.
    # The default handler makes Ctrl-C terminate immediately and quietly.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = QApplication(sys.argv)
    app.setApplicationName("G733 Battery")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray is available in this desktop session.", file=sys.stderr)
        return 1
    monitor = G733Tray(commands, interval)
    monitor.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
