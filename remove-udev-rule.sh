#!/usr/bin/env bash
# Remove the udev rule installed by install-udev-rule.sh.
set -euo pipefail

rule_path=/etc/udev/rules.d/99-logitech-g733-hidraw.rules

if [[ -e "$rule_path" ]]; then
  group=$(grep -o 'GROUP="[^"]*"' -- "$rule_path" | cut -d'"' -f2 || true)
  sudo rm -- "$rule_path"
  sudo udevadm control --reload-rules

  # Re-apply the remaining rules to a connected receiver, so the node returns to
  # root ownership now rather than at the next device event. Without this the
  # account keeps access to an already-present device until it is replugged.
  sudo udevadm trigger --subsystem-match=hidraw --action=add
  sudo udevadm settle

  echo "Removed $rule_path and reset access on any connected G733."
  echo 'If the receiver was not connected, the change applies when you plug it in.'
  if [[ -n "$group" ]]; then
    # The group can have other uses, so leave it and its members in place.
    echo "Group $group was kept. Delete it with: sudo groupdel $group"
  fi
else
  echo "No rule found at $rule_path; nothing to remove."
fi
