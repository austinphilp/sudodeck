#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
export SCRIPTDECK_HOME="$work/queue"

printf '#!/bin/sh\necho hello-test\necho stderr-test >&2\n' > "$work/fixture.sh"
chmod 700 "$work/fixture.sh"
hash=$(sha256sum "$work/fixture.sh" | awk '{print $1}')
id=$(python3 "$root/scriptdeck.py" add "$work/fixture.sh" --sha256 "$hash" --description 'Harmless test fixture')
printf 'r\nRUN\n' | python3 "$root/scriptdeck.py" review > "$work/display.out" 2> "$work/display.err"
result=$(find "$SCRIPTDECK_HOME/runs/$id" -name result.json -print -quit)
grep -q '"status": "succeeded"' "$result"
grep -q hello-test "$(dirname "$result")/stdout.log"
grep -q stderr-test "$(dirname "$result")/stderr.log"
test "$(sha256sum "$(dirname "$result")/payload.sh" | awk '{print $1}')" = "$hash"

printf '#!/bin/sh\necho changed\n' > "$SCRIPTDECK_HOME/payloads/$id.sh"
chmod 700 "$SCRIPTDECK_HOME/payloads/$id.sh"
if printf 'r\nRUN\n' | python3 "$root/scriptdeck.py" review > /dev/null 2> "$work/tamper.err"; then exit 1; fi
grep -q 'payload changed for' "$work/tamper.err"

echo 'ScriptDeck tests passed'
