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

Completed items are hidden by default but retain private results/logs. Use `list --include-ran`, `results [ID]`, or `review --include-ran` for history and deliberate reruns.

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

## Agent skill

The bundled skill is [`.agents/skills/sudodeck/SKILL.md`](.agents/skills/sudodeck/SKILL.md). Codex loads it from `.agents/skills/sudodeck/` or `~/.agents/skills/sudodeck/`; Claude Code can use the same portable folder under `.claude/skills/sudodeck/` or `~/.claude/skills/sudodeck/`.

## Development

```sh
./tests/run.sh
python3 -m py_compile sudodeck.py
```
