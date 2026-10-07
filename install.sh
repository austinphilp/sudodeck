#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
mkdir -p "$bin_dir"
chmod 700 "$bin_dir"
install -m 700 "$root/scriptdeck.py" "$bin_dir/scriptdeck"
echo "Installed $bin_dir/scriptdeck"
echo "Add $bin_dir to PATH if needed. Existing queues were not changed."
