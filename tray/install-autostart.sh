#!/usr/bin/env bash
# Install a per-user KDE/XDG autostart entry for this checkout.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
autostart_dir="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
desktop_file="$autostart_dir/g733-battery-tray.desktop"

mkdir -p -- "$autostart_dir"
cat >"$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Name=G733 Battery
Comment=Show Logitech G733 battery level in the system tray
Exec="$script_dir/start.sh"
Icon=audio-headset
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

echo "Installed $desktop_file"
echo 'It will start automatically at your next Plasma login.'
