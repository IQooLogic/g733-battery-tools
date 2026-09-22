#!/usr/bin/env bash
# Start the G733 tray monitor from this directory, regardless of the caller's cwd.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$script_dir/g733_battery_tray.py" "$@"
