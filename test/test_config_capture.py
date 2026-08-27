from __future__ import annotations

from io import StringIO
import json
from pathlib import Path

import pytest

from iii.__main__ import build_parser, main
from iii.runner import inventory_parser
from iii_drone_contracts.configuration_capture import content_identity


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "deps").mkdir(parents=True)
    (workspace / "deps/submodule-lock.txt").write_text("fixture\n")
    return workspace


def _environment(workspace: Path) -> dict[str, str]:
    return {
        "CLI_CONFIGURATION": "dev",
        "WORKSPACE_DIR": str(workspace),
        "III_CAPTURE_ROOT": str(workspace / ".iii/captures"),
        "III_OPERATION_STATE_DIR": str(workspace / ".iii/operations"),
        "III_RUNTIME_API_CLI_TOKEN": "fixture-token",
    }


def _invoke(argv: list[str], environment: dict[str, str]) -> tuple[int, dict]:
    stdout, stderr = StringIO(), StringIO()
    status = main(
        [*argv, "--json"],
        stdout=stdout,
        stderr=stderr,
        environment=environment,
    )
    assert stderr.getvalue() == ""
    return status, json.loads(stdout.getvalue())


def _source(snapshot_id: str, profile: str) -> dict:
    suffix = "1" if "one" in snapshot_id else "2"
    head = {
        "schema": "iii.configuration-tuning-wal-entry/v1",
        "sequence": 8,
        "previous_checksum": "e" * 64,
        "checksum": "",
        "kind": "committed",
        "timestamp": "2026-08-27T12:00:04Z",
        "session_id": "c" * 64,
        "transaction_id": "f" * 64,
        "request_id": "request-8",
        "revision": 4,
        "body": {"operator_id": "operator-test"},
    }
    head["checksum"] = content_identity(
        {key: item for key, item in head.items() if key != "checksum"}
    )
    return {
        "schema": "iii.configuration-capture-source/v1",
        "snapshot_id": snapshot_id,
        "snapshot_content_sha256": suffix * 64,
        "values": {"/control/gain": float(suffix)},
        "parameter_document": {
            "/**": {"ros__parameters": {"/control/gain": float(suffix)}},
            "/sensor/example": {"ros__parameters": {"frame_id": "sensor"}},
        },
        "target_id": "drone-1" if profile == "real" else "sim",
        "runtime_profile": profile,
        "release_id": "a" * 64,
        "workspace_id": "workspace-test",
        "manifest_id": "b" * 64,
        "session_id": "c" * 64,
        "baseline_id": "d" * 64,
        "baseline_values": {"/control/gain": 0.5},
        "session_created_at": "2026-08-27T12:00:00Z",
        "journal_updated_at": "2026-08-27T12:00:04Z",
        "journal_revision": 4,
        "journal_sequence": 8,
        "journal_checksum": head["checksum"],
        "journal_head_entry": head,
        "pending_boot_values": {},
        "source_is_active": snapshot_id.endswith("one.yaml"),
        "source_is_default": False,
    }


class FakeRuntimeClient:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.deleted = []
        self.fail_snapshot = None
        self.__class__.instances.append(self)

    def configuration_capture_source(self, *, snapshot_id, expected_profile):
        if snapshot_id == self.fail_snapshot:
            raise OSError("interrupted transfer")
        return _source(snapshot_id, expected_profile)

    def delete_configuration_snapshot(self, request):
        self.deleted.append(request)
        return {
            "schema": "iii.configuration-snapshot-delete-result/v1",
            "snapshot_id": request["snapshot_id"],
            "content_sha256": "1" * 64,
            "forced": request["force"],
            "deleted": True,
        }


def _pull_args(operation_id: str) -> list[str]:
    return [
        "config",
        "capture",
        "pull",
        "--target",
        "sim",
        "--snapshot",
        "snapshots/one.yaml",
        "--name",
        "First tune",
        "--description",
        "Stable simulation gain set",
        "--operation-id",
        operation_id,
        "--confirm",
        "--non-interactive",
    ]


def test_capture_leaves_declare_mutation_and_plan_contracts():
    inventory = inventory_parser(build_parser())
    expected = {
        "pull": True,
        "list": False,
        "show": False,
        "diff": False,
        "verify": False,
        "export": True,
        "import": True,
        "delete": True,
    }
    for leaf, mutating in expected.items():
        spec = inventory[("config", "capture", leaf)]
        assert spec.mutating is mutating
        assert bool(spec.plan_provider) is mutating
        assert spec.interactive is False


def test_multi_capture_repeat_metadata_offline_verify_and_no_target_mutation(
    tmp_path, monkeypatch
):
    import iii.config_capture as module

    FakeRuntimeClient.instances.clear()
    monkeypatch.setattr(module, "RuntimeApiClient", FakeRuntimeClient)
    workspace = _workspace(tmp_path)
    environment = _environment(workspace)
    argv = _pull_args("capture-pull-0001")
    insert_at = argv.index("--operation-id")
    argv[insert_at:insert_at] = [
        "--snapshot",
        "snapshots/two.yaml",
        "--name",
        "First tune",
        "--description",
        "Independent inactive set",
    ]

    status, pulled = _invoke(argv, environment)
    assert status == 0
    assert len(pulled["payload"]["capture_ids"]) == 2
    capture_ids = pulled["payload"]["capture_ids"]
    assert all(
        (workspace / ".iii/captures" / capture_id / "capture.json").is_file()
        for capture_id in capture_ids
    )
    assert all(instance.deleted == [] for instance in FakeRuntimeClient.instances)
    listed_status, listed = _invoke(["config", "capture", "list"], environment)
    assert listed_status == 0
    assert [
        item["metadata"][0]["short_name"] for item in listed["payload"]["captures"]
    ] == ["First tune", "First tune"]
    show_status, shown = _invoke(
        ["config", "capture", "show", capture_ids[0]], environment
    )
    diff_status, compared = _invoke(
        ["config", "capture", "diff", capture_ids[0], "--against", "baseline"],
        environment,
    )
    assert (
        show_status == 0 and shown["payload"]["capture"]["capture_id"] == capture_ids[0]
    )
    assert diff_status == 0 and compared["payload"]["changes"] == [
        {"name": "/control/gain", "capture": 1.0, "against": 0.5}
    ]

    status, verified = _invoke(
        ["config", "capture", "verify", *capture_ids], environment
    )
    assert status == 0 and verified["payload"]["valid"] is True

    repeat = _pull_args("capture-pull-0002")
    repeat[repeat.index("First tune")] = "Repeated display name"
    status, repeated = _invoke(repeat, environment)
    assert status == 0
    assert repeated["payload"]["capture_ids"] == [capture_ids[0]]
    metadata = list(
        (workspace / ".iii/captures" / capture_ids[0] / "metadata").glob("*.json")
    )
    assert len(metadata) == 2


def test_capture_export_import_deduplicates_and_tamper_fails(tmp_path, monkeypatch):
    import iii.config_capture as module

    monkeypatch.setattr(module, "RuntimeApiClient", FakeRuntimeClient)
    source_workspace = _workspace(tmp_path / "source")
    source_environment = _environment(source_workspace)
    status, pulled = _invoke(_pull_args("capture-export-source"), source_environment)
    assert status == 0
    capture_id = pulled["payload"]["capture_ids"][0]
    archive = tmp_path / "captures.zip"
    status, exported = _invoke(
        [
            "config",
            "capture",
            "export",
            "--capture-id",
            capture_id,
            "--archive",
            str(archive),
            "--operation-id",
            "capture-export-0001",
            "--confirm",
            "--non-interactive",
        ],
        source_environment,
    )
    assert status == 0 and exported["payload"]["archive_sha256"]

    destination_workspace = _workspace(tmp_path / "destination")
    destination_environment = _environment(destination_workspace)
    import_argv = [
        "config",
        "capture",
        "import",
        str(archive),
        "--operation-id",
        "capture-import-0001",
        "--confirm",
        "--non-interactive",
    ]
    status, imported = _invoke(import_argv, destination_environment)
    assert status == 0 and imported["payload"]["capture_ids"] == [capture_id]

    status, imported_again = _invoke(
        [
            *import_argv[:4],
            "--operation-id",
            "capture-import-0002",
            "--confirm",
            "--non-interactive",
        ],
        destination_environment,
    )
    assert status == 0 and imported_again["payload"]["deduplicated"] is True

    capture_path = destination_workspace / ".iii/captures" / capture_id / "capture.json"
    value = json.loads(capture_path.read_text())
    value["source"]["values"]["/control/gain"] = 99.0
    capture_path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    )
    status, rejected = _invoke(
        ["config", "capture", "verify", capture_id], destination_environment
    )
    assert status == 20
    assert rejected["code"] == "III_CONFIG_CAPTURE_VERIFY_REJECTED"


def test_archive_publish_retains_partial_bytes_when_atomic_link_fails(
    tmp_path, monkeypatch
):
    import iii.config_capture as module

    destination = tmp_path / "captures.zip"
    monkeypatch.setattr(
        module.os,
        "link",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk detached")),
    )

    with pytest.raises(OSError, match="disk detached"):
        module._publish_bytes_without_overwrite(destination, b"archive bytes")

    assert not destination.exists()
    partials = list(tmp_path.glob(".captures.zip.*.partial"))
    assert len(partials) == 1
    assert partials[0].read_bytes() == b"archive bytes"


def test_interrupted_import_is_marked_and_resumes_without_overwrite(
    tmp_path, monkeypatch
):
    import iii.config_capture as module

    monkeypatch.setattr(module, "RuntimeApiClient", FakeRuntimeClient)
    source_workspace = _workspace(tmp_path / "source")
    source_environment = _environment(source_workspace)
    argv = _pull_args("capture-import-partial-source")
    insert_at = argv.index("--operation-id")
    argv[insert_at:insert_at] = [
        "--snapshot",
        "snapshots/two.yaml",
        "--name",
        "Second tune",
        "--description",
        "Second archived set",
    ]
    status, pulled = _invoke(argv, source_environment)
    assert status == 0
    archive = tmp_path / "two-captures.zip"
    status, _ = _invoke(
        [
            "config",
            "capture",
            "export",
            *sum(
                (["--capture-id", item] for item in pulled["payload"]["capture_ids"]),
                [],
            ),
            "--archive",
            str(archive),
            "--operation-id",
            "capture-import-partial-export",
            "--confirm",
            "--non-interactive",
        ],
        source_environment,
    )
    assert status == 0

    destination_workspace = _workspace(tmp_path / "destination")
    destination_environment = _environment(destination_workspace)
    original = module._immutable_document
    capture_writes = 0

    def fail_second_capture(path, value):
        nonlocal capture_writes
        if path.name == "capture.json":
            capture_writes += 1
            if capture_writes == 2:
                raise OSError("target filesystem unavailable")
        return original(path, value)

    monkeypatch.setattr(module, "_immutable_document", fail_second_capture)
    status, rejected = _invoke(
        [
            "config",
            "capture",
            "import",
            str(archive),
            "--operation-id",
            "capture-import-partial-0001",
            "--confirm",
            "--non-interactive",
        ],
        destination_environment,
    )
    assert status == 20
    assert rejected["code"] == "III_CONFIG_CAPTURE_IMPORT_REJECTED"
    marker = next(
        (destination_workspace / ".iii/captures/.partial").glob("import-*.json")
    )
    partial = json.loads(marker.read_text())
    assert partial["status"] == "interrupted"
    assert len(partial["completed_capture_ids"]) == 1

    monkeypatch.setattr(module, "_immutable_document", original)
    status, recovered = _invoke(
        [
            "config",
            "capture",
            "import",
            str(archive),
            "--operation-id",
            "capture-import-partial-0002",
            "--confirm",
            "--non-interactive",
        ],
        destination_environment,
    )
    assert status == 0
    assert sorted(recovered["payload"]["capture_ids"]) == sorted(
        pulled["payload"]["capture_ids"]
    )
    assert not marker.exists()


def test_interrupted_multi_pull_retains_explicit_partial_marker(tmp_path, monkeypatch):
    import iii.config_capture as module

    class InterruptingClient(FakeRuntimeClient):
        def configuration_capture_source(self, *, snapshot_id, expected_profile):
            if snapshot_id.endswith("two.yaml"):
                raise OSError("interrupted transfer")
            return super().configuration_capture_source(
                snapshot_id=snapshot_id, expected_profile=expected_profile
            )

    monkeypatch.setattr(module, "RuntimeApiClient", InterruptingClient)
    workspace = _workspace(tmp_path)
    environment = _environment(workspace)
    argv = _pull_args("capture-partial-0001")
    insert_at = argv.index("--operation-id")
    argv[insert_at:insert_at] = [
        "--snapshot",
        "snapshots/two.yaml",
        "--name",
        "Second tune",
        "--description",
        "Transfer interruption fixture",
    ]

    status, result = _invoke(argv, environment)

    assert status == 20
    assert result["code"] == "III_CONFIG_CAPTURE_PULL_REJECTED"
    partials = list((workspace / ".iii/captures/.partial").glob("*.json"))
    assert len(partials) == 1
    partial = json.loads(partials[0].read_text())
    assert partial["status"] == "interrupted"
    assert len(partial["completed_capture_ids"]) == 1


def test_snapshot_delete_uses_verified_receipt_and_separates_confirmed_force(
    tmp_path, monkeypatch
):
    import iii.config_capture as module

    FakeRuntimeClient.instances.clear()
    monkeypatch.setattr(module, "RuntimeApiClient", FakeRuntimeClient)
    workspace = _workspace(tmp_path)
    environment = _environment(workspace)
    status, pulled = _invoke(_pull_args("capture-delete-source"), environment)
    assert status == 0
    capture_id = pulled["payload"]["capture_ids"][0]

    status, deleted = _invoke(
        [
            "config",
            "capture",
            "delete",
            "--target",
            "sim",
            "--snapshot",
            "snapshots/one.yaml",
            "--capture-id",
            capture_id,
            "--operation-id",
            "capture-delete-0001",
            "--confirm",
            "--non-interactive",
        ],
        environment,
    )
    assert status == 0 and deleted["payload"]["result"]["deleted"] is True
    request = FakeRuntimeClient.instances[-1].deleted[0]
    assert request["force"] is False
    assert request["capture_receipt"]["capture_id"] == capture_id

    status, unconfirmed = _invoke(
        [
            "config",
            "capture",
            "delete",
            "--target",
            "sim",
            "--snapshot",
            "snapshots/two.yaml",
            "--force",
            "--operation-id",
            "capture-delete-force-bad",
            "--confirm",
            "--non-interactive",
        ],
        environment,
    )
    assert status == 20
    assert unconfirmed["code"] == "III_OPERATION_ERROR"

    status, forced = _invoke(
        [
            "config",
            "capture",
            "delete",
            "--target",
            "sim",
            "--snapshot",
            "snapshots/two.yaml",
            "--force",
            "--confirm-snapshot",
            "delete:snapshots/two.yaml",
            "--operation-id",
            "capture-delete-force-0001",
            "--confirm",
            "--non-interactive",
        ],
        environment,
    )
    assert status == 0 and forced["payload"]["result"]["forced"] is True


def test_capture_rejects_secret_bearing_parameter_names(tmp_path, monkeypatch):
    import iii.config_capture as module

    class SecretClient(FakeRuntimeClient):
        def configuration_capture_source(self, *, snapshot_id, expected_profile):
            source = super().configuration_capture_source(
                snapshot_id=snapshot_id, expected_profile=expected_profile
            )
            source["parameter_document"]["/sensor/example"]["ros__parameters"][
                "api_token"
            ] = "must-not-export"
            return source

    monkeypatch.setattr(module, "RuntimeApiClient", SecretClient)
    workspace = _workspace(tmp_path)
    status, result = _invoke(
        _pull_args("capture-secret-rejection"), _environment(workspace)
    )

    assert status == 20
    assert result["code"] == "III_CONFIG_CAPTURE_PULL_REJECTED"
    assert not list((workspace / ".iii/captures").glob("[0-9a-f]" * 64))
