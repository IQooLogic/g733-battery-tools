#!/usr/bin/env bash
# Install the udev rule that lets this user read the G733.
set -euo pipefail

rule_path=/etc/udev/rules.d/99-logitech-g733-hidraw.rules
# Use a direct owner rule because some logind/KDE setups do not apply uaccess
# ACLs to hidraw devices. SUDO_USER handles accidental execution via sudo.
target_user="${SUDO_USER:-$USER}"
if ! id "$target_user" >/dev/null 2>&1; then
  echo "Cannot find local user: $target_user" >&2
  exit 1
fi
rule="SUBSYSTEM==\"hidraw\", ATTRS{idVendor}==\"046d\", ATTRS{idProduct}==\"0b1f\", OWNER=\"$target_user\", MODE=\"0600\""

echo "Installing $rule_path for user $target_user"
printf '%s\n' "$rule" | sudo tee "$rule_path" >/dev/null
sudo udevadm control --reload-rules

echo
printf '%s\n' 'Rule installed. Unplug and reconnect the G733 USB receiver (or reboot).'
printf '%s\n' 'Then run ./check-hidraw.sh and ./battery-status.sh.'
