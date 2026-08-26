from __future__ import annotations

import base64
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

from iii import logs
from iii.__main__ import main


def target() -> dict:
    return {
        "selector": "real",
        "endpoint": "iii.local",
        "logical_id": "drone",
        "execution_host": "aircraft",
        "runtime_profile": "real",
    }


def manifest(content: bytes, *, protected: bool = False) -> dict:
    value = {
        "schema": "iii.log-export-manifest/v1",
        "domain": "logs",
        "files": [
            {
                "locator": "var/log/iii/session/runtime.jsonl",
                "content_id": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "protected": protected,
            }
        ],
        "total_bytes": len(content),
    }
    return {
        **value,
        "manifest_id": logs.content_id(value),
        "created_at": "2026-08-26T12:00:00Z",
    }


class Manager:
    client_id = "b" * 64

    def __init__(self, content: bytes, source_manifest: dict):
        self.content = content
        self.manifest = source_manifest
        self.actions: list[str] = []
        self.fail_after_chunks: int | None = None
        self.corrupt = False
        self.prune_plan = {
            "schema": "iii.log-prune-plan/v1",
            "plan_id": "c" * 64,
            "receipt_id": "d" * 64,
            "manifest_id": source_manifest["manifest_id"],
            "remove": source_manifest["files"],
            "protected": [],
        }

    def receiver_request(self, request):
        action = request["action"]
        self.actions.append(action)
        if action == "log-export":
            return {"manifest": self.manifest}
        if action == "log-chunk":
            chunks = self.actions.count("log-chunk")
            if self.fail_after_chunks is not None and chunks > self.fail_after_chunks:
                raise RuntimeError("link interrupted")
            offset = request["payload"]["offset"]
            data = self.content[offset : offset + request["payload"]["length"]]
            if self.corrupt:
                data = b"corrupt" + data[7:]
            return {
                "chunk": {
                    "schema": "iii.log-chunk/v1",
                    "manifest_id": self.manifest["manifest_id"],
                    "content_id": self.manifest["files"][0]["content_id"],
                    "offset": offset,
                    "data": base64.b64encode(data).decode("ascii"),
                    "eof": offset + len(data) == len(self.content),
                }
            }
        if action == "plan-log-receipt":
            return {
                "plan": {
                    "plan_id": "e" * 64,
                    "parameters": {"receipt_id": "d" * 64},
                },
                "nonce": "f" * 64,
            }
        if action == "log-receipt":
            return {"detached": True, "operation": {"state": "accepted"}}
        if action == "plan-log-prune":
            return {
                "plan": {
                    "plan_id": "1" * 64,
                    "parameters": {"prune_plan": self.prune_plan},
                },
                "nonce": "2" * 64,
            }
        if action == "log-prune":
            return {"detached": True, "operation": {"state": "accepted"}}
        raise AssertionError(action)


def args(tmp_path: Path, source_manifest: dict, operation: str = "iii-log-pull-test"):
    destination = tmp_path / "registry"
    return SimpleNamespace(
        log_domain="logs",
        destination=destination,
        target="real",
        _iii_operation_id=operation,
        _iii_environment={"III_OPERATION_STATE_DIR": str(tmp_path / "operations")},
        _iii_retained_plan={
            "preflight": {
                "manifest": source_manifest,
                "target": {"logical_id": "drone", "profile": "real"},
                "destination": str(destination / "log-pulls"),
            }
        },
    )


def test_pull_hash_verifies_before_receipt_and_duplicate_is_idempotent(
    monkeypatch, tmp_path: Path
) -> None:
    content = b'{"event":"boot"}\n'
    source_manifest = manifest(content)
    manager = Manager(content, source_manifest)
    monkeypatch.setattr(logs, "_target", lambda _args: target())
    monkeypatch.setattr(logs, "_manager", lambda: manager)

    first = logs.pull(args(tmp_path, source_manifest))

    assert first.outcome.value == "success"
    assert manager.actions == ["log-chunk", "plan-log-receipt", "log-receipt"]
    local = Path(first.payload["local_root"])
    assert (local / "pull-manifest.json").is_file()
    assert (local / "source-manifest.json").is_file()
    assert (local / "files/var/log/iii/session/runtime.jsonl").read_bytes() == content
    assert stat.S_IMODE((local / "pull-manifest.json").stat().st_mode) == 0o440
    assert stat.S_IMODE(local.stat().st_mode) == 0o550

    manager.actions.clear()
    duplicate = logs.pull(
        args(tmp_path, source_manifest, operation="iii-log-pull-duplicate")
    )
    assert duplicate.outcome.value == "success"
    assert manager.actions == ["plan-log-receipt", "log-receipt"]
    assert duplicate.payload["local_manifest"] == first.payload["local_manifest"]


def test_duplicate_pull_rejects_rebound_local_manifest(
    monkeypatch, tmp_path: Path
) -> None:
    content = b"verified"
    source_manifest = manifest(content)
    manager = Manager(content, source_manifest)
    monkeypatch.setattr(logs, "_target", lambda _args: target())
    monkeypatch.setattr(logs, "_manager", lambda: manager)
    first = logs.pull(args(tmp_path, source_manifest))
    root = Path(first.payload["local_root"])
    path = root / "pull-manifest.json"
    local = json.loads(path.read_text(encoding="utf-8"))
    local["target"]["logical_id"] = "another-drone"
    local["local_manifest_id"] = logs.content_id(
        {
            key: item
            for key, item in local.items()
            if key not in {"local_manifest_id", "completed_at"}
        }
    )
    os.chmod(root, 0o750)
    os.chmod(path, 0o640)
    path.write_bytes(logs._canonical(local) + b"\n")
    os.chmod(path, 0o440)
    os.chmod(root, 0o550)
    manager.actions.clear()

    duplicate = logs.pull(
        args(tmp_path, source_manifest, operation="iii-log-pull-rebound")
    )

    assert duplicate.outcome.value == "rejected"
    assert manager.actions == []


def test_corrupt_pull_records_no_receipt(monkeypatch, tmp_path: Path) -> None:
    content = b"expected-content"
    source_manifest = manifest(content)
    manager = Manager(content, source_manifest)
    manager.corrupt = True
    monkeypatch.setattr(logs, "_target", lambda _args: target())
    monkeypatch.setattr(logs, "_manager", lambda: manager)

    result = logs.pull(args(tmp_path, source_manifest))

    assert result.outcome.value == "rejected"
    assert manager.actions == ["log-chunk"]
    assert not list((tmp_path / "registry/log-pulls").glob("[!.]*"))


def test_pull_rejects_stale_target_or_destination_before_transfer(
    monkeypatch, tmp_path: Path
) -> None:
    content = b"stable"
    source_manifest = manifest(content)
    manager = Manager(content, source_manifest)
    monkeypatch.setattr(logs, "_target", lambda _args: target())
    monkeypatch.setattr(logs, "_manager", lambda: manager)
    stale = args(tmp_path, source_manifest)
    stale._iii_retained_plan["preflight"]["destination"] = str(tmp_path / "other")

    result = logs.pull(stale)

    assert result.outcome.value == "rejected"
    assert manager.actions == []


def test_interrupted_pull_resumes_partial_without_receipt(
    monkeypatch, tmp_path: Path
) -> None:
    content = b"a" * (logs.CHUNK_BYTES + 41)
    source_manifest = manifest(content)
    interrupted = Manager(content, source_manifest)
    interrupted.fail_after_chunks = 1
    monkeypatch.setattr(logs, "_target", lambda _args: target())
    monkeypatch.setattr(logs, "_manager", lambda: interrupted)

    first = logs.pull(args(tmp_path, source_manifest))

    assert first.outcome.value == "rejected"
    assert interrupted.actions == ["log-chunk", "log-chunk"]
    partial = next((tmp_path / "registry/log-pulls").glob(".*.partial"))
    partial_file = partial / "files/var/log/iii/session/runtime.jsonl"
    assert partial_file.stat().st_size == logs.CHUNK_BYTES

    resumed = Manager(content, source_manifest)
    monkeypatch.setattr(logs, "_manager", lambda: resumed)
    second = logs.pull(args(tmp_path, source_manifest))
    assert second.outcome.value == "success"
    assert resumed.actions == ["log-chunk", "plan-log-receipt", "log-receipt"]


def test_interrupted_multifile_pull_reuses_completed_read_only_file(
    monkeypatch, tmp_path: Path
) -> None:
    content = b"same-content"
    source_manifest = manifest(content)
    source_manifest["files"].append(
        {**source_manifest["files"][0], "locator": "var/log/iii/second.jsonl"}
    )
    source_manifest["total_bytes"] = len(content) * 2
    source_manifest["manifest_id"] = logs._manifest_identity(source_manifest)
    interrupted = Manager(content, source_manifest)
    interrupted.fail_after_chunks = 1
    monkeypatch.setattr(logs, "_target", lambda _args: target())
    monkeypatch.setattr(logs, "_manager", lambda: interrupted)

    first = logs.pull(args(tmp_path, source_manifest))

    assert first.outcome.value == "rejected"
    completed = next((tmp_path / "registry/log-pulls").glob(".*.partial"))
    first_file = completed / "files/var/log/iii/session/runtime.jsonl"
    assert stat.S_IMODE(first_file.stat().st_mode) == 0o440

    resumed = Manager(content, source_manifest)
    monkeypatch.setattr(logs, "_manager", lambda: resumed)
    second = logs.pull(args(tmp_path, source_manifest))

    assert second.outcome.value == "success"
    assert resumed.actions == ["log-chunk", "plan-log-receipt", "log-receipt"]


def test_prune_revalidates_exact_receiver_targets(monkeypatch, tmp_path: Path) -> None:
    content = b"log"
    source_manifest = manifest(content)
    manager = Manager(content, source_manifest)
    monkeypatch.setattr(logs, "_target", lambda _args: target())
    monkeypatch.setattr(logs, "_manager", lambda: manager)
    prune_args = SimpleNamespace(
        pulled="d" * 64,
        target="real",
        _iii_operation_id="iii-log-prune-test",
        _iii_retained_plan={"preflight": {"prune_plan": manager.prune_plan}},
    )

    accepted = logs.prune(prune_args)
    assert accepted.outcome.value == "success"
    assert manager.actions == ["plan-log-prune", "log-prune"]

    manager.actions.clear()
    manager.prune_plan = {
        **manager.prune_plan,
        "remove": [],
        "protected": source_manifest["files"],
    }
    refused = logs.prune(prune_args)
    assert refused.outcome.value == "rejected"
    assert manager.actions == ["plan-log-prune"]


def test_dry_run_apply_reuses_exact_snapshot_manifest(
    monkeypatch, tmp_path: Path
) -> None:
    content = b"stable snapshot"
    source_manifest = manifest(content)
    manager = Manager(content, source_manifest)
    monkeypatch.setattr(logs, "_target", lambda _args: target())
    monkeypatch.setattr(logs, "_manager", lambda: manager)
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path / "operations"))
    argv = ["logs", "pull", "--destination", str(tmp_path / "registry")]
    output = StringIO()

    assert main([*argv, "--dry-run", "--json"], stdout=output, stderr=StringIO()) == 0
    operation_id = json.loads(output.getvalue())["operation"]["id"]
    assert manager.actions == ["log-export"]

    output = StringIO()
    assert (
        main(
            [
                *argv,
                "--operation-id",
                operation_id,
                "--confirm",
                "--non-interactive",
                "--json",
            ],
            stdout=output,
            stderr=StringIO(),
        )
        == 0
    )
    assert manager.actions == [
        "log-export",
        "log-chunk",
        "plan-log-receipt",
        "log-receipt",
    ]
