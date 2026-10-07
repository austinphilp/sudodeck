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
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time

APP = "scriptdeck"
ROOT = pathlib.Path(os.environ.get("SCRIPTDECK_HOME", pathlib.Path.home() / ".local/share" / APP))
PAYLOADS = ROOT / "payloads"
META = ROOT / "metadata"
RUNS = ROOT / "runs"
LOCK = ROOT / ".lock"


class QueueError(RuntimeError):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


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
    return details


def show(item: dict, show_hash: bool = False) -> None:
    payload = verify(item)
    print(describe(item, show_hash) + "\n")
    print(payload.read_text(encoding="utf-8", errors="replace"), end="")


def ask(item: dict) -> None:
    payload = verify(item)
    codex = shutil.which("codex")
    if not codex:
        print("Codex CLI is unavailable; use view, run, skip, or quit.", file=sys.stderr)
        return
    question = input("Question for Codex (blank cancels): ").strip()
    if not question:
        return
    # The only context is the immutable queued payload and the user's question.
    prompt = textwrap.dedent(f"""\
        You are reviewing one queued shell script. Answer the question using only
        the script below. Do not run commands, edit files, use tools, recommend
        bypassing review, or provide instructions that execute this script.
        State uncertainty plainly.\n\nQuestion: {question}\n\nScript:\n{payload.read_text(encoding='utf-8', errors='replace')}""")
    try:
        result = subprocess.run([codex, "exec", "--help"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if result.returncode != 0:
            raise QueueError("Codex CLI cannot provide non-interactive help")
        listed = subprocess.run([codex, "mcp", "list", "--json"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, check=True)
        servers = json.loads(listed.stdout)
        if not isinstance(servers, list) or any(not isinstance(server, dict) or not isinstance(server.get("name"), str) or not server["name"].replace("-", "").replace("_", "").isalnum() for server in servers):
            raise QueueError("cannot safely disable configured Codex connectors")
        disable_mcp = [part for server in servers for part in ("-c", f"mcp_servers.{server['name']}.enabled=false")]
        effective = subprocess.run([codex, *disable_mcp, "mcp", "list", "--json"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, check=True)
        effective_servers = json.loads(effective.stdout)
        if not isinstance(effective_servers, list) or any(server.get("enabled") for server in effective_servers if isinstance(server, dict)):
            raise QueueError("configured Codex connectors remained enabled")
        # `exec` accepts its prompt as an argument on supported CLI releases.
        completed = subprocess.run([codex, "exec", *disable_mcp, "--sandbox", "read-only", "--ephemeral",
                                    "--ignore-rules", "--disable", "apps", "--disable", "plugins",
                                    "--disable", "hooks", "--disable", "browser_use", "--disable", "computer_use",
                                    "--disable", "in_app_browser", "--skip-git-repo-check",
                                    "-C", str(PAYLOADS), prompt], stdin=subprocess.DEVNULL,
                                   text=True, timeout=120)
        if completed.returncode:
            print(f"Codex Q&A exited {completed.returncode}; queue unchanged.", file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError, json.JSONDecodeError, QueueError) as exc:
        print(f"Codex Q&A unavailable ({exc}); queue unchanged.", file=sys.stderr)


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


def review(show_hash: bool = False) -> int:
    with lock():
        queued = items()
        if not queued:
            print("Queue is empty.")
            return 0
        for item in queued:
            print("\n" + "=" * 72)
            print(describe(item, show_hash))
            while True:
                action = input("[v]iew [a]sk Codex [r]un [s]kip [q]uit: ").strip().lower()
                if action == "v": show(item, show_hash)
                elif action == "a": ask(item)
                elif action == "r": run(item); break
                elif action == "s": break
                elif action == "q": return 0
                else: print("Choose v, a, r, s, or q.")
    return 0


def list_items(args: argparse.Namespace) -> int:
    for item in items():
        title = fields(item)[0]
        hash_part = f"  {item['sha256']}" if args.show_sha256 else ""
        print(f"{item['id']}{hash_part}  {title}")
    return 0


def results(args: argparse.Namespace) -> int:
    base = RUNS / args.id if args.id else RUNS
    if not base.exists():
        print("No results yet."); return 0
    records = sorted(base.glob("**/result.json"))
    if not records:
        print("No results yet.")
        return 0
    for path in records:
        print(path.read_text(encoding="utf-8"), end="")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog=os.environ.get("SCRIPTDECK_PROG"), description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="copy a script into the queue and record its review notes")
    add.add_argument("path"); add.add_argument("--sha256", help="optional expected source digest"); add.add_argument("--title", required=True); add.add_argument("--summary", required=True); add.add_argument("--affects", required=True); add.add_argument("--risks", required=True); add.add_argument("--arg", action="append", default=[], help="literal argument passed when explicitly run"); add.set_defaults(func=enqueue)
    review_parser = commands.add_parser("review", help="review queued scripts and explicitly run/skip them")
    review_parser.add_argument("--show-sha256", action="store_true", help="show internal payload digests")
    review_parser.set_defaults(func=lambda args: review(args.show_sha256))
    list_parser = commands.add_parser("list", help="list queued scripts")
    list_parser.add_argument("--show-sha256", action="store_true", help="show internal payload digests")
    list_parser.set_defaults(func=list_items)
    result = commands.add_parser("results", help="show saved run records"); result.add_argument("id", nargs="?"); result.set_defaults(func=results)
    args = parser.parse_args()
    try: return args.func(args)
    except QueueError as exc: print(f"error: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
