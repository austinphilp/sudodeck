#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
mkdir -p "$bin_dir"
install -m 700 "$root/sudodeck.py" "$bin_dir/sudodeck"
ln -sfn sudodeck "$bin_dir/scriptdeck"
echo "Installed $bin_dir/sudodeck (scriptdeck compatibility alias created)"
echo "Add $bin_dir to PATH if needed. Existing queues were not changed."
