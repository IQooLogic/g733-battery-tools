#!/usr/bin/env bash
# Print one JSON battery reading every POLL_SECONDS seconds. Stop with Ctrl-C.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
poll_seconds="${POLL_SECONDS:-60}"

if [[ ! "$poll_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "POLL_SECONDS must be a positive whole number; got: $poll_seconds" >&2
  exit 2
fi

while true; do
  # A failed reading has already printed its reason; keep watching, since the
  # headset may simply be off or out of range for a while.
  if ! "$script_dir/battery-status.sh"; then
    echo "reading failed; retrying in ${poll_seconds}s" >&2
  fi
  sleep "$poll_seconds"
done
