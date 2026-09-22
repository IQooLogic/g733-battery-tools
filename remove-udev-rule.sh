#!/usr/bin/env bash
# Remove the udev rule installed by install-udev-rule.sh.
set -euo pipefail

rule_path=/etc/udev/rules.d/99-logitech-g733-hidraw.rules

if [[ -e "$rule_path" ]]; then
  sudo rm -- "$rule_path"
  sudo udevadm control --reload-rules
  echo "Removed $rule_path. Reconnect the receiver to apply the change."
else
  echo "No rule found at $rule_path; nothing to remove."
fi
