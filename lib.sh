# Shared HeadsetControl location logic, sourced by the scripts beside it.
# Not executable on its own.

headsetcontrol_appimage="$HOME/Downloads/headsetcontrol-x86_64.AppImage"
headsetcontrol_package=headsetcontrol

# Print the first usable HeadsetControl, or explain what was tried and return 1.
#
# HEADSETCONTROL may be a path or a command name on PATH. When it is unset the
# AppImage is tried first, because it is usually the newer build, and the
# packaged command second, so an installed package works with no configuration.
resolve_headsetcontrol() {
  local candidates candidate resolved

  if [[ -n "${HEADSETCONTROL:-}" ]]; then
    candidates=("$HEADSETCONTROL")
  else
    candidates=("$headsetcontrol_appimage" "$headsetcontrol_package")
  fi

  for candidate in "${candidates[@]}"; do
    if [[ "$candidate" == */* ]]; then
      if [[ -x "$candidate" ]]; then
        printf '%s\n' "$candidate"
        return 0
      fi
    else
      resolved=$(type -P -- "$candidate" || true)
      if [[ -n "$resolved" ]]; then
        printf '%s\n' "$resolved"
        return 0
      fi
    fi
  done

  echo "HeadsetControl not found or not executable: ${candidates[*]}" >&2
  echo 'Install it with your package manager, for example:' >&2
  echo '  sudo pacman -S headsetcontrol' >&2
  echo 'or set HEADSETCONTROL to a path or to a command name on PATH.' >&2
  return 1
}
