#!/usr/bin/env python3
"""Real PTY tests using simulated owner choices on disposable, harmless fixtures.

These tests never inspect or execute the user's queue or privileged wrappers.
"""
import errno
import hashlib
import json
import os
import pathlib
import pty
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest


APP = pathlib.Path(__file__).resolve().parents[1] / "sudodeck.py"


class Terminal:
    def __init__(self, environment, command=None):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execve(sys.executable, command or [sys.executable, str(APP), "review"], environment)
        self.output = b""
        self.status = None
        self.initial_attributes = termios.tcgetattr(self.fd)

    def send(self, value):
        os.write(self.fd, value)

    def read(self, timeout):
        if self.fd is None:
            time.sleep(timeout)
            return
        if select.select([self.fd], [], [], timeout)[0]:
            try:
                self.output += os.read(self.fd, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise

    def expect(self, value, timeout=8):
        deadline = time.monotonic() + timeout
        while value not in self.output:
            if time.monotonic() >= deadline:
                raise AssertionError(f"PTY did not emit {value!r}: {self.output!r}")
            self.read(0.05)

    def finish(self, timeout=8):
        deadline = time.monotonic() + timeout
        while self.status is None:
            self.read(0.05)
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)
            elif time.monotonic() >= deadline:
                raise AssertionError(f"PTY reviewer did not exit: {self.output!r}")
        return self.status

    def close(self):
        # A failing assertion must not leave a fixture waiting for terminal input.
        if self.status is None:
            foreground = os.tcgetpgrp(self.fd) if self.fd is not None else 0
            for group in {foreground, self.pid} - {0, os.getpgrp()}:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            os.waitpid(self.pid, 0)
        if self.fd is not None:
            os.close(self.fd)

    def hangup(self):
        os.close(self.fd)
        self.fd = None


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sudodeck-pty-test-")
        self.addCleanup(self.temporary.cleanup)
        self.work = pathlib.Path(self.temporary.name)
        self.queue = self.work / "queue"
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith(("SUDODECK_", "SCRIPTDECK_"))}
        self.environment.update(SUDODECK_HOME=str(self.queue),
                                SUDODECK_CONFIG=str(self.work / "config.json"),
                                TERM="dumb", NO_COLOR="1")

    def cli(self, *arguments, input=None):
        return subprocess.run([sys.executable, str(APP), *arguments],
                              env=self.environment, input=input, capture_output=True,
                              text=True, timeout=10)

    def enqueue(self, body, interactive=True, arguments=()):
        source = self.work / "fixture.sh"
        source.write_text("#!/bin/sh\n" + body + "\n")
        source.chmod(0o700)
        options = ["add", str(source), "--title", "Harmless PTY fixture",
                   "--summary", "Exercise disposable test input and output",
                   "--affects", "Only this temporary test directory", "--risks", "None"]
        if interactive:
            options.append("--interactive")
        options.extend("--arg=" + argument for argument in arguments)
        result = self.cli(*options)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def python_fixture(self, code, **options):
        return self.enqueue("exec " + shlex.quote(sys.executable) + " -c " + shlex.quote(code) + ' "$@"', **options)

    def metadata(self, item_id):
        return json.loads((self.queue / "metadata" / (item_id + ".json")).read_text())

    def receipt(self, item_id):
        records = list((self.queue / "runs" / item_id).glob("*/result.json"))
        self.assertEqual(len(records), 1)
        return records[0].parent, json.loads(records[0].read_text())

    def terminal(self, command=None):
        terminal = Terminal(self.environment, command)
        self.addCleanup(terminal.close)
        terminal.expect(b"> ")
        return terminal

    def assert_not_run(self, item_id):
        self.assertFalse((self.queue / "runs" / item_id).exists())
        self.assertEqual(self.metadata(item_id)["runs"], [])

    def assert_audit(self, item_id, status, mode="terminal"):
        directory, record = self.receipt(item_id)
        self.assertEqual(record["status"], status)
        self.assertEqual(record["stdin_mode"], mode)
        self.assertEqual(record["uid"], os.getuid())
        self.assertEqual(record["gid"], os.getgid())
        self.assertIn("ended_at", record)
        self.assertEqual(record["sha256"], hashlib.sha256((directory / "payload.sh").read_bytes()).hexdigest())
        self.assertEqual(self.metadata(item_id)["runs"][-1]["status"], status)
        return directory, record

    def test_terminal_input_piped_output_live_prompts_and_no_input_log(self):
        item_id = self.python_fixture('''import os, sys, termios
assert os.isatty(0) and not os.isatty(1) and not os.isatty(2)
assert os.tcgetpgrp(0) == os.getpgrp()
settings = termios.tcgetattr(0)
settings[3] &= ~termios.ECHO
termios.tcsetattr(0, termios.TCSANOW, settings)
sys.stdout.write("stdout-prompt: "); sys.stdout.flush()
sys.stderr.write("stderr-prompt: "); sys.stderr.flush()
assert sys.stdin.readline().strip()
print("input-accepted")
''')
        self.assertIs(self.metadata(item_id)["interactive"], True)
        terminal = self.terminal()
        self.assert_not_run(item_id)
        terminal.send(b"r\n")
        # Waiting before typing catches read(4096), which buffers short prompts.
        terminal.expect(b"stdout-prompt: ")
        terminal.expect(b"stderr-prompt: ")
        secret = b"test-input-never-record-this-8bf217"
        terminal.send(secret + b"\n")
        self.assertEqual(terminal.finish(), 0)
        directory, record = self.assert_audit(item_id, "succeeded")
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual((directory / "stdout.log").read_bytes(), b"stdout-prompt: input-accepted\n")
        self.assertEqual((directory / "stderr.log").read_bytes(), b"stderr-prompt: ")
        for path in directory.iterdir():
            if path.is_file():
                self.assertNotIn(secret, path.read_bytes(), path.name)
        self.assertEqual(termios.tcgetattr(terminal.fd), terminal.initial_attributes)

    def test_nontty_interactive_rejected_without_execution(self):
        marker = self.work / "executed"
        item_id = self.enqueue("touch " + shlex.quote(str(marker)))
        result = self.cli("review", input="r\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("terminal", result.stderr.lower())
        self.assertFalse(marker.exists())
        self.assert_not_run(item_id)

    def test_quit_deny_and_closed_review_do_not_run(self):
        item_id = self.enqueue("echo should-not-execute")
        terminal = self.terminal()
        terminal.send(b"q\n")
        self.assertEqual(terminal.finish(), 0)
        self.assert_not_run(item_id)
        self.assertEqual(self.cli("review", input="").returncode, 0)
        self.assert_not_run(item_id)
        result = self.cli("review", input="d\nharmless fixture denied\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_not_run(item_id)
        self.assertEqual(self.metadata(item_id)["denials"][-1]["reason"], "harmless fixture denied")
        self.assertNotIn(item_id, self.cli("list", "--json").stdout)

    def test_changed_payload_cannot_run(self):
        item_id = self.enqueue("echo original")
        payload = self.queue / "payloads" / (item_id + ".sh")
        payload.write_text("#!/bin/sh\necho changed\n")
        terminal = self.terminal()
        terminal.send(b"r\n")
        self.assertNotEqual(terminal.finish(), 0)
        self.assertIn(b"payload changed", terminal.output)
        self.assert_not_run(item_id)

    def test_noninteractive_and_legacy_stdin_remain_closed(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                item_id = self.python_fixture('''import os, sys
assert not os.isatty(0)
assert sys.stdin.read() == ""
print("closed-stdin")
''', interactive=False)
                metadata = self.metadata(item_id)
                self.assertFalse(metadata.get("interactive", False))
                if legacy:
                    metadata.pop("interactive", None)
                    (self.queue / "metadata" / (item_id + ".json")).write_text(json.dumps(metadata))
                result = self.cli("review", input="r\nmust-not-reach-payload\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                directory, _ = self.assert_audit(item_id, "succeeded", "closed")
                self.assertEqual((directory / "stdout.log").read_text(), "closed-stdin\n")

    def test_terminal_eof_is_delivered_and_failure_is_audited(self):
        item_id = self.python_fixture('''import sys
print("waiting-for-eof", flush=True)
assert sys.stdin.readline() == ""
print("eof-received", flush=True)
sys.exit(4)
''')
        terminal = self.terminal()
        terminal.send(b"r\n")
        terminal.expect(b"waiting-for-eof")
        terminal.send(b"\x04")
        self.assertEqual(terminal.finish(), 0)
        _, record = self.assert_audit(item_id, "failed")
        self.assertEqual(record["exit_code"], 4)

    def test_ctrl_c_and_sigterm_cleanup_restore_terminal_and_audit(self):
        for cancellation in ("ctrl-c", "sigterm", "sighup", "hangup"):
            with self.subTest(cancellation=cancellation):
                pid_path = self.work / (cancellation + ".pid")
                item_id = self.python_fixture('''import os, pathlib, subprocess, sys, termios
settings = termios.tcgetattr(0)
settings[3] &= ~termios.ECHO
termios.tcsetattr(0, termios.TCSANOW, settings)
child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.signal(signal.SIGINT, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"], stdout=subprocess.PIPE)
assert child.stdout.readline() == b"ready\\n"
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
print("waiting-for-cancellation", flush=True)
sys.stdin.readline()
''', arguments=(str(pid_path),))
                terminal = self.terminal()
                terminal.send(b"r\n")
                terminal.expect(b"waiting-for-cancellation")
                child_pid = int(pid_path.read_text())
                if cancellation == "ctrl-c":
                    terminal.send(b"\x03")
                elif cancellation == "sigterm":
                    os.kill(terminal.pid, signal.SIGTERM)
                elif cancellation == "sighup":
                    os.kill(terminal.pid, signal.SIGHUP)
                else:
                    terminal.hangup()
                terminal.finish()
                self.assert_audit(item_id, "interrupted")
                if terminal.fd is not None:
                    self.assertEqual(termios.tcgetattr(terminal.fd), terminal.initial_attributes)
                state = pathlib.Path(f"/proc/{child_pid}/stat")
                self.assertTrue(not state.exists() or state.read_text().split()[2] == "Z",
                                "fixture descendant survived cancellation")

    def test_foreground_is_restored_before_next_review(self):
        item_id = self.enqueue("printf fixture-completed")
        pending = self.enqueue("echo must-not-run")
        metadata = self.metadata(pending)
        metadata["created_at"] = "2999-01-01T00:00:00+00:00"
        (self.queue / "metadata" / (pending + ".json")).write_text(json.dumps(metadata))
        terminal = self.terminal()
        terminal.send(b"r\n")
        terminal.expect(b"Review 2 of 2")
        self.assertEqual(os.tcgetpgrp(terminal.fd), terminal.pid)
        self.assertEqual(termios.tcgetattr(terminal.fd), terminal.initial_attributes)
        terminal.send(b"q\n")
        self.assertEqual(terminal.finish(), 0)
        self.assert_audit(item_id, "succeeded")
        self.assert_not_run(pending)

    def test_terminal_tostop_still_streams_prompt(self):
        item_id = self.python_fixture('''import sys
sys.stdout.write("tostop-prompt: "); sys.stdout.flush()
assert sys.stdin.readline().strip() == "fixture-answer"
''')
        terminal = self.terminal()
        attributes = termios.tcgetattr(terminal.fd)
        attributes[3] |= termios.TOSTOP
        termios.tcsetattr(terminal.fd, termios.TCSANOW, attributes)
        terminal.send(b"r\n")
        terminal.expect(b"tostop-prompt: ")
        terminal.send(b"fixture-answer\n")
        self.assertEqual(terminal.finish(), 0)
        self.assertEqual(termios.tcgetattr(terminal.fd), attributes)
        self.assert_audit(item_id, "succeeded")

    def test_spawn_error_restores_terminal_and_saves_failed_receipt(self):
        item_id = self.enqueue("echo must-not-execute")
        launcher = ("import runpy,sys; from unittest.mock import patch; "
                    f"sys.argv=[{str(APP)!r}, 'review']; "
                    "patcher=patch('subprocess.Popen', side_effect=OSError('fixture launch denied')); "
                    f"patcher.start(); runpy.run_path({str(APP)!r}, run_name='__main__')")
        terminal = self.terminal([sys.executable, "-c", launcher])
        terminal.send(b"r\n")
        self.assertEqual(terminal.finish(), 0)
        self.assertEqual(termios.tcgetattr(terminal.fd), terminal.initial_attributes)
        _, record = self.assert_audit(item_id, "failed")
        self.assertIsNone(record["exit_code"])
        self.assertIn("fixture launch denied", record["reason"])

    def test_detached_output_holder_cannot_block_receipt(self):
        pid_path = self.work / "detached.pid"
        item_id = self.python_fixture('''import pathlib, subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
print("detached-holder-started", flush=True)
''', arguments=(str(pid_path),))
        terminal = self.terminal()
        terminal.send(b"r\n")
        terminal.expect(b"detached-holder-started")
        child_pid = int(pid_path.read_text())

        def stop_fixture():
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        self.addCleanup(stop_fixture)
        self.assertEqual(terminal.finish(), 0)
        _, record = self.assert_audit(item_id, "succeeded")
        self.assertIs(record["output_incomplete"], True)


    def test_log_write_failure_restores_terminal_and_saves_failed_receipt(self):
        item_id = self.python_fixture("""import sys, termios, time
settings = termios.tcgetattr(0)
settings[3] &= ~termios.ECHO
termios.tcsetattr(0, termios.TCSANOW, settings)
sys.stdout.write('x' * 20000); sys.stdout.flush()
time.sleep(30)
""")
        launcher = """import errno, pathlib, runpy, sys
original = pathlib.Path.open
class FailingLog:
    def __init__(self, handle): self.handle = handle
    def __enter__(self): return self
    def __exit__(self, *args): return self.handle.__exit__(*args)
    def write(self, chunk): raise OSError(errno.ENOSPC, 'fixture log device full')
    def flush(self): self.handle.flush()
def patched(path, *args, **kwargs):
    handle = original(path, *args, **kwargs)
    return FailingLog(handle) if path.name == 'stdout.log' and args == ('wb',) else handle
pathlib.Path.open = patched
sys.argv = [%r, 'review']
runpy.run_path(%r, run_name='__main__')
""" % (str(APP), str(APP))
        terminal = self.terminal([sys.executable, "-c", launcher])
        terminal.send(b"r\n")
        self.assertEqual(terminal.finish(), 0)
        self.assertEqual(termios.tcgetattr(terminal.fd), terminal.initial_attributes)
        _, record = self.assert_audit(item_id, "failed")
        self.assertIn("fixture log device full", record["reason"])

    def test_final_receipt_failure_is_uncertain_and_cannot_auto_rerun(self):
        marker = self.work / "execution-count"
        item_id = self.python_fixture(r"""import pathlib, sys, termios
settings = termios.tcgetattr(0)
settings[3] &= ~termios.ECHO
termios.tcsetattr(0, termios.TCSANOW, settings)
with pathlib.Path(sys.argv[1]).open('a') as handle:
    handle.write('one execution\n')
print('fixture-finished', flush=True)
""", arguments=(str(marker),))
        launcher = """import errno, json, os, pathlib, runpy, sys
original = os.replace
def patched(source, destination):
    if pathlib.Path(destination).name == 'result.json':
        record = json.loads(pathlib.Path(source).read_text())
        if record.get('status') != 'running':
            raise OSError(errno.ENOSPC, 'fixture receipt device full')
    return original(source, destination)
os.replace = patched
sys.argv = [%r, 'review']
runpy.run_path(%r, run_name='__main__')
""" % (str(APP), str(APP))
        terminal = self.terminal([sys.executable, "-c", launcher])
        terminal.send(b"r\n")
        self.assertNotEqual(terminal.finish(), 0)
        self.assertEqual(termios.tcgetattr(terminal.fd), terminal.initial_attributes)
        self.assertIn(b"fixture-finished", terminal.output)
        self.assertIn(b"fixture receipt device full", terminal.output)
        self.assertNotIn(b"succeeded (exit", terminal.output)
        self.assertNotIn(b"stdout/stderr saved", terminal.output)
        _, record = self.receipt(item_id)
        self.assertEqual(record["status"], "running")
        self.assertNotIn("ended_at", record)
        result = self.cli("review", input="r\nq\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("run in progress", result.stdout)
        self.receipt(item_id)  # Still exactly one uncertain run, not a retry.
        self.assertEqual(marker.read_text(), "one execution\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
