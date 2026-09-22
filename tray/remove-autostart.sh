#!/usr/bin/env bash
set -euo pipefail

file="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/g733-battery-tray.desktop"
if [[ -e "$file" ]]; then
  rm -- "$file"
  echo "Removed $file"
else
  echo "No autostart entry found."
fi
