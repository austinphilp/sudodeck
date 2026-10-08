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
printf 'q\n' | python3 "$root/sudodeck.py" review > "$work/review.out"
grep -q 'Title: Harmless fixture' "$work/review.out"
grep -q 'Summary: Prints test output' "$work/review.out"
grep -q 'Affects: Temporary test files' "$work/review.out"
grep -q 'Risks: None' "$work/review.out"
grep -q '\[d\]eny' "$work/review.out"
if grep -q "$hash" "$work/review.out"; then exit 1; fi
test ! -d "$SUDODECK_HOME/runs/$id"

# The built-in Enter behavior is safe. Q&A and closed stdin cannot inherit a run default.
printf '\nq\n' | python3 "$root/sudodeck.py" review > "$work/default-none.out"
grep -q '\[Enter: No action\]' "$work/default-none.out"
grep -q 'No action selected.' "$work/default-none.out"
test ! -d "$SUDODECK_HOME/runs/$id"
python3 "$root/sudodeck.py" config set-default-action run > "$work/default-run-config.out"
grep -q 'Saved review default action: run' "$work/default-run-config.out"
printf 'a\n\n\n' | python3 "$root/sudodeck.py" review > "$work/submenu.out" 2> "$work/submenu.err"
test ! -d "$SUDODECK_HOME/runs/$id"
: | python3 "$root/sudodeck.py" review > "$work/eof.out" 2> "$work/eof.err"
grep -q 'Review input closed; no action taken.' "$work/eof.err"
test ! -d "$SUDODECK_HOME/runs/$id"
SUDODECK_DEFAULT_ACTION=edit python3 "$root/sudodeck.py" config show > "$work/env-precedence.json"
ENV_CONFIG="$work/env-precedence.json" python3 - <<'PY'
import json, os
assert json.load(open(os.environ["ENV_CONFIG"]))["review"]["default_action"] == "edit"
PY
if SUDODECK_DEFAULT_ACTION=invalid python3 "$root/sudodeck.py" config show > /dev/null 2> "$work/invalid-action.err"; then exit 1; fi
grep -q 'invalid default action' "$work/invalid-action.err"
printf '\n' | python3 "$root/sudodeck.py" review > "$work/display.out" 2> "$work/display.err"
grep -q '\[Enter: Run\]' "$work/display.out"
if grep -q 'Type RUN' "$work/display.out"; then exit 1; fi
result=$(find "$SUDODECK_HOME/runs/$id" -name result.json -print -quit)
grep -q '"status": "succeeded"' "$result"
grep -q hello-test "$(dirname "$result")/stdout.log"
grep -q stderr-test "$(dirname "$result")/stderr.log"
test "$(sha256sum "$(dirname "$result")/payload.sh" | awk '{print $1}')" = "$hash"
test "$(find "$SUDODECK_HOME/runs/$id" -name result.json | wc -l)" -eq 1
python3 "$root/sudodeck.py" config set-default-action none >/dev/null
python3 "$root/sudodeck.py" list > "$work/post-run-list.out"
if grep -q 'Harmless fixture' "$work/post-run-list.out"; then exit 1; fi
python3 "$root/sudodeck.py" list --include-ran > "$work/history-list.out"
grep -q '\[SUCCEEDED, exit 0\].*Harmless fixture' "$work/history-list.out"

printf '#!/bin/sh\necho must-not-run\n' > "$work/deny.sh"
chmod 700 "$work/deny.sh"
denied_id=$(python3 "$root/sudodeck.py" add "$work/deny.sh" --title 'Denied fixture' --summary 'Must remain unexecuted' --affects 'Temporary test files' --risks 'None')
printf 'd\nnot approved\n' | python3 "$root/sudodeck.py" review > "$work/deny.out"
grep -q 'Denied; the queued revision was not executed' "$work/deny.out"
test ! -d "$SUDODECK_HOME/runs/$denied_id"
python3 "$root/sudodeck.py" list > "$work/denied-default-list.out"
if grep -q 'Denied fixture' "$work/denied-default-list.out"; then exit 1; fi
printf 'q\n' | python3 "$root/sudodeck.py" review > "$work/denied-default-review.out"
if grep -q 'Denied fixture' "$work/denied-default-review.out"; then exit 1; fi
python3 "$root/sudodeck.py" list --include-denied > "$work/denied-list.out"
grep -q '\[DENIED\].*Denied fixture' "$work/denied-list.out"
python3 "$root/sudodeck.py" list --include-denied --json > "$work/denied-list.json"
python3 "$root/sudodeck.py" results "$denied_id" > "$work/denied-results.json"
python3 "$root/sudodeck.py" history "$denied_id" > "$work/denied-history.json"
DENIED_LIST="$work/denied-list.json" DENIED_RESULTS="$work/denied-results.json" DENIED_HISTORY="$work/denied-history.json" python3 - <<'PY'
import json, os
for path in (os.environ["DENIED_LIST"], os.environ["DENIED_RESULTS"], os.environ["DENIED_HISTORY"]):
    value = json.load(open(path))
    text = json.dumps(value)
    assert "not approved" in text and "denial_id" in text
PY
printf 'c\nRECONSIDER\nq\n' | python3 "$root/sudodeck.py" review --include-denied > "$work/reconsider.out"
grep -q 'Denial retained in history' "$work/reconsider.out"
python3 "$root/sudodeck.py" list > "$work/reconsider-list.out"
grep -q 'Denied fixture' "$work/reconsider-list.out"
printf 'd\nkept denied\n' | python3 "$root/sudodeck.py" review > "$work/redeny.out"

blank_id=$(python3 "$root/sudodeck.py" add "$work/deny.sh" --title 'Blank denial fixture' --summary 'Must remain unexecuted' --affects 'Temporary test files' --risks 'None')
printf 'd\n\n' | python3 "$root/sudodeck.py" review > "$work/blank-deny.out"
python3 "$root/sudodeck.py" history "$blank_id" > "$work/blank-deny.json"
BLANK_DENY="$work/blank-deny.json" python3 - <<'PY'
import json, os
assert json.load(open(os.environ["BLANK_DENY"]))["items"][0]["denials"][0]["reason"] == ""
PY
test ! -d "$SUDODECK_HOME/runs/$blank_id"

legacy_id=legacy-item
printf '#!/bin/sh\necho legacy\n' > "$SUDODECK_HOME/payloads/$legacy_id.sh"
chmod 700 "$SUDODECK_HOME/payloads/$legacy_id.sh"
printf '{"id":"legacy-item","created_at":"2000-01-01T00:00:00+00:00","description":"Legacy description","sha256":"%s","runs":[]}' "$(sha256sum "$SUDODECK_HOME/payloads/$legacy_id.sh" | awk '{print $1}')" > "$SUDODECK_HOME/metadata/$legacy_id.json"
chmod 600 "$SUDODECK_HOME/metadata/$legacy_id.json"
printf 'q\n' | python3 "$root/sudodeck.py" review > "$work/legacy.out"
grep -q 'Title: Legacy description' "$work/legacy.out"
grep -q 'Affects: Not recorded (legacy item).' "$work/legacy.out"
grep -q 'Risks: Not recorded (legacy item).' "$work/legacy.out"
python3 "$root/sudodeck.py" list > "$work/legacy-pending.out"
grep -q 'Legacy description' "$work/legacy-pending.out"

printf '#!/bin/sh\nexit 7\n' > "$work/failure.sh"
chmod 700 "$work/failure.sh"
failure_id=$(python3 "$root/sudodeck.py" add "$work/failure.sh" --title 'Failure fixture' --summary 'Exits 7' --affects 'Temporary test files' --risks 'Expected test failure')
printf 'd\nlegacy not selected\nr\n' | python3 "$root/sudodeck.py" review > "$work/failure.out" 2> "$work/failure.err"
failure_result=$(find "$SUDODECK_HOME/runs/$failure_id" -name result.json -print -quit)
grep -q '"status": "failed"' "$failure_result"
grep -q '"exit_code": 7' "$failure_result"
python3 "$root/sudodeck.py" list > "$work/pending-after-failure.out"
if grep -q 'Failure fixture' "$work/pending-after-failure.out"; then exit 1; fi
python3 "$root/sudodeck.py" list --include-ran > "$work/history-after-failure.out"
grep -q '\[FAILED, exit 7\].*Failure fixture' "$work/history-after-failure.out"
printf 'q\n' | python3 "$root/sudodeck.py" review --include-ran > "$work/rerun-menu.out"
grep -q '\[r\]erun' "$work/rerun-menu.out"

printf '#!/bin/sh\necho changed\n' > "$SUDODECK_HOME/payloads/$id.sh"
chmod 700 "$SUDODECK_HOME/payloads/$id.sh"
if printf 'r\n' | python3 "$root/sudodeck.py" review --include-ran > /dev/null 2> "$work/tamper.err"; then exit 1; fi
grep -q 'payload changed for' "$work/tamper.err"

python3 tests/qa_adapters.py
python3 tests/edit_review.py

echo 'SudoDeck tests passed'
