#!/usr/bin/env bash
# Print one JSON battery reading from HeadsetControl.
set -euo pipefail

headsetcontrol="${HEADSETCONTROL:-$HOME/Downloads/headsetcontrol-x86_64.AppImage}"

# Accept a bare command name found on PATH as well as a path, so HEADSETCONTROL
# means the same thing here as it does for the tray monitor.
resolved=$headsetcontrol
if [[ "$resolved" != */* ]]; then
  resolved=$(type -P -- "$resolved" || true)
fi

if [[ -z "$resolved" || ! -x "$resolved" ]]; then
  echo "HeadsetControl not found or not executable: $headsetcontrol" >&2
  echo 'Set HEADSETCONTROL to a path or to a command name on PATH.' >&2
  exit 1
fi

# Avoid a blocked HID request leaving an interactive shell stuck indefinitely.
timeout_seconds="${TIMEOUT_SECONDS:-15}"
if command -v timeout >/dev/null; then
  exec timeout --foreground "$timeout_seconds" "$resolved" -b -o json
else
  exec "$resolved" -b -o json
fi
