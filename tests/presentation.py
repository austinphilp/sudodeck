#!/usr/bin/env python3
"""Isolated display checks. Never select run or execute a queued payload."""
import argparse
import contextlib
import copy
import datetime as dt
import io
import json
import os
import pathlib
import runpy
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


class Terminal(io.StringIO):
    def isatty(self):
        return True


class PresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="sudodeck-presentation-")
        cls.root = pathlib.Path(cls.work.name)
        os.environ["SUDODECK_HOME"] = str(cls.root / "queue")
        os.environ["SUDODECK_CONFIG"] = str(cls.root / "config.json")
        os.environ["SUDODECK_DEFAULT_ACTION"] = "none"
        cls.module = runpy.run_path("sudodeck.py", run_name="sudodeck_presentation_test")
        cls.module["setup"]()
        cls.payload = cls.root / "harmless.sh"
        cls.payload.write_text("#!/bin/sh\necho MUST-NOT-EXECUTE\n")
        cls.payload.chmod(0o700)
        with contextlib.redirect_stdout(io.StringIO()):
            cls.module["enqueue"](argparse.Namespace(path=str(cls.payload), sha256=None,
                title="Rotate one service certificate", summary="Validate then rotate the named certificate.",
                affects="Only the named service and its private logs", risks="Requires scoped sudo", arg=[]))
        cls.item = cls.module["items"]()[0]

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def test_local_dates_and_invalid_legacy_values(self):
        with patch.dict(os.environ, {"TZ": "America/Chicago"}):
            time.tzset()
            try:
                self.assertEqual(self.module["display_date"]("2026-07-12T23:29:28+00:00"),
                    "Jul 12, 2026 at 6:29:28 PM CDT (UTC-05:00)")
                self.assertEqual(self.module["display_date"]("2026-01-12T23:29:28Z"),
                    "Jan 12, 2026 at 5:29:28 PM CST (UTC-06:00)")
                self.assertEqual(self.module["display_date"]("2026-01-12T23:29:28"),
                    "2026-01-12T23:29:28 (timezone unknown)")
                self.assertEqual(self.module["display_date"]("legacy"), "legacy (unrecognized date)")
                self.assertNotIn("\x1b", self.module["display_date"]("\x1b[2J"))
            finally:
                # patch.dict restores TZ after exiting; restore libc timezone there.
                pass
        time.tzset()

    def test_color_capabilities_and_status_words(self):
        with contextlib.redirect_stdout(Terminal()), patch.dict(os.environ, {"TERM": "xterm"}):
            with patch.dict(os.environ):
                os.environ.pop("NO_COLOR", None)
                self.assertIn("\x1b[33m", self.module["status_text"](self.item))
            with patch.dict(os.environ, {"NO_COLOR": ""}):
                self.assertEqual(self.module["status_text"](self.item), "[PENDING]")
            with patch.dict(os.environ, {"TERM": "dumb"}):
                self.assertEqual(self.module["status_text"](self.item), "[PENDING]")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.module["status_text"](self.item), "[PENDING]")

    def test_width_long_unicode_and_terminal_controls(self):
        item = copy.deepcopy(self.item)
        item["title"] = "Long title " * 15 + "\x1b[2J\x07\u202e"
        item["summary"] = "Multiline\ntext\t" + "漢字 e\u0301 " * 30
        for width in (80, 40, 24, 12):
            with contextlib.redirect_stdout(io.StringIO()), patch("shutil.get_terminal_size", return_value=os.terminal_size((width, 24))):
                output = self.module["describe"](item, True)
                self.assertNotIn("\x1b", output)
                self.assertNotIn("\x07", output)
                self.assertNotIn("\u202e", output)
                for line in output.splitlines():
                    self.assertLessEqual(self.module["cell_width"](line), width, line)
                self.assertIn(item["id"], output.replace("\n", ""))
                self.assertIn(item["sha256"], output.replace("\n", ""))

    def test_all_prompts_preserve_default_shortcuts(self):
        completed = copy.deepcopy(self.item)
        completed["runs"] = [{"status": "succeeded", "sha256": completed["sha256"], "exit_code": 0}]
        denied = copy.deepcopy(self.item)
        denied["denials"] = [{"sha256": denied["sha256"], "denied_at": "2026-10-12T23:29:28Z"}]
        for item in (self.item, completed, denied):
            for action, shortcut in (("none", None), ("run", "r"), ("edit", "e"), ("ask", "a"), ("deny", "d"), ("quit", "q")):
                prompt, actual = self.module["review_prompt"](item, action)
                expected = None if item is denied and action in {"run", "deny"} else shortcut
                self.assertEqual(actual, expected)
                self.assertIn("[Enter: " + (action.title() if expected else "No action") + "]", prompt)
                if item is denied:
                    self.assertIn("[c]onsider again", prompt)
                    self.assertNotIn("[r]", prompt)
                elif item is completed:
                    self.assertIn("[r]erun", prompt)

    def test_json_and_safe_review_leave_queue_unchanged(self):
        source = pathlib.Path("sudodeck.py").resolve()
        before = {str(path): path.read_bytes() for path in (self.root / "queue").rglob("*") if path.is_file()}
        for action in ("none", "run", "edit", "ask", "deny", "quit"):
            env = dict(os.environ, SUDODECK_DEFAULT_ACTION=action)
            for response in ("q\n", ""):
                result = subprocess.run(["python3", str(source), "review"], input=response,
                    text=True, capture_output=True, env=env, check=True)
                self.assertNotIn("\x1b", result.stdout)
                self.assertIn("Added:", result.stdout)
                self.assertIn(self.item["id"], result.stdout)
                self.assertNotIn("MUST-NOT-EXECUTE", result.stdout)
        result = subprocess.run(["python3", str(source), "review"], input="\nq\n",
            text=True, capture_output=True, check=True)
        self.assertIn("No action selected.", result.stdout)
        result = subprocess.run(["python3", str(source), "list", "--json"], text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"items": [self.item]})
        after = {str(path): path.read_bytes() for path in (self.root / "queue").rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(list((self.root / "queue" / "runs").rglob("result.json")))


if __name__ == "__main__":
    unittest.main()
