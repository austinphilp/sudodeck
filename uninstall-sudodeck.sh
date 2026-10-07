#!/bin/sh
set -eu
bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
target="$bin_dir/sudodeck"
if [ -e "$target" ] || [ -L "$target" ]; then
  rm -f -- "$target"
  echo "Removed $target"
else
  echo "No ScriptDeck command found at $target"
fi
if [ "$(readlink "$bin_dir/scriptdeck" 2>/dev/null || true)" = sudodeck ]; then
  rm -f -- "$bin_dir/scriptdeck"
fi
echo "Queue and log data were preserved."
