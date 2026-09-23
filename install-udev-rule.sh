#!/usr/bin/env bash
# Install the udev rule that lets one user, or the members of one group, read
# the G733.
set -euo pipefail

rule_path=/etc/udev/rules.d/99-logitech-g733-hidraw.rules
match='SUBSYSTEM=="hidraw", ATTRS{idVendor}=="046d", ATTRS{idProduct}=="0b1f"'
# SUDO_USER handles accidental execution via sudo.
invoking_user="${SUDO_USER:-$USER}"

usage() {
  cat <<EOF
Usage: $0 [USER]
       $0 --group GROUP [USER...]

Without --group, USER becomes the owner of the G733 node (MODE 0600).
USER defaults to the account that runs this script ($invoking_user).

With --group, GROUP owns the node (MODE 0660) and every member can use the
headset. The group is created if it does not exist, and each USER is added
to it. USER defaults to $invoking_user. New members must log out and back in
before the membership applies.
EOF
}

require_user() {
  if ! id "$1" >/dev/null 2>&1; then
    echo "Cannot find local user: $1" >&2
    exit 1
  fi
}

group=
if [[ "${1:-}" == -h || "${1:-}" == --help ]]; then
  usage
  exit 0
elif [[ "${1:-}" == --group ]]; then
  if [[ -z "${2:-}" ]]; then
    echo '--group needs a group name' >&2
    usage >&2
    exit 2
  fi
  group=$2
  shift 2
  # Keep the name to what groupadd accepts, so it is safe to put in the rule.
  if [[ ! "$group" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
    echo "Invalid group name: $group" >&2
    exit 2
  fi
  users=("$@")
  if (( ${#users[@]} == 0 )); then
    users=("$invoking_user")
  fi
elif [[ "${1:-}" == -* ]]; then
  echo "Unknown option: $1" >&2
  usage >&2
  exit 2
else
  if (( $# > 1 )); then
    echo 'Only one owner is possible; use --group GROUP for several users' >&2
    exit 2
  fi
  users=("${1:-$invoking_user}")
fi

for user in "${users[@]}"; do
  require_user "$user"
done

# Use a direct owner or group rule because some logind/KDE setups do not apply
# uaccess ACLs to hidraw devices.
if [[ -n "$group" ]]; then
  if ! getent group "$group" >/dev/null; then
    echo "Creating group $group"
    sudo groupadd --system -- "$group"
  fi
  added=()
  for user in "${users[@]}"; do
    if id -nG -- "$user" | tr ' ' '\n' | grep -qxF -- "$group"; then
      echo "User $user is already in group $group"
    else
      echo "Adding user $user to group $group"
      sudo usermod -aG "$group" -- "$user"
      added+=("$user")
    fi
  done
  rule="$match, GROUP=\"$group\", MODE=\"0660\""
  echo "Installing $rule_path for group $group"
else
  rule="$match, OWNER=\"${users[0]}\", MODE=\"0600\""
  echo "Installing $rule_path for user ${users[0]}"
fi

printf '%s\n' "$rule" | sudo tee "$rule_path" >/dev/null
sudo udevadm control --reload-rules

# Re-apply the rule to a receiver that is already connected, so the usual case
# needs no replug. This does nothing when the receiver is absent, which is why
# the replug instruction stays below as a fallback.
sudo udevadm trigger --subsystem-match=hidraw --action=add
sudo udevadm settle

echo
printf '%s\n' 'Rule installed and applied to any connected G733.'
if [[ -n "$group" ]] && (( ${#added[@]} > 0 )); then
  printf 'Log out and back in as %s so the new group membership applies.\n' "${added[*]}"
fi
printf '%s\n' 'Run ./check-hidraw.sh and ./battery-status.sh to confirm.'
printf '%s\n' 'If it still reports no permission, unplug and reconnect the receiver (or reboot).'
