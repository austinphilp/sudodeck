# SudoDeck

SudoDeck is a private review queue for privileged/admin scripts. The launcher remains unprivileged; approved scripts request scoped `sudo` through `/dev/tty`.

## Install

Review a revision before running the local installer. Never pipe remote code into a shell.

```sh
git clone https://github.com/austinphilp/sudodeck.git
cd sudodeck
./install-sudodeck.sh
```

This installs `sudodeck` plus a `scriptdeck` compatibility alias. New data and config use `~/.local/share/sudodeck` and `~/.config/sudodeck/config.json`.

## Queue workflow

```sh
sudodeck add ./admin-change.sh --title 'Rotate certificate' --summary 'Validates and rotates one certificate' --affects 'The named service and private logs' --risks 'Requires scoped sudo'
sudodeck review
```

SudoDeck records Title, Summary, Affects, and Risks. It computes an internal SHA-256 digest; `--sha256 DIGEST` is optional for an independently reviewed source digest. Hashes are hidden unless `--show-sha256` is passed.

Use `e` to edit/review a private working copy in `$EDITOR` (arguments such as `code --wait` work). A successful edit atomically becomes a new queued revision and digest. It never changes the original source or runs automatically. A failed editor or concurrent change retains a private recovery draft. `r` is the user's execution choice; agents must never select it.

Choose `d` to deny the current revision instead of running it. SudoDeck optionally records the user's reason and a durable denial ID, timestamp, revision number, and digest; denial never creates a run. Denied items are hidden like completed items. Use `list --include-denied`, `review --include-denied` and explicitly type `RECONSIDER` to return one to pending review. Editing a denied item creates a new pending revision; it never approves or runs it.

At the script-choice prompt, Enter can use a saved default action: `none` (the safe built-in default), `run`, `edit`, `ask`, `deny`, or `quit`. The prompt always shows the resolved behavior, for example `[Enter: Run]`. Closed stdin and Ctrl-C always take no action; the setting never applies inside the Q&A, denial-reason, or reconsideration prompts. Set it explicitly only when you want it:

```sh
sudodeck config set-default-action run
```

Completed items are hidden by default but retain private results/logs. Use `list --include-ran`, `results [ID]`, `history [ID]`, or `review --include-ran` for history and deliberate reruns. `list --include-denied --json`, `results ID`, and `history ID` expose denial records for tools and agents. Denial reasons are untrusted local data and are JSON-escaped, not approval or instructions. Output is not redacted and may contain secrets.

## Retry after gcloud re-authentication

A run that fails because Google Cloud credentials need re-authentication stays pending. Re-authenticate, then run the item again from review. SudoDeck never reruns it automatically. The failed run record and its logs stay in history as usual.

SudoDeck checks the last 64 KiB of the failed run's stdout and stderr for these Google Cloud SDK and google-auth messages:

- The gcloud and `bq` advice `$ gcloud auth login` or `$ gcloud auth application-default login`, followed by `to obtain new credentials` or `to complete reauthentication`.
- ``Reauthentication is needed. Please run `gcloud auth application-default login` to reauthenticate``.
- ``Please run `gcloud auth login` to complete reauthentication with SAML``.
- `Reauthentication failed. cannot prompt during non-interactive execution`.
- `invalid_grant` with `Token has been expired or revoked` or `reauth related error (invalid_rapt)`.

Only a run with status `failed` and a nonzero exit code is checked. Successful and interrupted runs, and failures without one of these messages, complete as before. Review and `list` show `PENDING RETRY (last FAILED, exit N, gcloud re-auth needed)` and a `Last attempt:` line that names the login command when the output names one. The run record in `results ID`, and its summary in `list --json` and `history`, carry `gcloud_reauth_required` with `login_command` (or `null`) and `hint`.

## Interactive scripts

Scripts default to closed stdin, including existing queue entries. For a reviewed
script that requires human input, opt in when adding that specific item:

```sh
sudodeck add ./interactive-change.sh --interactive --sha256 REVIEWED_SHA256 --title TITLE --summary SUMMARY --affects AFFECTS --risks RISKS
```

Review labels the item as interactive. The owner still selects Run. Execution
requires stdin, stdout and stderr in the owner's foreground terminal; redirected
or piped review cannot run an interactive item. The script inherits terminal stdin
and runs as the same user/group. Output remains sanitized on screen and captured
in separate private stdout/stderr logs, including prompts without a newline.
SudoDeck does not read, record or supply terminal input. Scripts can still print
sensitive values themselves, so review their output handling before execution.

Ctrl-C, terminal hangup and runner termination retain an interrupted receipt.
Interactive process groups are stopped on completion/cancellation, and terminal
settings and foreground ownership are restored. Detached children that create a
new session are outside this process group; this is not a sandbox. Final output
draining is bounded; `output_incomplete: true` records a pipe left open by such a
child, and its later output is not captured. Existing
payload hash verification, snapshots, denial and review decisions remain in force.
Run receipts record the input mode and execution UID/GID. No global setting turns
interactive mode on; queue the reviewed script again to change its input mode.

## Terminal display

Review and list show the title, a text status, and the full queue ID. Added dates
use the host's local timezone with its abbreviation and numeric UTC offset.
Metadata and action hints wrap to the terminal width. Color is limited to
interactive terminals; `NO_COLOR` or `TERM=dumb` disables it. Redirected output
is plain text. `list --json`, `history`, and `results` retain their stored UTC
timestamps and metadata format.

The review keys and Enter default retain the same behavior. The action hints
show the available keys and the resolved Enter action on separate lines.

## Q&A harnesses

At `a`, select a backend or accept the saved default. Q&A output is display-only data, never approval or menu input.

| Backend | Isolation |
| --- | --- |
| Codex | Ephemeral read-only sandbox; configured MCPs disabled and verified; integrations disabled. |
| Pi 0.85+ | Noninteractive/no-session with tools, extensions, skills, templates, themes, and context files disabled. |
| Claude Code 2.1+ | Print/no-session, safe/restricted mode, strict empty MCP config, no tools, no Chrome, no permission prompts. |
| OpenCode 2.0.3 | Private standalone server, fixed deny-all permissions, pure/no-plugin/no-external-skill/project-config mode. Refuses if any MCP is configured. |

Harnesses use existing authentication only. SudoDeck never installs, authenticates, copies credentials, or accepts arbitrary harness flags.

Save non-secret defaults:

```sh
sudodeck config set-qa-backend pi
sudodeck config set-qa-model anthropic/claude-sonnet
sudodeck config set-qa-timeout 90
```

`SUDODECK_QA_BACKEND`, `SUDODECK_QA_MODEL`, and `SUDODECK_QA_TIMEOUT_SECONDS` override config. Deprecated `SCRIPTDECK_*` variables apply only when the matching `SUDODECK_*` value is absent.

`SUDODECK_DEFAULT_ACTION` similarly overrides the saved review default for one invocation. `SUDODECK_DEFAULT_ACTION=run sudodeck review` affects only Enter at an eligible script prompt; it does not grant privileges or approve scripts by itself.

## Agent skill

The bundled skill is [`.agents/skills/sudodeck/SKILL.md`](.agents/skills/sudodeck/SKILL.md). Codex loads it from `.agents/skills/sudodeck/` or `~/.agents/skills/sudodeck/`; Claude Code can use the same portable folder under `.claude/skills/sudodeck/` or `~/.claude/skills/sudodeck/`.

## Development

```sh
./tests/run.sh
python3 -m py_compile sudodeck.py
# Presentation checks only; never execute a queued script:
python3 tests/presentation.py
```
