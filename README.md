# ScriptDeck

ScriptDeck is a small, private queue for privileged/admin shell scripts that deserve a deliberate review before they run. It copies a hash-verified payload into a per-user directory, lets you inspect or ask about it, and requires an explicit user choice for every execution.

It is a local CLI: it does not upload scripts or logs, create users, require root, or change security configuration.

## Install

Prerequisites: POSIX shell, Python 3.10+, and common Unix utilities. Clone a specific revision, inspect it, then run the local installer—do not pipe remote code into a shell.

```sh
git clone https://github.com/austinphilp/scriptdeck.git
cd scriptdeck
git checkout <reviewed-tag-or-commit>
./install.sh
```

The installer places `scriptdeck` in `~/.local/bin` and data in `~/.local/share/scriptdeck`. Ensure `~/.local/bin` is on your `PATH`. `./uninstall.sh` removes only the command and deliberately preserves queue data.

## Quick start

```sh
scriptdeck add ./examples/hello.sh \
  --title 'Greeting example' \
  --summary 'Prints a harmless greeting' \
  --affects 'Terminal output only' \
  --risks 'None beyond printing text'
scriptdeck review
```

Each item requires `Title`, `Summary`, `Affects`, and `Risks` notes. `review` offers view, optional Codex question, run, skip, and quit. Pressing `r` is the execution choice; there is no second confirmation prompt. Store literal execution arguments with `--arg=VALUE`.

After an item has a durable run record—whether it succeeded, failed, was interrupted, or was rejected—it is hidden from the default pending queue. Its logs and result history remain available through `scriptdeck results [ID]`. Use `scriptdeck list --include-ran` to see completed items and their final statuses. A rerun is deliberate: start `scriptdeck review --include-ran`, then choose `r` for the completed item. An item whose durable record is still `running` remains visible and cannot be started again.

ScriptDeck always calculates and stores a SHA-256 digest internally for tamper checks. You do not need to supply one. Pass `--sha256 DIGEST` only when you want `add` to reject a source file that does not match a separately reviewed digest. Hashes are hidden in the normal human UI; use `scriptdeck list --show-sha256` or `scriptdeck review --show-sha256` to display them.

## Safety model and limits

ScriptDeck never runs a script on `add`. It copies requested bytes, records SHA-256, rejects symlinked payloads and metadata, serializes reviews with a lock, rechecks bytes, and executes a post-approval snapshot. Each run saves ID, hash, start/end, status, exit code, and stdout/stderr paths.

Run data lives under `~/.local/share/scriptdeck/runs/` in private directories. Output is not redacted and can contain secrets; do not upload or share logs blindly. Terminal control characters are made visible during live display, while raw output remains in the log.

ScriptDeck itself stays unprivileged. Scripts request their own scoped interactive `sudo` through `/dev/tty`; scripts that require passwords from stdin are incompatible by design. ScriptDeck does not provide a privileged runner, sudoers changes, or auto-approval. It protects against accidental changes and cooperating concurrent processes, not a malicious process running as the same Unix account.

## Q&A backends

At the `a` review action, choose a backend or press Enter for the saved default. Q&A receives only the queued bytes and your question; its output is display-only data and never selects `r`, adds a script, or changes queue state. Missing CLI/authentication and failed isolation checks leave normal review usable.

| Backend | Status | Isolation used |
| --- | --- | --- |
| Codex CLI | Supported | Ephemeral read-only sandbox; every configured MCP server is disabled and verified disabled; apps, plugins, hooks, browser, and computer integrations are disabled. |
| Pi coding agent | Supported on Pi 0.85+ | Noninteractive/no-session mode with all tools, extensions, skills, prompt templates, themes, and context files disabled. |
| Claude Code | Supported on Claude Code 2.1+ | Print/no-session mode with safe mode, restricted mode, strict empty MCP configuration, no tools, no Chrome, and no permission prompts. |
| OpenCode | Deliberately unavailable | OpenCode 2.0.3 exposes noninteractive mode but no verified no-tools/no-MCP isolation. ScriptDeck fails closed and does not start a session. |

These adapters still need their harness's existing authentication and model-service network access. ScriptDeck never installs a harness, logs in, copies credentials, or accepts arbitrary harness flags.

### Default and overrides

Saved non-secret settings live in `~/.config/scriptdeck/config.json` (or `$SCRIPTDECK_CONFIG`):

```sh
scriptdeck config set-qa-backend pi
scriptdeck config set-qa-model anthropic/claude-sonnet
scriptdeck config set-qa-timeout 90
scriptdeck config show
```

Environment variables take precedence over config, which takes precedence over built-in defaults:

```sh
SCRIPTDECK_QA_BACKEND=claude SCRIPTDECK_QA_MODEL=sonnet scriptdeck review
SCRIPTDECK_QA_TIMEOUT_SECONDS=60 scriptdeck review
```

The built-in backend default is `codex`; the built-in timeout is 120 seconds. Do not put credentials in ScriptDeck config or environment examples.

## Agent skill

The portable skill is [`skills/scriptdeck/SKILL.md`](skills/scriptdeck/SKILL.md). For Codex, copy its directory to `~/.agents/skills/scriptdeck/` or a repository's `.agents/skills/scriptdeck/`. For Claude Code, copy it to `~/.claude/skills/scriptdeck/` or `.claude/skills/scriptdeck/`. These locations are documented by [OpenAI](https://developers.openai.com/codex/skills) and [Anthropic](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview).

## Development

Run the isolated integration checks on a Unix host:

```sh
./tests/run.sh
python3 -m py_compile scriptdeck.py
```

Tests execute generated harmless fixtures in temporary directories.
