#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
export SCRIPTDECK_HOME="$work/queue"

printf '#!/bin/sh\necho hello-test\necho stderr-test >&2\n' > "$work/fixture.sh"
chmod 700 "$work/fixture.sh"
hash=$(sha256sum "$work/fixture.sh" | awk '{print $1}')
if python3 "$root/scriptdeck.py" add "$work/fixture.sh" --sha256 "${hash%?}0" --title bad --summary bad --affects bad --risks bad >/dev/null 2>"$work/hash.err"; then exit 1; fi
grep -q 'source hash mismatch' "$work/hash.err"
id=$(python3 "$root/scriptdeck.py" add "$work/fixture.sh" --title 'Harmless fixture' --summary 'Prints test output' --affects 'Temporary test files' --risks 'None')
python3 "$root/scriptdeck.py" list > "$work/list.out"
grep -q 'Harmless fixture' "$work/list.out"
if grep -q "$hash" "$work/list.out"; then exit 1; fi
python3 "$root/scriptdeck.py" list --show-sha256 > "$work/list-hash.out"
grep -q "$hash" "$work/list-hash.out"
printf 's\n' | python3 "$root/scriptdeck.py" review > "$work/skip.out"
grep -q 'Title: Harmless fixture' "$work/skip.out"
grep -q 'Summary: Prints test output' "$work/skip.out"
grep -q 'Affects: Temporary test files' "$work/skip.out"
grep -q 'Risks: None' "$work/skip.out"
if grep -q "$hash" "$work/skip.out"; then exit 1; fi
test ! -d "$SCRIPTDECK_HOME/runs/$id"
printf 'r\n' | python3 "$root/scriptdeck.py" review > "$work/display.out" 2> "$work/display.err"
if grep -q 'Type RUN' "$work/display.out"; then exit 1; fi
result=$(find "$SCRIPTDECK_HOME/runs/$id" -name result.json -print -quit)
grep -q '"status": "succeeded"' "$result"
grep -q hello-test "$(dirname "$result")/stdout.log"
grep -q stderr-test "$(dirname "$result")/stderr.log"
test "$(sha256sum "$(dirname "$result")/payload.sh" | awk '{print $1}')" = "$hash"

legacy_id=legacy-item
printf '#!/bin/sh\necho legacy\n' > "$SCRIPTDECK_HOME/payloads/$legacy_id.sh"
chmod 700 "$SCRIPTDECK_HOME/payloads/$legacy_id.sh"
printf '{"id":"legacy-item","created_at":"2000-01-01T00:00:00+00:00","description":"Legacy description","sha256":"%s","runs":[]}' "$(sha256sum "$SCRIPTDECK_HOME/payloads/$legacy_id.sh" | awk '{print $1}')" > "$SCRIPTDECK_HOME/metadata/$legacy_id.json"
chmod 600 "$SCRIPTDECK_HOME/metadata/$legacy_id.json"
printf 's\ns\n' | python3 "$root/scriptdeck.py" review > "$work/legacy.out"
grep -q 'Title: Legacy description' "$work/legacy.out"
grep -q 'Affects: Not recorded (legacy item).' "$work/legacy.out"
grep -q 'Risks: Not recorded (legacy item).' "$work/legacy.out"

printf '#!/bin/sh\necho changed\n' > "$SCRIPTDECK_HOME/payloads/$id.sh"
chmod 700 "$SCRIPTDECK_HOME/payloads/$id.sh"
if printf 's\nr\n' | python3 "$root/scriptdeck.py" review > /dev/null 2> "$work/tamper.err"; then exit 1; fi
grep -q 'payload changed for' "$work/tamper.err"

echo 'ScriptDeck tests passed'
