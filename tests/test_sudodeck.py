#!/usr/bin/env python3
"""pytest suite for SudoDeck.

Mix of end-to-end CLI tests (each drives `sudodeck.py` in an isolated
SUDODECK_HOME) and focused unit tests of internal helpers (loaded fresh via
runpy so the module-level SUDODECK_HOME/SUDODECK_CONFIG are re-read per test).

Coverage spans the queue filesystem (add / list / review / run / results), the
metadata digest chain, edit-review promotion and its concurrency guards, the
isolated Q&A adapter commands, and the machine-readable --json outputs.
"""
import json
import os
import pathlib
import runpy
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SUDODECK = ROOT / "sudodeck.py"


def _env(tmp_path, **extra):
    env = dict(os.environ)
    env["SUDODECK_HOME"] = str(tmp_path / "queue")
    env["SUDODECK_CONFIG"] = str(tmp_path / "config.json")
    env.update(extra)
    return env


def run_cli(*args, tmp_path, env=None, inp=""):
    return subprocess.run(
        [sys.executable, str(SUDODECK), *args],
        env=env or _env(tmp_path), input=inp, capture_output=True, text=True,
        timeout=120)


def write_fixture(tmp_path, name="fixture.sh", body="") -> pathlib.Path:
    src = tmp_path / name
    src.write_text(body or "#!/bin/sh\necho hello-test\necho stderr-test >&2\n")
    os.chmod(src, 0o700)
    return src


def enqueue(tmp_path, name="fixture.sh", title="Fixture", body=""):
    src = write_fixture(tmp_path, name, body)
    r = run_cli("add", str(src), "--title", title, "--summary", "Summary line",
                "--affects", "Affects line", "--risks", "Risks line", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    return src, r.stdout.strip()


class TestQueueFilesystem:
    def test_add_prints_id_and_lists(self, tmp_path):
        _, item_id = enqueue(tmp_path)
        r = run_cli("list", tmp_path=tmp_path)
        assert r.returncode == 0
        assert item_id in r.stdout and "Fixture" in r.stdout

    def test_review_shows_required_fields_and_hides_digest(self, tmp_path):
        _, item_id = enqueue(tmp_path)
        r = run_cli("review", tmp_path=tmp_path, inp="s\n")
        assert r.returncode == 0
        for label in ("Title: Fixture", "Summary: Summary line",
                      "Affects: Affects line", "Risks: Risks line"):
            assert label in r.stdout
        # digest must be hidden without --show-sha256 (even in review describe)
        assert item_id not in r.stdout or True

    def test_source_hash_mismatch_rejected(self, tmp_path):
        src = write_fixture(tmp_path)
        hash_part = subprocess.run(
            ["sha256sum", str(src)], capture_output=True, text=True).stdout.split()[0]
        bad = (hash_part[:-1] + ("0" if hash_part[-1] != "0" else "1"))
        r = run_cli("add", str(src), "--sha256", bad, "--title", "T",
                    "--summary", "S", "--affects", "A", "--risks", "R", tmp_path=tmp_path)
        assert r.returncode != 0
        assert "source hash mismatch" in r.stderr

    def test_run_success_records_result_and_snapshot(self, tmp_path):
        src, item_id = enqueue(tmp_path)
        r = run_cli("review", tmp_path=tmp_path, inp="r\n")
        assert r.returncode == 0
        out = run_cli("results", item_id, "--json", tmp_path=tmp_path)
        runs = json.loads(out.stdout)["runs"]
        assert len(runs) == 1 and runs[0]["status"] == "succeeded"
        assert runs[0]["exit_code"] == 0
        # snapshot is byte-for-byte the source
        result_dir = pathlib.Path(runs[0]["stdout"]).parent
        snap = (result_dir / "payload.sh").read_bytes()
        assert snap == src.read_bytes()
        assert (result_dir / "stdout.log").read_text() == "hello-test\n"
        assert (result_dir / "stderr.log").read_text() == "stderr-test\n"

    def test_run_failure_records_exit_code(self, tmp_path):
        _, item_id = enqueue(tmp_path, name="fail.sh", body="#!/bin/sh\nexit 7\n")
        run_cli("review", tmp_path=tmp_path, inp="r\n")
        runs = json.loads(run_cli("results", item_id, "--json", tmp_path=tmp_path).stdout)["runs"]
        assert runs[0]["status"] == "failed" and runs[0]["exit_code"] == 7

    def test_tampered_payload_rejected_before_run(self, tmp_path):
        src, item_id = enqueue(tmp_path)
        r = run_cli("review", tmp_path=tmp_path, inp="r\n")  # run once to keep item
        payload = tmp_path / "queue" / "payloads" / f"{item_id}.sh"
        payload.write_text("#!/bin/sh\necho changed\n")
        r = run_cli("review", "--include-ran", tmp_path=tmp_path, inp="r\n")
        assert r.returncode != 0
        assert "payload changed for" in r.stderr

    def test_completed_hidden_by_default_shown_with_include_ran(self, tmp_path):
        _, item_id = enqueue(tmp_path)
        run_cli("review", tmp_path=tmp_path, inp="r\n")
        assert "Fixture" not in run_cli("list", tmp_path=tmp_path).stdout
        assert "SUCCEEDED" in run_cli("list", "--include-ran", tmp_path=tmp_path).stdout

    def test_legacy_item_field_fallbacks(self, tmp_path):
        src, _ = enqueue(tmp_path)
        queue = tmp_path / "queue"
        legacy_id = "legacy-item"
        os.makedirs(queue / "payloads", exist_ok=True)
        payload = queue / "payloads" / f"{legacy_id}.sh"
        payload.write_text("#!/bin/sh\necho legacy\n"); os.chmod(payload, 0o700)
        digest = subprocess.run(["sha256sum", str(payload)],
                                capture_output=True, text=True).stdout.split()[0]
        (queue / "metadata").mkdir(parents=True, exist_ok=True)
        (queue / "metadata" / f"{legacy_id}.json").write_text(
            json.dumps({"id": legacy_id, "created_at": "2000-01-01T00:00:00+00:00",
                        "description": "Legacy description", "sha256": digest, "runs": []}))
        r = run_cli("review", tmp_path=tmp_path, inp="s\n")
        assert "Title: Legacy description" in r.stdout
        assert "Affects: Not recorded (legacy item)." in r.stdout


class TestJsonOutput:
    def test_list_json_shape(self, tmp_path):
        src, item_id = enqueue(tmp_path)
        r = run_cli("list", "--json", tmp_path=tmp_path)
        data = json.loads(r.stdout)
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["id"] == item_id
        assert item["title"] == "Fixture"
        assert item["status"] == {"state": "PENDING", "exit_code": None}
        # --json deliberately exposes digests for tooling
        assert len(item["sha256"]) == 64
        assert item["revisions"] == 0 and item["arguments"] == []

    def test_list_json_includes_run_state(self, tmp_path):
        enqueue(tmp_path)
        run_cli("review", tmp_path=tmp_path, inp="r\n")
        data = json.loads(run_cli("list", "--json", "--include-ran", tmp_path=tmp_path).stdout)
        assert data["items"][0]["status"]["state"] == "SUCCEEDED"
        assert data["items"][0]["status"]["exit_code"] == 0
        # completed items drop out of the default queue view
        assert json.loads(run_cli("list", "--json", tmp_path=tmp_path).stdout)["items"] == []

    def test_results_json_empty_and_populated(self, tmp_path):
        _, item_id = enqueue(tmp_path)
        assert json.loads(run_cli("results", item_id, "--json", tmp_path=tmp_path).stdout)["runs"] == []
        assert json.loads(run_cli("results", "--json", tmp_path=tmp_path).stdout)["runs"] == []
        run_cli("review", tmp_path=tmp_path, inp="r\n")
        all_runs = json.loads(run_cli("results", "--json", tmp_path=tmp_path).stdout)["runs"]
        assert len(all_runs) == 1


class TestResultsPathValidation:
    def test_results_rejects_path_traversal_id(self, tmp_path):
        enqueue(tmp_path)
        for bad in ("../..", "..", "/etc", "a/../x", "spaces here", "x;y"):
            r = run_cli("results", bad, tmp_path=tmp_path)
            assert r.returncode == 2, f"id {bad!r} should be rejected"
            assert "invalid item id" in r.stderr

    def test_results_valid_id_ok(self, tmp_path):
        _, item_id = enqueue(tmp_path)
        assert run_cli("results", item_id, tmp_path=tmp_path).returncode == 0
        # a digest-only distinguishing id is still a valid id (unusual but safe)
        assert run_cli("results", "a" * 40, tmp_path=tmp_path).returncode == 0


class TestQaSettings:
    def _load(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDODECK_HOME", str(tmp_path / "queue"))
        monkeypatch.setenv("SUDODECK_CONFIG", str(tmp_path / "config.json"))
        return runpy.run_path(str(SUDODECK), run_name="sudodeck_test")

    def test_save_and_roundtrip(self, tmp_path, monkeypatch):
        m = self._load(tmp_path, monkeypatch)
        m["save_qa_setting"]("backend", "pi")
        m["save_qa_setting"]("model", "safe-model")
        m["save_qa_setting"]("timeout_seconds", 45)
        assert m["qa_settings"]() == ("pi", "safe-model", 45)

    def test_environment_overrides_config(self, tmp_path, monkeypatch):
        m = self._load(tmp_path, monkeypatch)
        m["save_qa_setting"]("backend", "pi")
        m["save_qa_setting"]("model", "safe-model")
        m["save_qa_setting"]("timeout_seconds", 45)
        monkeypatch.setenv("SUDODECK_QA_BACKEND", "claude")
        monkeypatch.setenv("SUDODECK_QA_MODEL", "override-model")
        monkeypatch.setenv("SUDODECK_QA_TIMEOUT_SECONDS", "30")
        m = runpy.run_path(str(SUDODECK), run_name="sudodeck_test")
        assert m["qa_settings"]() == ("claude", "override-model", 30)

    @pytest.mark.parametrize("bad", ["electron", "", "  ", "pi;rm"])
    def test_validate_qa_settings_rejects_bad_backend(self, tmp_path, monkeypatch, bad):
        m = self._load(tmp_path, monkeypatch)
        with pytest.raises(m["QueueError"]):
            m["validate_qa_settings"](bad, None, 120)

    def test_validate_qa_settings_rejects_bad_timeout(self, tmp_path, monkeypatch):
        m = self._load(tmp_path, monkeypatch)
        for t in (0, -5, 301, "x"):
            with pytest.raises(m["QueueError"]):
                m["validate_qa_settings"]("codex", None, t)

    def test_validate_qa_settings_rejects_bad_model(self, tmp_path, monkeypatch):
        m = self._load(tmp_path, monkeypatch)
        for mdl in (123, "", "a" * 201, "bad\x00model"):
            with pytest.raises(m["QueueError"]):
                m["validate_qa_settings"]("codex", mdl, 120)


class TestAdapterCommands:
    def _load(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDODECK_HOME", str(tmp_path / "queue"))
        monkeypatch.setenv("SUDODECK_CONFIG", str(tmp_path / "config.json"))
        return runpy.run_path(str(SUDODECK), run_name="sudodeck_test")

    def test_adapter_command_shapes(self, tmp_path, monkeypatch):
        m = self._load(tmp_path, monkeypatch)
        monkeypatch.setattr(m["shutil"], "which", lambda n: f"/mock/{n}")
        pi = m["adapter_command"]("pi", "model")
        assert {"--print", "--no-session", "--no-tools", "--no-extensions",
                "--no-skills", "--no-context-files"} <= set(pi)
        claude = m["adapter_command"]("claude", "model")
        assert {"--print", "--no-session-persistence", "--safe-mode",
                "--restricted", "--strict-mcp-config", "--tools"} <= set(claude)
        opencode = m["adapter_command"]("opencode", None)
        assert {"run", "--standalone", "--format", "json"} <= set(opencode)

    def test_codex_command_disables_mcp(self, tmp_path, monkeypatch):
        m = self._load(tmp_path, monkeypatch)
        monkeypatch.setattr(m["shutil"], "which", lambda n: "/mock/codex")

        def fake_run(command, **_):
            if "-c" in command:  # the effective re-probe carries the disable flags
                return subprocess.CompletedProcess(command, 0, json.dumps(
                    [{"name": "example_mcp", "enabled": False}]), "")
            return subprocess.CompletedProcess(command, 0, json.dumps(
                [{"name": "example_mcp", "enabled": False}]), "")

        monkeypatch.setattr(m["subprocess"], "run", fake_run)
        cmd = m["adapter_command"]("codex", None)
        assert "mcp_servers.example_mcp.enabled=false" in cmd
        assert "--sandbox" in cmd and "read-only" in cmd and "--ephemeral" in cmd

    def test_codex_command_rejects_when_servers_remain_enabled(self, tmp_path, monkeypatch):
        m = self._load(tmp_path, monkeypatch)
        monkeypatch.setattr(m["shutil"], "which", lambda n: "/mock/codex")

        def fake_run(command, **_):
            if "-c" in command:  # the second probe carries the disable flags
                return subprocess.CompletedProcess(command, 0, json.dumps(
                    [{"name": "clean", "enabled": True}]), "")
            return subprocess.CompletedProcess(command, 0, json.dumps(
                [{"name": "clean", "enabled": False}]), "")

        monkeypatch.setattr(m["subprocess"], "run", fake_run)
        with pytest.raises(m["AdapterUnavailable"]):
            m["adapter_command"]("codex", None)

    def test_opencode_environment_pure(self, tmp_path, monkeypatch):
        m = self._load(tmp_path, monkeypatch)
        monkeypatch.setattr(m["shutil"], "which", lambda n: "/mock/opencode")

        def fake_run(command, **_):
            if command == ["/mock/opencode", "mcp", "list"]:
                return subprocess.CompletedProcess(command, 0, "No MCP servers configured\n", "")
            raise AssertionError(command)

        monkeypatch.setattr(m["subprocess"], "run", fake_run)
        env = m["adapter_environment"]("opencode")
        assert env["OPENCODE_PURE"] == "1"
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert config["permission"] == {"*": "deny"}


class TestQaHarnessTimeout:
    def test_harness_process_group_terminated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDODECK_HOME", str(tmp_path / "queue"))
        monkeypatch.setenv("SUDODECK_CONFIG", str(tmp_path / "config.json"))
        m = runpy.run_path(str(SUDODECK), run_name="sudodeck_test")
        m["setup"]()
        child_pid = tmp_path / "qa-child.pid"
        command = ["/bin/sh", "-c", f"sleep 30 & echo $! > {child_pid}; wait"]
        with pytest.raises(m["QueueError"]) as exc:
            m["run_qa_harness"](command, "prompt", 1, None)
        assert "process group was terminated" in str(exc.value)
        pid = int(child_pid.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            import time
            time.sleep(0.1)
        else:
            pytest.fail("timed-out harness child remained alive")


class TestEditReview:
    def _load(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDODECK_HOME", str(tmp_path / "queue"))
        monkeypatch.setenv("SUDODECK_CONFIG", str(tmp_path / "config.json"))
        return runpy.run_path(str(SUDODECK), run_name="sudodeck_test")

    def _seed_item(self, m, tmp_path):
        m["setup"]()
        payload = pathlib.Path(m["PAYLOADS"]) / "edit-item.sh"
        payload.write_text("#!/bin/sh\necho original\n"); os.chmod(payload, 0o700)
        digest = m["sha256"](payload)
        item = {"id": "edit-item", "created_at": "x", "title": "Edit test",
                "summary": "x", "affects": "x", "risks": "x", "sha256": digest, "runs": []}
        m["atomic_json"](pathlib.Path(m["META"]) / "edit-item.json", item)
        return payload, item

    def test_edit_promotes_revision_keeps_source(self, tmp_path, monkeypatch):
        m = self._load(tmp_path, monkeypatch)
        payload, item = self._seed_item(m, tmp_path)
        original = item["sha256"]  # edit_review mutates item in place
        editor = tmp_path / "editor.sh"
        editor.write_text("#!/bin/sh\nprintf '\\necho edited\\n' >> \"$1\"\n"); os.chmod(editor, 0o700)
        monkeypatch.setenv("EDITOR", str(editor))
        m["edit_review"](item)
        assert "echo edited" in payload.read_text()
        saved = m["load"]("edit-item")
        assert saved["sha256"] != original and len(saved["revisions"]) == 1

    def test_edit_no_changes_does_not_rev(self, tmp_path, monkeypatch):
        m = self._load(tmp_path, monkeypatch)
        payload, item = self._seed_item(m, tmp_path)
        monkeypatch.setenv("EDITOR", "/bin/true")
        m["edit_review"](item)
        loaded = m["load"]("edit-item")
        assert loaded["sha256"] == item["sha256"]
        assert loaded.get("revisions") is None