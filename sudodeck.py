#!/usr/bin/env python3
"""Private, reviewed script queue for one user.

Queued payloads are copied into a mode-0700 private directory.  Nothing runs
on enqueue; `review` makes each execution decision explicitly.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time

APP = "sudodeck"
DEFAULT_ROOT = pathlib.Path.home() / ".local/share" / APP
DEFAULT_CONFIG = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")) / APP / "config.json"
LEGACY_ROOT = pathlib.Path.home() / ".local/share/scriptdeck"
LEGACY_CONFIG = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")) / "scriptdeck" / "config.json"
ROOT = pathlib.Path(os.environ.get("SUDODECK_HOME", os.environ.get("SCRIPTDECK_HOME", pathlib.Path.home() / ".local/share" / APP)))
PAYLOADS = ROOT / "payloads"
META = ROOT / "metadata"
RUNS = ROOT / "runs"
LOCK = ROOT / ".lock"
CONFIG = pathlib.Path(os.environ.get("SUDODECK_CONFIG", os.environ.get("SCRIPTDECK_CONFIG", DEFAULT_CONFIG)))
BACKENDS = {"codex", "pi", "claude", "opencode"}
DEFAULT_ACTIONS = {"none", "run", "edit", "ask", "deny", "quit"}


class QueueError(RuntimeError):
    pass


class AdapterUnavailable(QueueError):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def checked_config() -> dict:
    if not CONFIG.exists():
        return {}
    checked_file(CONFIG)
    try:
        value = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueueError(f"invalid SudoDeck config: {CONFIG}") from exc
    if not isinstance(value, dict):
        raise QueueError(f"invalid SudoDeck config: {CONFIG}")
    return value


def validate_qa_settings(backend: object, model: object, raw_timeout: object) -> tuple[str, str | None, int]:
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise QueueError(f"unsupported Q&A backend: {backend!r}")
    if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 200 or "\x00" in model):
        raise QueueError("invalid Q&A model setting")
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise QueueError("invalid Q&A timeout setting") from exc
    if not 1 <= timeout <= 300:
        raise QueueError("Q&A timeout must be between 1 and 300 seconds")
    return backend, model, timeout


def qa_settings() -> tuple[str, str | None, int]:
    config = checked_config().get("qa", {})
    if not isinstance(config, dict):
        raise QueueError("invalid qa config")
    return validate_qa_settings(os.environ.get("SUDODECK_QA_BACKEND", os.environ.get("SCRIPTDECK_QA_BACKEND", config.get("backend", "codex"))),
                                os.environ.get("SUDODECK_QA_MODEL", os.environ.get("SCRIPTDECK_QA_MODEL", config.get("model"))),
                                os.environ.get("SUDODECK_QA_TIMEOUT_SECONDS", os.environ.get("SCRIPTDECK_QA_TIMEOUT_SECONDS", config.get("timeout_seconds", 120))))


def save_qa_setting(key: str, value: object) -> None:
    config = checked_config()
    qa = config.setdefault("qa", {})
    if not isinstance(qa, dict):
        raise QueueError("invalid qa config")
    candidate = dict(qa)
    candidate[key] = value
    validate_qa_settings(candidate.get("backend", "codex"), candidate.get("model"), candidate.get("timeout_seconds", 120))
    qa[key] = value
    private_dir(CONFIG.parent)
    atomic_json(CONFIG, config)


def validate_default_action(value: object) -> str:
    if not isinstance(value, str) or value not in DEFAULT_ACTIONS:
        raise QueueError(f"invalid default action; choose one of: {', '.join(sorted(DEFAULT_ACTIONS))}")
    return value


def default_action() -> str:
    config = checked_config().get("review", {})
    if not isinstance(config, dict):
        raise QueueError("invalid review config")
    return validate_default_action(os.environ.get("SUDODECK_DEFAULT_ACTION",
                                   os.environ.get("SCRIPTDECK_DEFAULT_ACTION", config.get("default_action", "none"))))


def save_default_action(value: str) -> None:
    action = validate_default_action(value)
    config = checked_config()
    review_config = config.setdefault("review", {})
    if not isinstance(review_config, dict):
        raise QueueError("invalid review config")
    review_config["default_action"] = action
    private_dir(CONFIG.parent)
    atomic_json(CONFIG, config)


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def private_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise QueueError(f"unsafe queue path: {path}")
    os.chmod(path, 0o700)


def setup() -> None:
    for directory in (ROOT, PAYLOADS, META, RUNS):
        private_dir(directory)


def lock():
    setup()
    import fcntl
    handle = LOCK.open("a+")
    os.chmod(LOCK, 0o600)
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def payload_path(item_id: str) -> pathlib.Path:
    if not item_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in item_id):
        raise QueueError("invalid queue ID")
    return PAYLOADS / f"{item_id}.sh"


def metadata_path(item_id: str) -> pathlib.Path:
    return META / f"{item_id}.json"


def checked_file(path: pathlib.Path) -> pathlib.Path:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise QueueError(f"missing queued payload: {path.name}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise QueueError(f"unsafe queued payload: {path.name}")
    return path


def load(item_id: str) -> dict:
    path = metadata_path(item_id)
    checked_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueueError(f"invalid metadata for {item_id}") from exc
    if value.get("id") != item_id or not isinstance(value.get("sha256"), str):
        raise QueueError(f"invalid metadata for {item_id}")
    return value


def items() -> list[dict]:
    setup()
    result = []
    for path in sorted(META.glob("*.json")):
        if path.is_symlink():
            continue
        try:
            result.append(load(path.stem))
        except QueueError as exc:
            print(f"warning: {exc}", file=sys.stderr)
    return sorted(result, key=lambda item: item["created_at"])


def latest_execution(item: dict) -> dict | None:
    """Return the newest durable result, falling back to metadata after a crash."""
    records = []
    run_root = RUNS / item["id"]
    if run_root.is_dir() and not run_root.is_symlink():
        for path in run_root.glob("*/result.json"):
            try:
                checked_file(path)
                record = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(record, dict) and isinstance(record.get("status"), str):
                    records.append(record)
            except (OSError, json.JSONDecodeError, QueueError):
                continue
    if records:
        return max(records, key=lambda record: record.get("started_at", ""))
    runs = item.get("runs", [])
    if isinstance(runs, list):
        valid = [record for record in runs if isinstance(record, dict) and isinstance(record.get("status"), str)]
        if valid:
            return valid[-1]
    return None


def current_denial(item: dict) -> dict | None:
    """Return the active denial for the currently queued digest, if any."""
    denials = item.get("denials", [])
    if not isinstance(denials, list):
        return None
    for record in reversed(denials):
        if (isinstance(record, dict) and record.get("sha256") == item.get("sha256")
                and isinstance(record.get("denied_at"), str) and not record.get("reconsidered_at")):
            return record
    return None


def revision_number(item: dict) -> int:
    revisions = item.get("revisions", [])
    return len(revisions) if isinstance(revisions, list) else 0


def is_denied(item: dict) -> bool:
    return current_denial(item) is not None


def is_completed(item: dict) -> bool:
    record = latest_execution(item)
    # A successful old run does not complete a subsequently edited payload.
    return bool(record and record.get("status") != "running" and record.get("sha256") == item.get("sha256"))


def execution_label(item: dict) -> str:
    denial = current_denial(item)
    if denial:
        return "DENIED"
    record = latest_execution(item)
    if not record:
        return "PENDING"
    status = record["status"].upper()
    code = record.get("exit_code")
    suffix = f", exit {code}" if code is not None else ""
    if record.get("sha256") != item.get("sha256"):
        return f"PENDING REVISION (last {status}{suffix})"
    return f"{status}{suffix}"


def verify(item: dict) -> pathlib.Path:
    payload = checked_file(payload_path(item["id"]))
    actual = sha256(payload)
    if actual != item["sha256"]:
        raise QueueError(f"payload changed for {item['id']}; expected {item['sha256']}, got {actual}")
    return payload


def atomic_json(path: pathlib.Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def enqueue(args: argparse.Namespace) -> int:
    source = pathlib.Path(args.path).expanduser().resolve(strict=True)
    checked_file(source)
    if source.stat().st_size > 5 * 1024 * 1024:
        raise QueueError("refusing payload larger than 5 MiB")
    actual = sha256(source)
    if args.sha256 and actual != args.sha256.lower():
        raise QueueError(f"source hash mismatch; expected {args.sha256.lower()}, got {actual}")
    with lock():
        item_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
        target = payload_path(item_id)
        shutil.copyfile(source, target)
        os.chmod(target, 0o700)
        if sha256(target) != actual:
            target.unlink(missing_ok=True)
            raise QueueError("copy verification failed")
        record = {"id": item_id, "created_at": now(), "title": args.title,
                  "summary": args.summary, "affects": args.affects, "risks": args.risks,
                  "source": str(source), "sha256": actual, "kind": "shell", "arguments": args.arg, "runs": []}
        atomic_json(metadata_path(item_id), record)
    print(item_id)
    return 0


def fields(item: dict) -> tuple[str, str, str, str]:
    legacy = item.get("description")
    title = item.get("title") or legacy or "Untitled legacy item"
    summary = item.get("summary") or (legacy if legacy else "Not recorded (legacy item).")
    affects = item.get("affects") or "Not recorded (legacy item)."
    risks = item.get("risks") or "Not recorded (legacy item)."
    return title, summary, affects, risks


def describe(item: dict, show_hash: bool = False) -> str:
    title, summary, affects, risks = fields(item)
    details = f"ID: {item['id']}\nAdded: {item['created_at']}\nTitle: {title}\nSummary: {summary}\nAffects: {affects}\nRisks: {risks}\nArguments: {item.get('arguments', [])}"
    if show_hash:
        details += f"\nSHA256: {item['sha256']}"
    denial = current_denial(item)
    if denial:
        # JSON encoding keeps pasted terminal controls in untrusted reasons inert.
        details += f"\nDenied: {denial['denied_at']} (revision {denial.get('revision', 0)})\nReason: {json.dumps(denial.get('reason', ''))}"
    return details


def show(item: dict, show_hash: bool = False) -> None:
    payload = verify(item)
    print(describe(item, show_hash) + "\n")
    print(payload.read_text(encoding="utf-8", errors="replace"), end="")


def edit_review(item: dict) -> None:
    """Edit a private copy and atomically promote it only if the queued bytes stay current."""
    payload = verify(item)
    original_hash = item["sha256"]
    editor_text = os.environ.get("EDITOR") or ("vi" if shutil.which("vi") else "nano" if shutil.which("nano") else "")
    if not editor_text:
        print("No editor available; set EDITOR to an editor command (for example: code --wait).", file=sys.stderr)
        return
    try:
        editor = shlex.split(editor_text)
    except ValueError as exc:
        print(f"Invalid EDITOR setting: {exc}", file=sys.stderr)
        return
    if not editor:
        print("Invalid EDITOR setting.", file=sys.stderr)
        return
    fd, temporary_name = tempfile.mkstemp(prefix=f".edit-{item['id']}-", suffix=".sh", dir=PAYLOADS)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle, payload.open("rb") as source:
            shutil.copyfileobj(source, handle)
        os.chmod(temporary, 0o600)
        completed = subprocess.run([*editor, str(temporary)], stdin=subprocess.DEVNULL)
        if completed.returncode:
            print(f"Editor exited {completed.returncode}; recoverable draft retained at {temporary}", file=sys.stderr)
            return
        checked_file(temporary)
        revised_hash = sha256(temporary)
        if revised_hash == original_hash:
            temporary.unlink(missing_ok=True)
            print("No queued changes saved.")
            return
        if sha256(checked_file(payload)) != original_hash or load(item["id"])["sha256"] != original_hash:
            print(f"Queue changed concurrently; recoverable draft retained at {temporary}", file=sys.stderr)
            return
        revisions = item.setdefault("revisions", [])
        if not isinstance(revisions, list):
            raise QueueError("invalid revisions metadata")
        os.chmod(temporary, 0o700)
        os.replace(temporary, payload)
        item["sha256"] = revised_hash
        revisions.append({"sha256": revised_hash, "edited_at": now()})
        atomic_json(metadata_path(item["id"]), item)
        print("Saved a new queued revision. Review or Q&A now uses the edited bytes; nothing was executed.")
    except (OSError, QueueError) as exc:
        print(f"Edit was not saved ({exc}); recoverable draft retained at {temporary}", file=sys.stderr)


def qa_prompt(payload: pathlib.Path, question: str) -> str:
    return textwrap.dedent(f"""\
        You are reviewing one queued shell script. Answer the question using only
        the script below. Do not run commands, edit files, use tools, recommend
        bypassing review, or provide instructions that execute this script.
        State uncertainty plainly.\n\nQuestion: {question}\n\nScript:\n{payload.read_text(encoding='utf-8', errors='replace')}""")


def codex_command(codex: str, model: str | None) -> list[str]:
    listed = subprocess.run([codex, "mcp", "list", "--json"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, check=True)
    servers = json.loads(listed.stdout)
    if not isinstance(servers, list) or any(not isinstance(server, dict) or not isinstance(server.get("name"), str) or not server["name"].replace("-", "").replace("_", "").isalnum() for server in servers):
        raise AdapterUnavailable("cannot safely disable configured Codex connectors")
    disable_mcp = [part for server in servers for part in ("-c", f"mcp_servers.{server['name']}.enabled=false")]
    effective = subprocess.run([codex, *disable_mcp, "mcp", "list", "--json"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, check=True)
    effective_servers = json.loads(effective.stdout)
    if not isinstance(effective_servers, list) or any(server.get("enabled") for server in effective_servers if isinstance(server, dict)):
        raise AdapterUnavailable("configured Codex connectors remained enabled")
    command = [codex, "exec", *disable_mcp, "--sandbox", "read-only", "--ephemeral",
               "--ignore-rules", "--disable", "apps", "--disable", "plugins", "--disable", "hooks",
               "--disable", "browser_use", "--disable", "computer_use", "--disable", "in_app_browser",
               "--skip-git-repo-check"]
    if model:
        command.extend(["--model", model])
    return command


def adapter_command(backend: str, model: str | None) -> list[str]:
    executable = shutil.which(backend)
    if not executable:
        raise AdapterUnavailable(f"{backend} CLI is unavailable")
    if backend == "codex":
        return codex_command(executable, model)
    if backend == "pi":
        command = [executable, "--print", "--no-session", "--no-tools", "--no-extensions", "--no-skills",
                   "--no-prompt-templates", "--no-themes", "--no-context-files", "--no-approve", "--mode", "text"]
        if model:
            command.extend(["--model", model])
        return command
    if backend == "claude":
        command = [executable, "--print", "--no-session-persistence", "--safe-mode", "--restricted",
                   "--strict-mcp-config", "--permission-mode", "plan", "--permission-prompts", "none",
                   "--tools", "", "--no-chrome"]
        if model:
            command.extend(["--model", model])
        return command
    command = [executable, "run", "--standalone", "--format", "json"]
    if model:
        command.extend(["--model", model])
    return command


def adapter_environment(backend: str) -> dict[str, str] | None:
    if backend != "opencode":
        return None
    executable = shutil.which("opencode")
    if not executable:
        raise AdapterUnavailable("opencode CLI is unavailable")
    inventory = subprocess.run([executable, "mcp", "list"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, check=True)
    if inventory.stdout.strip() != "No MCP servers configured":
        raise AdapterUnavailable("OpenCode has configured MCP servers; ScriptDeck cannot verify a per-process disable-all override")
    environment = dict(os.environ)
    environment.update({
        "OPENCODE_PURE": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_CONFIG_CONTENT": json.dumps({"permission": {"*": "deny"}, "plugin": [], "mcp": {},
                                                 "skills": {"paths": [], "urls": []}, "instructions": [],
                                                 "formatter": False, "lsp": False}),
    })
    return environment


def run_qa_harness(command: list[str], prompt: str, timeout: int, environment: dict[str, str] | None) -> int:
    """Run one noninteractive harness and terminate its whole private group on timeout."""
    process = subprocess.Popen([*command, prompt], stdin=subprocess.DEVNULL, cwd=PAYLOADS,
                               text=True, env=environment, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        except ProcessLookupError:
            pass
        raise QueueError(f"Q&A timed out after {timeout} seconds; its private process group was terminated") from exc


def ask(item: dict) -> None:
    payload = verify(item)
    default_backend, model, timeout = qa_settings()
    try:
        selected = input(f"Q&A backend [{default_backend}] (codex, pi, claude, opencode): ").strip().lower()
        question = input(f"Question for {selected or default_backend} (blank cancels): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("Q&A cancelled; queue unchanged.", file=sys.stderr)
        return
    backend = selected or default_backend
    if backend not in BACKENDS:
        print(f"Unsupported Q&A backend: {backend}", file=sys.stderr)
        return
    if not question:
        return
    try:
        command = adapter_command(backend, model)
        environment = adapter_environment(backend)
        returncode = run_qa_harness(command, qa_prompt(payload, question), timeout, environment)
        if returncode:
            print(f"{backend} Q&A exited {returncode}; queue unchanged.", file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError, json.JSONDecodeError, QueueError) as exc:
        print(f"{backend} Q&A unavailable ({exc}); queue unchanged.", file=sys.stderr)


def execution_snapshot(item: dict, run_dir: pathlib.Path) -> pathlib.Path:
    """Copy exactly one verified regular payload into the immutable run record."""
    source = checked_file(payload_path(item["id"]))
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    snapshot = run_dir / "payload.sh"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise QueueError("unsafe queued payload")
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as input_file, snapshot.open("xb") as output_file:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                digest.update(chunk)
                output_file.write(chunk)
        os.chmod(snapshot, 0o500)
        if digest.hexdigest() != item["sha256"]:
            snapshot.unlink(missing_ok=True)
            raise QueueError("payload changed before its execution snapshot was created")
        return snapshot
    finally:
        os.close(fd)


def safe_terminal_write(terminal, chunk: bytes) -> None:
    """Keep raw bytes in logs but prevent output from controlling the review terminal."""
    text = chunk.decode("utf-8", errors="backslashreplace")
    visible = "".join(character if character in "\n\r\t" or ord(character) >= 32 and character != "\x7f"
                      else f"\\x{ord(character):02x}" for character in text)
    terminal.write(visible)
    terminal.flush()


def run(item: dict) -> None:
    verify(item)
    print(f"Executing reviewed item: {fields(item)[0]}")
    # The snapshot is created only after approval and is the only file executed.
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    run_dir = RUNS / item["id"] / run_id
    private_dir(run_dir.parent)
    private_dir(run_dir)
    stdout, stderr = run_dir / "stdout.log", run_dir / "stderr.log"
    record = {"id": run_id, "started_at": now(), "status": "running", "exit_code": None,
              "sha256": item["sha256"], "stdout": str(stdout), "stderr": str(stderr)}
    atomic_json(run_dir / "result.json", record)
    try:
        snapshot = execution_snapshot(item, run_dir)
    except QueueError as exc:
        record.update({"ended_at": now(), "status": "rejected", "reason": str(exc)})
        atomic_json(run_dir / "result.json", record)
        item["runs"].append({"run_id": run_id, "started_at": record["started_at"], "status": record["status"], "exit_code": None})
        atomic_json(metadata_path(item["id"]), item)
        print(f"Not run: {exc}. A rejected-run record was saved in {run_dir}", file=sys.stderr)
        return
    print(f"Running; logs: {run_dir}")
    with stdout.open("wb") as out, stderr.open("wb") as err:
        process = subprocess.Popen(["/bin/sh", str(snapshot), *item.get("arguments", [])], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def copy_stream(source, destination, terminal):
            while chunk := source.read(4096):
                destination.write(chunk); destination.flush()
                safe_terminal_write(terminal, chunk)
        threads = [threading.Thread(target=copy_stream, args=(process.stdout, out, sys.stdout)),
                   threading.Thread(target=copy_stream, args=(process.stderr, err, sys.stderr))]
        for thread in threads: thread.start()
        try:
            code = process.wait()
            record.update({"ended_at": now(), "status": "succeeded" if code == 0 else "failed", "exit_code": code})
        except KeyboardInterrupt:
            process.terminate()
            code = process.wait()
            record.update({"ended_at": now(), "status": "interrupted", "exit_code": code})
            print("Interrupted; result and logs were retained.", file=sys.stderr)
        for thread in threads: thread.join()
        atomic_json(run_dir / "result.json", record)
    item["runs"].append({"run_id": run_id, "started_at": record["started_at"], "status": record["status"], "exit_code": record["exit_code"]})
    atomic_json(metadata_path(item["id"]), item)
    print(f"{record['status']} (exit {record['exit_code']}); stdout/stderr saved in {run_dir}")


def deny(item: dict) -> None:
    """Persist a user's decision not to execute the current queued revision."""
    verify(item)
    try:
        reason = input("Reason for denial (optional; blank allowed): ")
    except (EOFError, KeyboardInterrupt):
        print("Denial cancelled; queue unchanged.", file=sys.stderr)
        return
    if len(reason) > 4000:
        print("Denial cancelled; reason must be at most 4000 characters.", file=sys.stderr)
        return
    current = load(item["id"])
    if current.get("sha256") != item.get("sha256") or sha256(verify(current)) != item.get("sha256"):
        print("Denial cancelled; queued bytes changed concurrently.", file=sys.stderr)
        return
    denials = current.setdefault("denials", [])
    if not isinstance(denials, list):
        raise QueueError("invalid denials metadata")
    if current_denial(current):
        print("This revision is already denied; queue unchanged.")
        return
    record = {"denial_id": dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3),
              "denied_at": now(), "sha256": current["sha256"], "revision": revision_number(current), "reason": reason}
    denials.append(record)
    atomic_json(metadata_path(current["id"]), current)
    item.clear(); item.update(current)
    print("Denied; the queued revision was not executed and is hidden from normal review.")


def reconsider(item: dict) -> None:
    denial = current_denial(item)
    if not denial:
        print("This revision is not currently denied.")
        return
    try:
        confirmation = input("Type RECONSIDER to return this denied revision to pending review: ")
    except (EOFError, KeyboardInterrupt):
        print("Reconsideration cancelled; queue unchanged.", file=sys.stderr)
        return
    if confirmation != "RECONSIDER":
        print("Reconsideration cancelled; queue unchanged.")
        return
    current = load(item["id"])
    active = current_denial(current)
    if not active or active.get("denial_id") != denial.get("denial_id"):
        print("Reconsideration cancelled; denial changed concurrently.", file=sys.stderr)
        return
    active["reconsidered_at"] = now()
    atomic_json(metadata_path(current["id"]), current)
    item.clear(); item.update(current)
    print("Denial retained in history; this revision is pending review again and was not executed.")


def visible_items(include_ran: bool, include_denied: bool) -> list[dict]:
    return [item for item in items()
            if (include_denied or not is_denied(item)) and (include_ran or not is_completed(item))]


def review_prompt(item: dict, configured_action: str) -> tuple[str, str | None]:
    if is_denied(item):
        choices = "[e]dit/review [a]sk Q&A [c]onsider again [q]uit"
        supported = {"edit": "e", "ask": "a", "quit": "q"}
    elif is_completed(item):
        choices = "[e]dit/review [a]sk Q&A [r]erun [d]eny [q]uit"
        supported = {"run": "r", "edit": "e", "ask": "a", "deny": "d", "quit": "q"}
    else:
        choices = "[e]dit/review [a]sk Q&A [r]un [d]eny [q]uit"
        supported = {"run": "r", "edit": "e", "ask": "a", "deny": "d", "quit": "q"}
    shortcut = supported.get(configured_action)
    label = configured_action.title() if shortcut else "No action"
    return f"{choices} [Enter: {label}]: ", shortcut


def review(show_hash: bool = False, include_ran: bool = False, include_denied: bool = False) -> int:
    with lock():
        queued = visible_items(include_ran, include_denied)
        if not queued:
            print("No pending scripts. Use --include-ran for completed history or --include-denied to reconsider denied revisions.")
            return 0
        for item in queued:
            print("\n" + "=" * 72)
            print(describe(item, show_hash) + f"\nState: {execution_label(item)}")
            while True:
                prompt, enter_action = review_prompt(item, default_action())
                try:
                    raw_action = input(prompt)
                except EOFError:
                    print("Review input closed; no action taken.", file=sys.stderr)
                    return 0
                except KeyboardInterrupt:
                    print("Review interrupted; no action taken.", file=sys.stderr)
                    return 0
                action = raw_action.strip().lower() or enter_action
                if action is None:
                    print("No action selected.")
                    continue
                if action == "e": edit_review(item)
                elif action == "a": ask(item)
                elif action == "c" and is_denied(item): reconsider(item)
                elif action == "r":
                    if latest_execution(item) and latest_execution(item).get("status") == "running":
                        print("This item has a run in progress; inspect results before attempting another run.")
                    elif is_completed(item) and not include_ran:
                        print("Completed items require --include-ran for a deliberate rerun.")
                    else:
                        run(item); break
                elif action == "d" and not is_denied(item):
                    deny(item)
                    if is_denied(item): break
                elif action == "q": return 0
                else: print("Choose e, a, r, d, c, or q as shown.")
    return 0


def list_items(args: argparse.Namespace) -> int:
    listed = visible_items(args.include_ran, args.include_denied)
    if args.json:
        print(json.dumps({"items": listed}, indent=2, sort_keys=True))
        return 0
    if not listed:
        print("No pending scripts. Use --include-ran for execution history or --include-denied for denied revisions.")
        return 0
    for item in listed:
        title = fields(item)[0]
        hash_part = f"  {item['sha256']}" if args.show_sha256 else ""
        print(f"{item['id']}{hash_part}  [{execution_label(item)}]  {title}")
    return 0


def results(args: argparse.Namespace) -> int:
    base = RUNS / args.id if args.id else RUNS
    if args.id:
        item = load(args.id)
        records = []
        if base.exists():
            for path in sorted(base.glob("**/result.json")):
                try:
                    checked_file(path)
                    records.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError, QueueError):
                    continue
        print(json.dumps({"id": args.id, "runs": records, "denials": item.get("denials", [])}, indent=2, sort_keys=True))
        return 0
    if not base.exists():
        print("No results yet."); return 0
    records = sorted(base.glob("**/result.json"))
    if not records:
        print("No results yet.")
        return 0
    for path in records:
        print(path.read_text(encoding="utf-8"), end="")
    return 0


def history(args: argparse.Namespace) -> int:
    selected = [load(args.id)] if args.id else items()
    # json.dumps escapes untrusted denial reasons rather than rendering controls.
    print(json.dumps({"items": selected}, indent=2, sort_keys=True))
    return 0


def config_show(_: argparse.Namespace) -> int:
    backend, model, timeout = qa_settings()
    print(json.dumps({"qa": {"backend": backend, "model": model, "timeout_seconds": timeout},
                      "review": {"default_action": default_action()},
                      "config_path": str(CONFIG),
                      "precedence": "SUDODECK_QA_* and SUDODECK_DEFAULT_ACTION, then deprecated SCRIPTDECK_* environment variables, override config values"}, indent=2))
    return 0


def config_set_backend(args: argparse.Namespace) -> int:
    save_qa_setting("backend", args.backend)
    print(f"Saved Q&A backend: {args.backend}")
    return 0


def config_set_model(args: argparse.Namespace) -> int:
    save_qa_setting("model", args.model)
    print("Saved Q&A model.")
    return 0


def config_set_timeout(args: argparse.Namespace) -> int:
    save_qa_setting("timeout_seconds", args.seconds)
    print(f"Saved Q&A timeout: {args.seconds} seconds")
    return 0


def config_set_default_action(args: argparse.Namespace) -> int:
    save_default_action(args.action)
    print(f"Saved review default action: {args.action}")
    return 0


def migrate(_: argparse.Namespace) -> int:
    """Move only the previous public ScriptDeck paths, without merging state."""
    if any(name in os.environ for name in ("SUDODECK_HOME", "SCRIPTDECK_HOME", "SUDODECK_CONFIG", "SCRIPTDECK_CONFIG")):
        raise QueueError("migrate requires default paths; unset queue/config override environment variables first")
    state_exists = LEGACY_ROOT.exists()
    config_exists = LEGACY_CONFIG.exists()
    if not state_exists and not config_exists:
        print("No legacy ScriptDeck state or config found; nothing migrated.")
        return 0
    if DEFAULT_ROOT.exists() or DEFAULT_CONFIG.exists():
        raise QueueError("refusing migration because a SudoDeck state directory or config already exists; nothing was changed")
    # Lock the old queue before checking and moving it, so an old alias cannot be
    # in the middle of an edit/run while its private tree is renamed.
    handle = None
    try:
        if state_exists:
            import fcntl
            legacy_lock = LEGACY_ROOT / ".lock"
            if legacy_lock.is_symlink():
                raise QueueError("unsafe legacy queue lock")
            handle = legacy_lock.open("a+")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if LEGACY_ROOT.is_symlink() or not LEGACY_ROOT.is_dir():
                raise QueueError("unsafe legacy queue directory")
        if config_exists:
            checked_file(LEGACY_CONFIG)
        if config_exists:
            private_dir(DEFAULT_CONFIG.parent)
            os.replace(LEGACY_CONFIG, DEFAULT_CONFIG)
            try:
                LEGACY_CONFIG.parent.rmdir()
            except OSError:
                pass
        if state_exists:
            DEFAULT_ROOT.parent.mkdir(parents=True, exist_ok=True)
            os.replace(LEGACY_ROOT, DEFAULT_ROOT)
        print(f"Migrated legacy ScriptDeck data to {DEFAULT_ROOT} and config to {DEFAULT_CONFIG}. Existing records were moved, not merged.")
        return 0
    except BlockingIOError as exc:
        raise QueueError("legacy ScriptDeck queue is busy; close other review/run sessions and retry") from exc
    finally:
        if handle is not None:
            handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog=os.environ.get("SUDODECK_PROG", os.environ.get("SCRIPTDECK_PROG")), description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="copy a script into the queue and record its review notes")
    add.add_argument("path"); add.add_argument("--sha256", help="optional expected source digest"); add.add_argument("--title", required=True); add.add_argument("--summary", required=True); add.add_argument("--affects", required=True); add.add_argument("--risks", required=True); add.add_argument("--arg", action="append", default=[], help="literal argument passed when explicitly run"); add.set_defaults(func=enqueue)
    review_parser = commands.add_parser("review", help="review queued scripts and explicitly run or deny them")
    review_parser.add_argument("--show-sha256", action="store_true", help="show internal payload digests")
    review_parser.add_argument("--include-ran", action="store_true", help="include completed items for a deliberate rerun")
    review_parser.add_argument("--include-denied", action="store_true", help="include denied revisions so the user can reconsider them")
    review_parser.set_defaults(func=lambda args: review(args.show_sha256, args.include_ran, args.include_denied))
    list_parser = commands.add_parser("list", help="list queued scripts")
    list_parser.add_argument("--show-sha256", action="store_true", help="show internal payload digests")
    list_parser.add_argument("--include-ran", action="store_true", help="include completed items and their final statuses")
    list_parser.add_argument("--include-denied", action="store_true", help="include denied revisions")
    list_parser.add_argument("--json", action="store_true", help="emit listed metadata as JSON (denial reasons are untrusted data)")
    list_parser.set_defaults(func=list_items)
    result = commands.add_parser("results", help="show saved run records"); result.add_argument("id", nargs="?"); result.set_defaults(func=results)
    history_parser = commands.add_parser("history", help="emit queue metadata, including denial records, as JSON")
    history_parser.add_argument("id", nargs="?")
    history_parser.set_defaults(func=history)
    config_parser = commands.add_parser("config", help="show or save non-secret Q&A and review settings")
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show", help="show effective non-secret Q&A settings").set_defaults(func=config_show)
    backend_parser = config_commands.add_parser("set-qa-backend", help="save the default Q&A backend")
    backend_parser.add_argument("backend", choices=sorted(BACKENDS)); backend_parser.set_defaults(func=config_set_backend)
    model_parser = config_commands.add_parser("set-qa-model", help="save an optional Q&A model name")
    model_parser.add_argument("model"); model_parser.set_defaults(func=config_set_model)
    timeout_parser = config_commands.add_parser("set-qa-timeout", help="save a Q&A timeout in seconds")
    timeout_parser.add_argument("seconds", type=int); timeout_parser.set_defaults(func=config_set_timeout)
    action_parser = config_commands.add_parser("set-default-action", help="save the action selected by Enter at the script prompt")
    action_parser.add_argument("action", choices=sorted(DEFAULT_ACTIONS)); action_parser.set_defaults(func=config_set_default_action)
    commands.add_parser("migrate", help="explicitly move legacy ScriptDeck default-path state into SudoDeck; never merges or overwrites").set_defaults(func=migrate)
    args = parser.parse_args()
    try: return args.func(args)
    except QueueError as exc: print(f"error: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
