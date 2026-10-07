#!/usr/bin/env python3
"""Unit tests for Q&A settings precedence and safe adapter command shapes."""
import json
import os
import runpy
import subprocess
import tempfile


with tempfile.TemporaryDirectory() as work:
    os.environ["SCRIPTDECK_HOME"] = f"{work}/queue"
    os.environ["SCRIPTDECK_CONFIG"] = f"{work}/config/config.json"
    module = runpy.run_path("scriptdeck.py", run_name="scriptdeck_test")

    module["save_qa_setting"]("backend", "pi")
    module["save_qa_setting"]("model", "safe-model")
    module["save_qa_setting"]("timeout_seconds", 45)
    assert module["qa_settings"]() == ("pi", "safe-model", 45)

    os.environ["SCRIPTDECK_QA_BACKEND"] = "claude"
    os.environ["SCRIPTDECK_QA_MODEL"] = "override-model"
    os.environ["SCRIPTDECK_QA_TIMEOUT_SECONDS"] = "30"
    assert module["qa_settings"]() == ("claude", "override-model", 30)
    del os.environ["SCRIPTDECK_QA_BACKEND"]
    del os.environ["SCRIPTDECK_QA_MODEL"]
    del os.environ["SCRIPTDECK_QA_TIMEOUT_SECONDS"]

    original_which = module["shutil"].which
    original_run = module["subprocess"].run
    module["shutil"].which = lambda name: f"/mock/{name}"
    try:
        pi = module["adapter_command"]("pi", "model")
        assert {"--print", "--no-session", "--no-tools", "--no-extensions", "--no-skills", "--no-context-files"} <= set(pi)
        claude = module["adapter_command"]("claude", "model")
        assert {"--print", "--no-session-persistence", "--safe-mode", "--restricted", "--strict-mcp-config", "--tools"} <= set(claude)
        try:
            module["adapter_command"]("opencode", None)
        except module["AdapterUnavailable"]:
            pass
        else:
            raise AssertionError("OpenCode must fail closed")

        def fake_run(command, **_):
            if command[-3:] == ["mcp", "list", "--json"]:
                return subprocess.CompletedProcess(command, 0, json.dumps([{"name": "example_mcp", "enabled": False}]), "")
            raise AssertionError(command)

        module["subprocess"].run = fake_run
        codex = module["adapter_command"]("codex", None)
        assert "mcp_servers.example_mcp.enabled=false" in codex
        assert "--sandbox" in codex and "read-only" in codex
    finally:
        module["shutil"].which = original_which
        module["subprocess"].run = original_run

print("Q&A adapter tests passed")
