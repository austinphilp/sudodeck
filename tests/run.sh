#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
export SUDODECK_HOME="$work/queue"

printf '#!/bin/sh\necho hello-test\necho stderr-test >&2\n' > "$work/fixture.sh"
chmod 700 "$work/fixture.sh"
hash=$(sha256sum "$work/fixture.sh" | awk '{print $1}')
if python3 "$root/sudodeck.py" add "$work/fixture.sh" --sha256 "${hash%?}0" --title bad --summary bad --affects bad --risks bad >/dev/null 2>"$work/hash.err"; then exit 1; fi
grep -q 'source hash mismatch' "$work/hash.err"
id=$(python3 "$root/sudodeck.py" add "$work/fixture.sh" --title 'Harmless fixture' --summary 'Prints test output' --affects 'Temporary test files' --risks 'None')
python3 "$root/sudodeck.py" list > "$work/list.out"
grep -q 'Harmless fixture' "$work/list.out"
if grep -q "$hash" "$work/list.out"; then exit 1; fi
python3 "$root/sudodeck.py" list --show-sha256 > "$work/list-hash.out"
grep -q "$hash" "$work/list-hash.out"
printf 's\n' | python3 "$root/sudodeck.py" review > "$work/skip.out"
grep -q 'Title: Harmless fixture' "$work/skip.out"
grep -q 'Summary: Prints test output' "$work/skip.out"
grep -q 'Affects: Temporary test files' "$work/skip.out"
grep -q 'Risks: None' "$work/skip.out"
if grep -q "$hash" "$work/skip.out"; then exit 1; fi
test ! -d "$SUDODECK_HOME/runs/$id"
printf 'r\n' | python3 "$root/sudodeck.py" review > "$work/display.out" 2> "$work/display.err"
if grep -q 'Type RUN' "$work/display.out"; then exit 1; fi
result=$(find "$SUDODECK_HOME/runs/$id" -name result.json -print -quit)
grep -q '"status": "succeeded"' "$result"
grep -q hello-test "$(dirname "$result")/stdout.log"
grep -q stderr-test "$(dirname "$result")/stderr.log"
test "$(sha256sum "$(dirname "$result")/payload.sh" | awk '{print $1}')" = "$hash"
python3 "$root/sudodeck.py" list > "$work/post-run-list.out"
if grep -q 'Harmless fixture' "$work/post-run-list.out"; then exit 1; fi
python3 "$root/sudodeck.py" list --include-ran > "$work/history-list.out"
grep -q '\[SUCCEEDED, exit 0\].*Harmless fixture' "$work/history-list.out"

legacy_id=legacy-item
printf '#!/bin/sh\necho legacy\n' > "$SUDODECK_HOME/payloads/$legacy_id.sh"
chmod 700 "$SUDODECK_HOME/payloads/$legacy_id.sh"
printf '{"id":"legacy-item","created_at":"2000-01-01T00:00:00+00:00","description":"Legacy description","sha256":"%s","runs":[]}' "$(sha256sum "$SUDODECK_HOME/payloads/$legacy_id.sh" | awk '{print $1}')" > "$SUDODECK_HOME/metadata/$legacy_id.json"
chmod 600 "$SUDODECK_HOME/metadata/$legacy_id.json"
printf 's\n' | python3 "$root/sudodeck.py" review > "$work/legacy.out"
grep -q 'Title: Legacy description' "$work/legacy.out"
grep -q 'Affects: Not recorded (legacy item).' "$work/legacy.out"
grep -q 'Risks: Not recorded (legacy item).' "$work/legacy.out"
python3 "$root/sudodeck.py" list > "$work/legacy-pending.out"
grep -q 'Legacy description' "$work/legacy-pending.out"

printf '#!/bin/sh\nexit 7\n' > "$work/failure.sh"
chmod 700 "$work/failure.sh"
failure_id=$(python3 "$root/sudodeck.py" add "$work/failure.sh" --title 'Failure fixture' --summary 'Exits 7' --affects 'Temporary test files' --risks 'Expected test failure')
printf 's\nr\n' | python3 "$root/sudodeck.py" review > "$work/failure.out" 2> "$work/failure.err"
failure_result=$(find "$SUDODECK_HOME/runs/$failure_id" -name result.json -print -quit)
grep -q '"status": "failed"' "$failure_result"
grep -q '"exit_code": 7' "$failure_result"
python3 "$root/sudodeck.py" list > "$work/pending-after-failure.out"
if grep -q 'Failure fixture' "$work/pending-after-failure.out"; then exit 1; fi
python3 "$root/sudodeck.py" list --include-ran > "$work/history-after-failure.out"
grep -q '\[FAILED, exit 7\].*Failure fixture' "$work/history-after-failure.out"
printf 's\ns\ns\n' | python3 "$root/sudodeck.py" review --include-ran > "$work/rerun-menu.out"
grep -q '\[r\]erun' "$work/rerun-menu.out"

printf '#!/bin/sh\necho changed\n' > "$SUDODECK_HOME/payloads/$id.sh"
chmod 700 "$SUDODECK_HOME/payloads/$id.sh"
if printf 's\nr\n' | python3 "$root/sudodeck.py" review --include-ran > /dev/null 2> "$work/tamper.err"; then exit 1; fi
grep -q 'payload changed for' "$work/tamper.err"

python3 tests/qa_adapters.py
python3 tests/edit_review.py

echo 'SudoDeck tests passed'
