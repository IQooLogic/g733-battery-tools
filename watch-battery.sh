#!/usr/bin/env bash
# Continuously print JSON battery readings. Stop with Ctrl-C.
set -euo pipefail

headsetcontrol="${HEADSETCONTROL:-$HOME/Downloads/headsetcontrol-x86_64.AppImage}"
poll_seconds="${POLL_SECONDS:-60}"

if [[ ! -x "$headsetcontrol" ]]; then
  echo "HeadsetControl is not executable: $headsetcontrol" >&2
  echo 'Set HEADSETCONTROL=/path/to/headsetcontrol to override the default.' >&2
  exit 1
fi

if [[ ! "$poll_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "POLL_SECONDS must be a positive whole number; got: $poll_seconds" >&2
  exit 2
fi

exec "$headsetcontrol" -f "$poll_seconds" -b -o json
