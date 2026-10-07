#!/bin/sh
set -eu
bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
target="$bin_dir/scriptdeck"
if [ -e "$target" ] || [ -L "$target" ]; then
  rm -f -- "$target"
  echo "Removed $target"
else
  echo "No ScriptDeck command found at $target"
fi
echo "Queue and log data were preserved."
