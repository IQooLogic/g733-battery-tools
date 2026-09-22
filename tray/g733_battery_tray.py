#!/usr/bin/env python3
"""KDE system-tray battery monitor for a Logitech G733 headset."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sys
from pathlib import Path

from PyQt6.QtCore import QProcess, QTimer, Qt
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

DEFAULT_COMMAND = Path.home() / "Downloads" / "headsetcontrol-x86_64.AppImage"
LOW_BATTERY_PERCENT = 20
LOW_BATTERY_RESET_PERCENT = 25
MINIMUM_INTERVAL_SECONDS = 5

# HeadsetControl battery states this monitor can display. Anything else is an
# error: the headset is off, out of range, or the receiver failed to answer.
STATUS_AVAILABLE = "BATTERY_AVAILABLE"
STATUS_CHARGING = "BATTERY_CHARGING"

USAGE = "Usage: g733_battery_tray.py [--command PATH] [--interval SECONDS]"

LOGGER = logging.getLogger("g733-battery-tray")

# A lightning bolt drawn inside the 64x64 icon, used while the headset charges
# without reporting a usable percentage. Drawn as a path rather than a glyph so
# it does not depend on the font having one.
BOLT_POINTS = ((38, 10), (23, 37), (32, 37), (27, 55), (43, 27), (34, 27))


def parse_interval(value: str, source: str) -> int:
    """Convert one interval value, reporting the option or variable it came from."""
    try:
        return int(value)
    except ValueError:
        print(f"{source} requires a positive whole number; got: {value!r}", file=sys.stderr)
        raise SystemExit(2) from None


def parse_arguments() -> tuple[str, int]:
    """Return HeadsetControl path and polling interval from a small CLI."""
    arguments = sys.argv[1:]
    # Handled before anything else so --help still works with a bad environment.
    if {"-h", "--help"}.intersection(arguments):
        print(USAGE)
        raise SystemExit(0)

    command = os.environ.get("HEADSETCONTROL", str(DEFAULT_COMMAND))
    interval = parse_interval(os.environ.get("POLL_SECONDS", "60"), "POLL_SECONDS")

    remaining = iter(arguments)
    for argument in remaining:
        if argument == "--command":
            command = next(remaining, "")
        elif argument == "--interval":
            interval = parse_interval(next(remaining, ""), "--interval")
        else:
            print(f"Unknown argument: {argument}", file=sys.stderr)
            raise SystemExit(2)

    if not command:
        print("HeadsetControl command cannot be empty", file=sys.stderr)
        raise SystemExit(2)
    if interval < MINIMUM_INTERVAL_SECONDS:
        print(
            f"Polling interval must be at least {MINIMUM_INTERVAL_SECONDS} seconds",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return command, interval


def format_duration(minutes: object, suffix: str) -> str:
    """Render a HeadsetControl minute count, which is absent or -1 when unknown."""
    if not isinstance(minutes, int) or minutes <= 0:
        return ""
    return f" · about {minutes // 60}h {minutes % 60}m {suffix}"


def icon_for(level: int | None, *, is_error: bool = False, is_charging: bool = False) -> QIcon:
    """Create a compact numeric icon that remains legible in Plasma's tray."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    if is_error:
        color, label = QColor("#7f8c8d"), "?"
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
        font.setPointSize(23 if len(label) < 3 else 17)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, label)
    else:
        bolt = QPainterPath()
        bolt.moveTo(*BOLT_POINTS[0])
        for point in BOLT_POINTS[1:]:
            bolt.lineTo(*point)
        bolt.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("white"))
        painter.drawPath(bolt)

    painter.end()
    return QIcon(pixmap)


class G733Tray:
    def __init__(self, command: str, interval_seconds: int) -> None:
        self.command = command
        self.interval_ms = interval_seconds * 1000
        self.in_flight = False
        self.low_battery_notified = False
        self.last_error: str | None = None

        self.tray = QSystemTrayIcon(icon_for(None), QApplication.instance())
        self.tray.setToolTip("G733 battery: waiting for first reading")
        self.tray.activated.connect(self.on_activated)

        self.menu = QMenu()
        self.status_action = QAction("G733: waiting for first reading", self.menu)
        self.status_action.setEnabled(False)
        self.refresh_action = QAction("Refresh now", self.menu)
        self.refresh_action.triggered.connect(self.refresh)
        self.quit_action = QAction("Quit", self.menu)
        self.quit_action.triggered.connect(self.quit)
        self.menu.addAction(self.status_action)
        self.menu.addSeparator()
        self.menu.addAction(self.refresh_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.tray.setContextMenu(self.menu)

        self.process = QProcess()
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)

        self.timeout = QTimer()
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self.process_timed_out)
        self.poll_timer = QTimer()
        self.poll_timer.setSingleShot(True)
        self.poll_timer.timeout.connect(self.refresh)

    def start(self) -> None:
        self.tray.show()
        QTimer.singleShot(0, self.refresh)

    def schedule_next_poll(self) -> None:
        self.poll_timer.start(self.interval_ms)

    def refresh(self) -> None:
        if self.in_flight:
            return
        # Resolved on every poll rather than once at startup, so an AppImage on
        # a volume that is mounted later starts working without a restart.
        # shutil.which accepts both a path and a bare name to look up on PATH.
        executable = shutil.which(self.command)
        if executable is None:
            self.show_error(f"HeadsetControl not found or not executable: {self.command}")
            self.schedule_next_poll()
            return

        self.in_flight = True
        self.refresh_action.setEnabled(False)
        self.status_action.setText("G733: refreshing…")
        self.process.start(executable, ["-b", "-o", "json"])
        self.timeout.start(15_000)

    def finish_request(self) -> None:
        self.timeout.stop()
        self.in_flight = False
        self.refresh_action.setEnabled(True)
        self.schedule_next_poll()

    def process_finished(self, exit_code: int, _exit_status: QProcess.ExitStatus) -> None:
        if not self.in_flight:
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
        self.tray.setToolTip(text)
        self.status_action.setText(text)

        if is_charging:
            self.low_battery_notified = False
        elif level <= LOW_BATTERY_PERCENT and not self.low_battery_notified:
            self.tray.showMessage(
                "G733 battery low", f"Battery is at {level}%.", QSystemTrayIcon.MessageIcon.Warning
            )
            self.low_battery_notified = True
        elif level >= LOW_BATTERY_RESET_PERCENT:
            self.low_battery_notified = False

    def show_error(self, message: str) -> None:
        # Deduplicated because a persistent fault would otherwise write one line
        # per poll; the tooltip always shows the current message regardless.
        if message != self.last_error:
            LOGGER.error("%s", message)
            self.last_error = message
        self.tray.setIcon(icon_for(None, is_error=True))
        self.tray.setToolTip(message)
        self.status_action.setText(message)

    def on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick}:
            self.refresh()

    def quit(self) -> None:
        """Stop timers and any in-progress request before exiting cleanly."""
        self.poll_timer.stop()
        self.timeout.stop()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
        self.tray.hide()
        QApplication.quit()


def main() -> int:
    command, interval = parse_arguments()
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
    monitor = G733Tray(command, interval)
    monitor.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
