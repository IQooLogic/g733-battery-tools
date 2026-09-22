#!/usr/bin/env bash
# Continuously print JSON battery readings. Stop with Ctrl-C.
set -euo pipefail

headsetcontrol="${HEADSETCONTROL:-$HOME/Downloads/headsetcontrol-x86_64.AppImage}"
poll_seconds="${POLL_SECONDS:-60}"

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

if [[ ! "$poll_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "POLL_SECONDS must be a positive whole number; got: $poll_seconds" >&2
  exit 2
fi

exec "$resolved" -f "$poll_seconds" -b -o json
