#!/usr/bin/env bash
# Print one JSON battery reading from HeadsetControl.
set -euo pipefail

headsetcontrol="${HEADSETCONTROL:-$HOME/Downloads/headsetcontrol-x86_64.AppImage}"

if [[ ! -x "$headsetcontrol" ]]; then
  echo "HeadsetControl is not executable: $headsetcontrol" >&2
  echo 'Set HEADSETCONTROL=/path/to/headsetcontrol to override the default.' >&2
  exit 1
fi

# Avoid a blocked HID request leaving an interactive shell stuck indefinitely.
timeout_seconds="${TIMEOUT_SECONDS:-15}"
if command -v timeout >/dev/null; then
  exec timeout --foreground "$timeout_seconds" "$headsetcontrol" -b -o json
else
  exec "$headsetcontrol" -b -o json
fi
