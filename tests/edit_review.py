#!/usr/bin/env python3
import os
import pathlib
import runpy
import tempfile


with tempfile.TemporaryDirectory() as work:
    os.environ["SUDODECK_HOME"] = f"{work}/queue"
    os.environ["SUDODECK_CONFIG"] = f"{work}/config.json"
    module = runpy.run_path("sudodeck.py", run_name="sudodeck_edit_test")
    module["setup"]()
    payload = pathlib.Path(module["PAYLOADS"]) / "edit-item.sh"
    payload.write_text("#!/bin/sh\necho original\n")
    os.chmod(payload, 0o700)
    digest = module["sha256"](payload)
    item = {"id": "edit-item", "created_at": "x", "title": "Edit test", "summary": "x", "affects": "x", "risks": "x", "sha256": digest, "runs": []}
    module["atomic_json"](pathlib.Path(module["META"]) / "edit-item.json", item)

    source = pathlib.Path(work) / "original.sh"
    source.write_text(payload.read_text())
    editor = pathlib.Path(work) / "editor.sh"
    editor.write_text("#!/bin/sh\n[ \"$1\" = --wait ] && shift\nprintf '\necho edited\n' >> \"$1\"\n")
    os.chmod(editor, 0o700)
    os.environ["EDITOR"] = f"{editor} --wait"
    module["edit_review"](item)
    assert "echo edited" in payload.read_text()
    assert source.read_text() == "#!/bin/sh\necho original\n"
    saved = module["load"]("edit-item")
    assert saved["sha256"] != digest and len(saved["revisions"]) == 1
    assert "echo edited" in module["qa_prompt"](payload, "what changed?")
    assert not list(pathlib.Path(module["RUNS"]).glob("**/result.json"))

    # An old successful run stays in history but cannot complete a new revision.
    run_dir = pathlib.Path(module["RUNS"]) / "edit-item" / "old-run"
    run_dir.mkdir(parents=True)
    module["atomic_json"](run_dir / "result.json", {"status": "succeeded", "started_at": "z", "sha256": saved["sha256"], "exit_code": 0})
    editor.write_text("#!/bin/sh\nprintf '\necho second revision\n' >> \"$1\"\n")
    os.environ["EDITOR"] = str(editor)
    module["edit_review"](saved)
    revised = module["load"]("edit-item")
    assert len(revised["revisions"]) == 2
    assert not module["is_completed"](revised)
    assert "PENDING REVISION" in module["execution_label"](revised)
    assert (run_dir / "result.json").exists()

    before = revised["sha256"]
    os.environ["EDITOR"] = "/bin/true"
    module["edit_review"](revised)
    assert module["load"]("edit-item")["sha256"] == before

    failing = pathlib.Path(work) / "failing-editor.sh"
    failing.write_text("#!/bin/sh\nexit 7\n")
    os.chmod(failing, 0o700)
    os.environ["EDITOR"] = str(failing)
    module["edit_review"](revised)
    assert module["load"]("edit-item")["sha256"] == before
    assert list(pathlib.Path(module["PAYLOADS"]).glob(".edit-edit-item-*.sh"))

    os.environ["EDITOR"] = "/not/a/real/editor"
    module["edit_review"](revised)
    assert module["load"]("edit-item")["sha256"] == before

    concurrent = pathlib.Path(work) / "concurrent-editor.sh"
    concurrent.write_text("#!/bin/sh\nprintf '\necho draft only\n' >> \"$1\"\nprintf '# concurrent write\n' >> \"$QUEUE_PAYLOAD\"\n")
    os.chmod(concurrent, 0o700)
    os.environ["EDITOR"] = str(concurrent)
    os.environ["QUEUE_PAYLOAD"] = str(payload)
    module["edit_review"](revised)
    assert module["load"]("edit-item")["sha256"] == before
    assert len(list(pathlib.Path(module["PAYLOADS"]).glob(".edit-edit-item-*.sh"))) >= 2

print("editor review tests passed")
