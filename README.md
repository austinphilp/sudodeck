# ScriptDeck

ScriptDeck is a small, private queue for shell scripts that deserve a deliberate review before they run. It copies a hash-verified payload into a per-user directory, lets you inspect or ask about it, and requires an explicit `RUN` confirmation for every execution.

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

Each item requires `Title`, `Summary`, `Affects`, and `Risks` notes. `review` offers view, optional Codex question, run, skip, and quit. Pressing `r` is the execution choice; there is no second confirmation prompt. Use `scriptdeck list` to inspect items and `scriptdeck results [ID]` to read machine-readable run records. Store literal execution arguments with `--arg=VALUE`.

ScriptDeck always calculates and stores a SHA-256 digest internally for tamper checks. You do not need to supply one. Pass `--sha256 DIGEST` only when you want `add` to reject a source file that does not match a separately reviewed digest. Hashes are hidden in the normal human UI; use `scriptdeck list --show-sha256` or `scriptdeck review --show-sha256` to display them.

## Safety model and limits

ScriptDeck never runs a script on `add`. It copies requested bytes, records SHA-256, rejects symlinked payloads and metadata, serializes reviews with a lock, rechecks bytes, and executes a post-approval snapshot. Each run saves ID, hash, start/end, status, exit code, and stdout/stderr paths.

Run data lives under `~/.local/share/scriptdeck/runs/` in private directories. Output is not redacted and can contain secrets; do not upload or share logs blindly. Terminal control characters are made visible during live display, while raw output remains in the log.

Scripts start with stdin disconnected. Normal interactive `sudo` can still prompt through `/dev/tty`; scripts that require passwords from stdin are incompatible by design. ScriptDeck does not provide a privileged runner, sudoers changes, or auto-approval. It protects against accidental changes and cooperating concurrent processes, not a malicious process running as the same Unix account.

## Codex Q&A

If the Codex CLI is available, ScriptDeck runs an ephemeral noninteractive Q&A session with the queued script and user question only. It uses a read-only sandbox, disables configured MCP servers and app/plugin/hook/browser/computer integrations, and never lets a response alter or execute queue contents. It still needs network access to Codex's model service. If isolation checks fail, normal review remains usable and Q&A reports the failure.

Claude Code and other agents are supported through the bundled portable skill; they do **not** receive an automatic Q&A backend from ScriptDeck.

## Agent skill

The portable skill is [`skills/scriptdeck/SKILL.md`](skills/scriptdeck/SKILL.md). For Codex, copy its directory to `~/.agents/skills/scriptdeck/` or a repository's `.agents/skills/scriptdeck/`. For Claude Code, copy it to `~/.claude/skills/scriptdeck/` or `.claude/skills/scriptdeck/`. These locations are documented by [OpenAI](https://developers.openai.com/codex/skills) and [Anthropic](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview).

## Development

Run the isolated integration checks on a Unix host:

```sh
./tests/run.sh
python3 -m py_compile scriptdeck.py
```

Tests execute generated harmless fixtures in temporary directories.
