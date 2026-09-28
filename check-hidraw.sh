#!/usr/bin/env bash
# Show HID raw devices and highlight supported Logitech headset interfaces.
set -euo pipefail

found=0
shopt -s nullglob
for device in /dev/hidraw*; do
  properties=$(udevadm info --query=property --name="$device" 2>/dev/null || true)
  vendor=$(grep -m1 '^ID_VENDOR_ID=' <<<"$properties" | cut -d= -f2- || true)
  product=$(grep -m1 '^ID_MODEL_ID=' <<<"$properties" | cut -d= -f2- || true)
  model=$(grep -m1 '^ID_MODEL=' <<<"$properties" | cut -d= -f2- || true)

  printf '%s  vendor=%s product=%s model=%s\n' \
    "$device" "${vendor:-unknown}" "${product:-unknown}" "${model:-unknown}"

  if [[ "$vendor" == 046d && "$product" == 0b1f ]]; then
    found=1
    echo '  ^ Logitech G733 detected; access control:'
    if command -v getfacl >/dev/null; then
      getfacl -p "$device"
    else
      ls -l "$device"
      echo '  (Install the acl package to inspect user ACLs with getfacl.)'
    fi
  elif [[ "$vendor" == 046d && "$product" == 0ac4 ]]; then
    found=1
    echo '  ^ Logitech G535 detected; access control:'
    if command -v getfacl >/dev/null; then
      getfacl -p "$device"
    else
      ls -l "$device"
      echo '  (Install the acl package to inspect user ACLs with getfacl.)'
    fi
  fi
done

if (( ! found )); then
  echo
  echo 'No supported G733 (046d:0b1f) or G535 (046d:0ac4) was found among hidraw devices.'
  echo 'Check that its USB receiver is connected and the headset is on.'
fi
