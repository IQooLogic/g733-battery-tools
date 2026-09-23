#!/usr/bin/env bash
# Print one JSON battery reading from the headset.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
headset="$script_dir/tray/g733_headset.py"

# Avoid a blocked HID request leaving an interactive shell stuck indefinitely.
timeout_seconds="${TIMEOUT_SECONDS:-15}"
if command -v timeout >/dev/null; then
  exec timeout --foreground "$timeout_seconds" "$headset" battery
else
  exec "$headset" battery
fi
