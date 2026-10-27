#!/usr/bin/env python3
"""A failed run that needs gcloud re-auth stays pending for a manual retry."""
import argparse
import contextlib
import io
import json
import os
import pathlib
import runpy
import tempfile
import unittest
from unittest.mock import patch

BQ_STDERR = """ERROR: (bq) There was a problem refreshing your current auth tokens: Reauthentication failed. cannot prompt during non-interactive execution.
Please run:

  $ gcloud auth login

to obtain new credentials.

If you have already logged in with a different account, run:

  $ gcloud config set account ACCOUNT

to select an already authenticated account to use.
"""


class AuthRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="sudodeck-auth-retry-")
        cls.root = pathlib.Path(cls.work.name)
        os.environ["SUDODECK_HOME"] = str(cls.root / "queue")
        os.environ["SUDODECK_CONFIG"] = str(cls.root / "config.json")
        cls.module = runpy.run_path("sudodeck.py", run_name="sudodeck_auth_retry_test")
        cls.module["setup"]()

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def enqueue(self, name, body, args=()):
        script = self.root / f"{name}.sh"
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o700)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.module["enqueue"](argparse.Namespace(path=str(script), sha256=None, title=name, summary="x",
                                                      affects="x", risks="x", arg=list(args)))
        return self.module["load"](output.getvalue().strip())

    def run_item(self, item_id):
        item = self.module["load"](item_id)
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            self.module["run"](item)
        return self.module["load"](item_id), output.getvalue()

    def detect(self, text):
        log = self.root / "detect.log"
        log.write_text(text)
        return self.module["detect_gcloud_reauth"]((log,))

    def test_bq_reauth_failure_stays_pending_until_a_manual_retry_succeeds(self):
        marker = self.root / "logged-in"
        item = self.enqueue("bq refresh", f'[ -f "$1" ] && exit 0\ncat >&2 <<\'EOF\'\n{BQ_STDERR}EOF\nexit 1\n',
                            args=[str(marker)])

        failed, output = self.run_item(item["id"])
        self.assertIn("Kept pending. Google Cloud credentials need re-authentication. Run `gcloud auth login`", output)
        self.assertEqual(failed["runs"][-1]["status"], "failed")
        self.assertEqual(failed["runs"][-1]["gcloud_reauth_required"]["login_command"], "gcloud auth login")
        self.assertIn(failed["id"], [listed["id"] for listed in self.module["visible_items"](False, False)])
        self.assertEqual(self.module["execution_label"](failed), "PENDING RETRY (last FAILED, exit 1, gcloud re-auth needed)")
        self.assertIn("Last attempt: Google Cloud credentials need re-authentication. Run `gcloud auth login`, then run "
                      "this item again from sudodeck review.", " ".join(self.module["describe"](failed).split()))
        with contextlib.redirect_stdout(io.StringIO()) as results:
            self.module["results"](argparse.Namespace(id=failed["id"]))
        record = json.loads(results.getvalue())["runs"][0]
        self.assertEqual((record["status"], record["exit_code"]), ("failed", 1))
        self.assertEqual(record["gcloud_reauth_required"]["login_command"], "gcloud auth login")

        marker.touch()
        # Run records order by second-resolution start time; keep the retry strictly later.
        with patch.dict(self.module["run"].__globals__, now=lambda: "2999-01-01T00:00:00+00:00"):
            retried, _ = self.run_item(item["id"])
        self.assertEqual([run["status"] for run in retried["runs"]], ["failed", "succeeded"])
        self.assertTrue(self.module["is_completed"](retried))

    def test_successful_run_that_prints_a_reauth_message_is_completed(self):
        item = self.enqueue("noisy success", f"cat >&2 <<'EOF'\n{BQ_STDERR}EOF\n")
        ran, _ = self.run_item(item["id"])
        self.assertNotIn("gcloud_reauth_required", ran["runs"][-1])
        self.assertTrue(self.module["is_completed"](ran))

    def test_named_login_commands(self):
        adc_reauth = ("There was a problem reauthenticating while refreshing your current auth tokens: reauth is required.\n"
                      "Please retry your command or run:\n\n  $ gcloud auth application-default login\n\n"
                      "to obtain new credentials.")
        self.assertEqual(self.detect(adc_reauth)["login_command"], "gcloud auth application-default login")
        google_auth = ("google.auth.exceptions.RefreshError: Reauthentication is needed. Please run "
                       "`gcloud auth application-default login` to reauthenticate.")
        self.assertEqual(self.detect(google_auth)["login_command"], "gcloud auth application-default login")
        saml = "Please run `gcloud auth login` to complete reauthentication with SAML."
        self.assertEqual(self.detect(saml)["login_command"], "gcloud auth login")
        web_login = "Please run:\n\n  $ gcloud auth login\n\nto complete reauthentication."
        self.assertEqual(self.detect(web_login)["login_command"], "gcloud auth login")

    def test_refresh_errors_without_a_named_command_suggest_both_logins(self):
        for text in ("google.auth.exceptions.RefreshError: ('invalid_grant: Token has been expired or revoked.', {})",
                     'oauth2: "invalid_grant" "Token has been expired or revoked."',
                     "RefreshError: ('invalid_grant: reauth related error (invalid_rapt)', {})",
                     "Reauthentication failed. cannot prompt during non-interactive execution."):
            with self.subTest(text=text):
                detected = self.detect(text)
                self.assertIsNone(detected["login_command"])
                self.assertIn("`gcloud auth login` (or `gcloud auth application-default login`", detected["hint"])

    def test_other_gcloud_auth_failures_are_not_reauth_failures(self):
        # gcloud appends the same login advice to every token refresh failure.
        advice = "\nPlease run:\n\n  $ gcloud auth login\n\nto obtain new credentials."
        for text in ("There was a problem refreshing your current auth tokens: temporarily_unavailable: try later" + advice,
                     "There was a problem refreshing your current auth tokens: invalid_grant: Bad Request" + advice,
                     "There was a problem refreshing your current auth tokens: invalid_grant: Invalid JWT: Token must be "
                     "a short-lived token (60 minutes) and in a reasonable timeframe." + advice,
                     "You do not currently have an active account selected." + advice,
                     "There was a problem refreshing your current auth tokens: denied by context aware access policy",
                     "Reauthentication required.",
                     "ERROR: permission denied; try gcloud auth login"):
            with self.subTest(text=text):
                self.assertIsNone(self.detect(text))

    def test_retry_note_applies_only_to_the_current_pending_revision(self):
        item = self.enqueue("bq stale", f"cat >&2 <<'EOF'\n{BQ_STDERR}EOF\nexit 1\n")
        failed, _ = self.run_item(item["id"])
        self.assertEqual(failed["runs"][-1]["gcloud_reauth_required"]["sha256"], failed["sha256"])
        with contextlib.redirect_stdout(io.StringIO()) as listed:
            self.module["list_items"](argparse.Namespace(include_ran=False, include_denied=False, json=False,
                                                         show_sha256=False))
        self.assertIn("Run `gcloud auth login`", " ".join(listed.getvalue().split()))

        revised = dict(failed, sha256="0" * 64)
        self.assertTrue(self.module["execution_label"](revised).startswith("PENDING REVISION (last FAILED"))
        self.assertNotIn("Last attempt", self.module["describe"](revised))
        denied = dict(failed, denials=[{"denial_id": "d", "denied_at": "x", "sha256": failed["sha256"], "reason": ""}])
        self.assertNotIn("Last attempt", self.module["describe"](denied))

if __name__ == "__main__":
    unittest.main()
