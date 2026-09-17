from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys

from iii import host_backup
from iii.__main__ import build_parser
from iii.runner import inventory_parser
from iii_deployment.portable_state import PortableBackupController


ROOT = Path(__file__).resolve().parents[3]
POLICY = ROOT / "deployment/portable-state-policy.json"


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _archive(root: Path) -> dict:
    _json(
        root / "source/var/lib/iii/configuration/checkpoints/base.json",
        {"schema": "iii.configuration-checkpoint/v1", "gain": 1.0},
    )
    controller = PortableBackupController(
        source_root=root / "source",
        policy_path=POLICY,
        logical_target="drone",
        profile="real",
        active_release_id=lambda: "a" * 64,
        maintenance_safe=lambda: True,
        quiesce_writers=lambda: {"writers_stopped": True, "flushed": True},
        resume_standby=lambda: {"standby_resumed": True},
        now=lambda: "2026-08-27T12:00:00Z",
    )
    return controller.seal(operation_id="backup-cli-fixture")


def _args(root: Path, **values):
    return argparse.Namespace(
        registry_root=root / ".iii",
        _iii_environment={},
        _iii_operation_id="backup-cli-operation",
        **values,
    )


def test_parser_inventory_covers_backup_and_salvage_leaf_semantics() -> None:
    leaves = inventory_parser(build_parser())
    for leaf in ("list", "show", "verify", "status"):
        assert leaves[("host", "backup", leaf)].mutating is False
    for leaf in ("create", "export", "import", "restore", "prune"):
        spec = leaves[("host", "backup", leaf)]
        assert spec.mutating is True and spec.plan_provider is not None
    salvage = leaves[("host", "salvage")]
    assert salvage.mutating is True
    assert salvage.interactive is True
    assert salvage.plan_provider is not None


def test_salvage_invokes_workspace_module_not_uninstalled_console_helper(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    archive = tmp_path / "portable-state.tar"
    archive.write_bytes(b"portable state")
    record_path = tmp_path / "salvage-record.json"
    _json(
        record_path,
        {
            "schema": "iii.host-salvage-record/v1",
            "backup_id": "a" * 64,
            "salvage_id": "b" * 64,
            "recorded_at": "2026-09-17T00:00:00Z",
            "target_state_hash": "c" * 64,
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "operator_notice": "recommissioning required",
        },
    )
    command: list[str] = []

    def fake_run(argv, **_kwargs):
        command[:] = argv
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "record_path": str(record_path),
                        "archive_path": str(archive),
                    }
                ),
                "stderr": "",
            },
        )()

    def fake_store(_root, _archive, _record, *, operation_id):
        destination = root / "backups" / ("a" * 64)
        destination.mkdir(parents=True)
        copied = destination / "portable-state.tar"
        copied.write_bytes(archive.read_bytes())
        return {"archive_path": copied, "receipt": {"backup_id": "a" * 64}}

    monkeypatch.setattr(host_backup.subprocess, "run", fake_run)
    monkeypatch.setattr(host_backup, "_store_external", fake_store)
    monkeypatch.setattr(host_backup.os, "geteuid", lambda: 1000)
    args = argparse.Namespace(
        registry_root=root,
        device="/dev/loop-test",
        _iii_operation_id="salvage-cli-operation",
        _iii_retained_plan={"preflight": {"schema": "iii.host-salvage-plan/v1"}},
    )
    result = host_backup.salvage(args)

    assert result.outcome.value == "warning"
    assert command[0:11] == [
        "sudo",
        "-n",
        "env",
        f"PYTHONPATH={ROOT / 'deployment' / 'src'}",
        "unshare",
        "--mount",
        "--propagation",
        "private",
        "--",
        sys.executable,
        "-m",
    ]
    assert command[11] == "iii_deployment.portable_state"
    owner_index = command.index("--output-owner")
    assert command[owner_index + 1] == f"{host_backup.os.getuid()}:{host_backup.os.getgid()}"


def test_external_store_list_show_verify_export_import_and_duplicate(
    tmp_path: Path,
) -> None:
    sealed = _archive(tmp_path)
    archive = Path(sealed["archive_path"])
    detail = host_backup._store_external(
        tmp_path / ".iii",
        archive,
        sealed,
        operation_id="backup-cli-store",
    )
    receipt = detail["receipt"]
    assert receipt["external_verified"] is True
    assert receipt["fresh"] is True
    assert detail["archive_path"].is_file()

    listed = host_backup.list_backups(_args(tmp_path))
    assert listed.payload["backups"][0]["backup_id"] == receipt["backup_id"]
    shown = host_backup.show(_args(tmp_path, backup_id=receipt["backup_id"]))
    assert shown.payload["verification"]["verified"] is True
    verified = host_backup.verify(_args(tmp_path, backup_id=None))
    assert verified.payload["backups"] == [
        {
            "backup_id": receipt["backup_id"],
            "archive_sha256": receipt["archive_sha256"],
            "verified": True,
        }
    ]

    export_path = tmp_path / "external/portable.tar"
    export_args = _args(
        tmp_path,
        backup_id=receipt["backup_id"],
        destination=export_path,
    )
    export_plan = host_backup.export_preflight(export_args)
    export_args._iii_retained_plan = {"preflight": export_plan}
    exported = host_backup.export(export_args)
    assert exported.outcome.value == "success"
    assert (
        hashlib.sha256(export_path.read_bytes()).hexdigest()
        == receipt["archive_sha256"]
    )

    replacement = tmp_path / "replacement"
    import_args = _args(replacement, archive=export_path)
    import_plan = host_backup.import_preflight(import_args)
    import_args._iii_retained_plan = {"preflight": import_plan}
    imported = host_backup.import_backup(import_args)
    assert imported.payload["backup_id"] == receipt["backup_id"]
    duplicate = host_backup._store_external(
        replacement / ".iii",
        export_path,
        sealed,
        operation_id="backup-cli-duplicate",
    )
    assert duplicate["duplicate_content"] is True


def test_prune_refuses_retained_reference_then_removes_unreferenced_backup(
    tmp_path: Path,
) -> None:
    sealed = _archive(tmp_path)
    detail = host_backup._store_external(
        tmp_path / ".iii",
        Path(sealed["archive_path"]),
        sealed,
        operation_id="backup-cli-prune",
    )
    backup_id = detail["receipt"]["backup_id"]
    reference = tmp_path / ".iii/commissioning/restore.json"
    _json(reference, {"schema": "iii.restore-audit/v1", "backup_id": backup_id})
    args = _args(tmp_path, backup_id=backup_id)
    blocked = host_backup.prune_preflight(args)
    assert blocked["removable"] is False
    assert blocked["references"] == ["commissioning/restore.json"]
    args._iii_retained_plan = {"preflight": blocked}
    assert host_backup.prune(args).outcome.value == "rejected"

    reference.unlink()
    plan = host_backup._prune_state(tmp_path / ".iii", backup_id)
    assert plan["removable"] is True
    args._iii_retained_plan = {"preflight": plan}
    result = host_backup.prune(args)
    assert result.outcome.value == "success"
    assert not (tmp_path / ".iii/backups" / backup_id).exists()


def test_interrupted_download_cleans_partial_and_verified_chunks_converge(
    tmp_path: Path,
) -> None:
    sealed = _archive(tmp_path)
    raw = Path(sealed["archive_path"]).read_bytes()

    class Manager:
        def __init__(self, fail: bool) -> None:
            self.client_id = "f" * 64
            self.fail = fail
            self.lengths: list[int] = []

        def receiver_request(self, request):
            offset = request["payload"]["offset"]
            self.lengths.append(request["payload"]["length"])
            if self.fail and offset:
                raise RuntimeError("interrupted")
            block = raw[offset : offset + request["payload"]["length"]]
            return {
                "chunk": {
                    "data_base64": base64.b64encode(block).decode(),
                    "offset": offset,
                    "bytes": len(block),
                    "sha256": hashlib.sha256(block).hexdigest(),
                    "archive_sha256": hashlib.sha256(raw).hexdigest(),
                }
            }

    destination = tmp_path / "pull/portable.tar"
    destination.parent.mkdir()
    # Force a second chunk without allocating a multi-megabyte fixture by using
    # a deliberately small first requested response.
    original_request = host_backup._request

    def small_request(manager, **kwargs):
        kwargs["payload"]["length"] = min(kwargs["payload"]["length"], 1024)
        return original_request(manager, **kwargs)

    host_backup._request = small_request
    try:
        try:
            host_backup._download(Manager(True), sealed, destination)
        except RuntimeError:
            pass
        assert not destination.exists()
        assert not list(destination.parent.glob("*.partial*"))
        manager = Manager(False)
        host_backup._download(manager, sealed, destination)
        assert max(manager.lengths) <= 512 * 1024
    finally:
        host_backup._request = original_request
    assert destination.read_bytes() == raw
