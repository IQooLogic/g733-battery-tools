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

# Re-apply the rule to a receiver that is already connected, so the usual case
# needs no replug. This does nothing when the receiver is absent, which is why
# the replug instruction stays below as a fallback.
sudo udevadm trigger --subsystem-match=hidraw --action=add
sudo udevadm settle

echo
printf '%s\n' 'Rule installed and applied to any connected G733.'
printf '%s\n' 'Run ./check-hidraw.sh and ./battery-status.sh to confirm.'
printf '%s\n' 'If the battery still reads -1, unplug and reconnect the receiver (or reboot).'
