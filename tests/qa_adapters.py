#!/usr/bin/env python3
"""Unit tests for Q&A settings precedence and safe adapter command shapes."""
import json
import os
import pathlib
import runpy
import subprocess
import tempfile
import time


with tempfile.TemporaryDirectory() as work:
    os.environ["SUDODECK_HOME"] = f"{work}/queue"
    os.environ["SUDODECK_CONFIG"] = f"{work}/config/config.json"
    module = runpy.run_path("sudodeck.py", run_name="sudodeck_test")

    module["save_qa_setting"]("backend", "pi")
    module["save_qa_setting"]("model", "safe-model")
    module["save_qa_setting"]("timeout_seconds", 45)
    assert module["qa_settings"]() == ("pi", "safe-model", 45)

    os.environ["SUDODECK_QA_BACKEND"] = "claude"
    os.environ["SUDODECK_QA_MODEL"] = "override-model"
    os.environ["SUDODECK_QA_TIMEOUT_SECONDS"] = "30"
    assert module["qa_settings"]() == ("claude", "override-model", 30)
    del os.environ["SUDODECK_QA_BACKEND"]
    del os.environ["SUDODECK_QA_MODEL"]
    del os.environ["SUDODECK_QA_TIMEOUT_SECONDS"]

    original_which = module["shutil"].which
    original_run = module["subprocess"].run
    module["shutil"].which = lambda name: f"/mock/{name}"
    try:
        pi = module["adapter_command"]("pi", "model")
        assert {"--print", "--no-session", "--no-tools", "--no-extensions", "--no-skills", "--no-context-files"} <= set(pi)
        claude = module["adapter_command"]("claude", "model")
        assert {"--print", "--no-session-persistence", "--safe-mode", "--restricted", "--strict-mcp-config", "--tools"} <= set(claude)
        opencode = module["adapter_command"]("opencode", None)
        assert {"run", "--standalone", "--format", "json"} <= set(opencode)

        def fake_run(command, **_):
            if command[-3:] == ["mcp", "list", "--json"]:
                return subprocess.CompletedProcess(command, 0, json.dumps([{"name": "example_mcp", "enabled": False}]), "")
            if command[-2:] == ["mcp", "list"]:
                return subprocess.CompletedProcess(command, 0, "No MCP servers configured\n", "")
            raise AssertionError(command)

        module["subprocess"].run = fake_run
        codex = module["adapter_command"]("codex", None)
        assert "mcp_servers.example_mcp.enabled=false" in codex
        assert "--sandbox" in codex and "read-only" in codex
        environment = module["adapter_environment"]("opencode")
        assert environment["OPENCODE_PURE"] == "1"
        assert environment["OPENCODE_CONFIG_CONTENT"]
    finally:
        module["shutil"].which = original_which
        module["subprocess"].run = original_run

    # A timed-out harness may fork; both the launcher and its child must go.
    module["setup"]()
    child_pid = pathlib.Path(work) / "qa-child.pid"
    command = ["/bin/sh", "-c", f"sleep 30 & echo $! > {child_pid}; wait"]
    try:
        module["run_qa_harness"](command, "ignored prompt", 1, None)
        raise AssertionError("expected timeout")
    except module["QueueError"] as exc:
        assert "process group was terminated" in str(exc)
    pid = int(child_pid.read_text())
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("timed-out harness child remained alive")

print("Q&A adapter tests passed")
