#!/usr/bin/env bash
# Print one JSON battery reading from HeadsetControl.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "$script_dir/lib.sh"

headsetcontrol=$(resolve_headsetcontrol) || exit 1

# Avoid a blocked HID request leaving an interactive shell stuck indefinitely.
timeout_seconds="${TIMEOUT_SECONDS:-15}"
if command -v timeout >/dev/null; then
  exec timeout --foreground "$timeout_seconds" "$headsetcontrol" -b -o json
else
  exec "$headsetcontrol" -b -o json
fi
