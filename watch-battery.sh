#!/usr/bin/env bash
# Continuously print JSON battery readings. Stop with Ctrl-C.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "$script_dir/lib.sh"

headsetcontrol=$(resolve_headsetcontrol) || exit 1
poll_seconds="${POLL_SECONDS:-60}"

if [[ ! "$poll_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "POLL_SECONDS must be a positive whole number; got: $poll_seconds" >&2
  exit 2
fi

exec "$headsetcontrol" -f "$poll_seconds" -b -o json
