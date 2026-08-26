from concurrent.futures import ProcessPoolExecutor
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import tarfile

import jsonschema
import pytest

from iii.__main__ import main
from iii import registry


SCHEMAS = Path(__file__).resolve().parents[3] / "deployment/schemas/v1"


def _validate(name: str, value: dict) -> None:
    schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    jsonschema.Draft7Validator(schema).validate(value)


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(registry.canonical_json(value) + b"\n")


def _concurrent_reindex(root: str) -> str:
    plan = registry.build_reindex_plan(Path(root))
    return registry.apply_reindex_plan(Path(root), plan)["registry_index_id"]


def _concurrent_registry_write(arguments: tuple[str, int]) -> str:
    root_value, index = arguments
    root = Path(root_value)
    value = {"schema": "iii.concurrent-record/v1", "index": index}
    with registry.registry_lock(root):
        return str(
            registry.atomic_json(root, f"readiness/concurrent-{index}.json", value)
        )


def _mixed_registry(root: Path) -> None:
    _json(
        root / "operations/op-1/state.json",
        {
            "schema": "iii.operation-state/v1",
            "state": "completed",
            "creation_source": "iii test",
            "target": {"logical_id": "aircraft-1", "profile": "real"},
        },
    )
    _json(
        root / "readiness/ready-1.json",
        {
            "schema": "iii.field-readiness/v1",
            "creation_source": "iii field check",
            "logical_id": "aircraft-1",
            "profile": "real",
        },
    )
    (root / "captures/capture-1").mkdir(parents=True)
    (root / "captures/capture-1/a.bin").write_bytes(b"duplicate-content")
    (root / "captures/capture-1/b.bin").write_bytes(b"duplicate-content")


def test_empty_and_mixed_inventory_tracks_identity_target_references_and_duplicates(
    tmp_path,
):
    empty = registry.build_inventory(tmp_path / "missing")
    assert empty["records"] == []
    assert empty["blobs"] == []

    root = tmp_path / "registry"
    _mixed_registry(root)
    inventory = registry.build_inventory(root)
    _validate("local-record-index", inventory)
    assert [item["domain"] for item in inventory["records"]] == [
        "captures",
        "operations",
        "readiness",
    ]
    operation = inventory["records"][1]
    assert operation["schema"] == registry.RECORD_SCHEMA
    assert operation["creation_source"]["value"] == "iii test"
    assert operation["target"] == {"logical_id": "aircraft-1", "profile": "real"}
    capture = inventory["records"][0]
    assert capture["files"][0]["content_id"] == capture["files"][1]["content_id"]


def test_concurrent_reindex_is_locked_atomic_and_cleans_crash_staging(tmp_path):
    root = tmp_path / "registry"
    _mixed_registry(root)
    partials = [
        root / "indexes/.records.json.partial-crash",
        root / "blobs/.blob.partial-crash",
        root / "captures/capture-1/.a.bin.partial-import-crash",
        root / ("operations/op-1/.state.json.123." + "a" * 32 + ".tmp"),
    ]
    for path in partials:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"partial")
    with registry.registry_lock(root):
        pass
    assert all(not path.exists() for path in partials)
    with ProcessPoolExecutor(max_workers=4) as executor:
        written = list(
            executor.map(
                _concurrent_registry_write,
                [(str(root), index) for index in range(12)],
            )
        )
    assert len(set(written)) == 12
    with ProcessPoolExecutor(max_workers=4) as executor:
        identities = list(executor.map(_concurrent_reindex, [str(root)] * 12))
    assert len(set(identities)) == 1
    assert registry.read_index(root)["index_id"] == identities[0]


def test_full_incremental_deterministic_cross_computer_import_and_reindex(tmp_path):
    source = tmp_path / "source"
    _mixed_registry(source)
    (source / "backups/empty-backup/nested/empty").mkdir(parents=True)
    full_a = tmp_path / "archives/full-a.tar"
    full_b = tmp_path / "archives/full-b.tar"
    first = registry.build_archive_plan(source, destination=full_a)
    assert first["mode"] == "full"
    assert first["capacity_sufficient"] is True
    assert first["included_domains"]
    assert first["referenced_blob_count"] == 3
    first_receipt = registry.apply_archive_plan(source, first)
    _validate("record-archive-manifest", first["archive_manifest"])
    _validate("record-archive-receipt", first_receipt)
    assert full_a.stat().st_size == first["projected_archive_bytes"]
    idempotent = registry.build_archive_plan(source, destination=full_a)
    assert idempotent["destination_state"] == "verified-identical"
    assert idempotent["additional_required_bytes"] == 0
    registry.apply_archive_plan(source, idempotent)
    second = registry.build_archive_plan(source, destination=full_b)
    registry.apply_archive_plan(source, second)
    assert full_a.read_bytes() == full_b.read_bytes()

    _json(
        source / "status-indexes/status-2.json",
        {"schema": "iii.release-status-index/v1", "creation_source": "iii release"},
    )
    incremental = tmp_path / "archives/incremental.tar"
    incremental_plan = registry.build_archive_plan(
        source, destination=incremental, base_archive=full_a
    )
    assert incremental_plan["mode"] == "incremental"
    assert incremental_plan["included_blob_count"] == 1
    registry.apply_archive_plan(source, incremental_plan)

    stale_base = tmp_path / "archives/stale-base.tar"
    stale_base.write_bytes(full_a.read_bytes())
    stale_plan = registry.build_archive_plan(
        source,
        destination=tmp_path / "archives/stale-delta.tar",
        base_archive=stale_base,
    )
    stale_base.write_bytes(stale_base.read_bytes()[:-512])
    with pytest.raises(registry.RegistryError, match="archive|base"):
        registry.apply_archive_plan(source, stale_plan)

    replacement = tmp_path / "replacement"
    base_import = registry.build_import_plan(replacement, archive_path=full_a)
    imported = registry.apply_import_plan(replacement, base_import)
    _validate("record-import-receipt", imported)
    assert (replacement / "backups/empty-backup").is_dir()
    assert (replacement / "backups/empty-backup/nested/empty").is_dir()
    delta_import = registry.build_import_plan(replacement, archive_path=incremental)
    registry.apply_import_plan(replacement, delta_import)
    assert registry.build_inventory(replacement)["records"]
    index = replacement / "indexes/records.json"
    index.unlink()
    repeat = registry.build_import_plan(replacement, archive_path=incremental)
    registry.apply_import_plan(replacement, repeat)
    assert registry.read_index(replacement) is not None


def test_incremental_import_refuses_missing_base_content(tmp_path):
    source = tmp_path / "source"
    _mixed_registry(source)
    full = tmp_path / "full.tar"
    registry.apply_archive_plan(
        source, registry.build_archive_plan(source, destination=full)
    )
    incremental = tmp_path / "incremental.tar"
    registry.apply_archive_plan(
        source,
        registry.build_archive_plan(source, destination=incremental, base_archive=full),
    )
    plan = registry.build_import_plan(tmp_path / "empty", archive_path=incremental)
    assert plan["missing_blob_ids"]
    with pytest.raises(registry.RegistryError, match="base content"):
        registry.apply_import_plan(tmp_path / "empty", plan)


def test_capacity_path_traversal_links_special_files_and_corruption_fail_closed(
    monkeypatch, tmp_path
):
    source = tmp_path / "source"
    _mixed_registry(source)
    usage = os.statvfs(tmp_path)
    monkeypatch.setattr(
        registry.shutil,
        "disk_usage",
        lambda _path: registry.shutil._ntuple_diskusage(
            usage.f_blocks, usage.f_blocks, 0
        ),
    )
    plan = registry.build_archive_plan(source, destination=tmp_path / "no-space.tar")
    assert plan["capacity_sufficient"] is False
    with pytest.raises(registry.RegistryError, match="capacity"):
        registry.apply_archive_plan(source, plan)
    with pytest.raises(registry.RegistryError, match="outside the registry"):
        registry.build_archive_plan(
            source, destination=source / "archives/not-external.tar"
        )

    linked = source / "backups/linked"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(source / "operations", target_is_directory=True)
    with pytest.raises(registry.RegistryError, match="symbolic link|unsafe"):
        registry.build_inventory(source)
    linked.unlink()
    special = source / "backups/fifo"
    os.mkfifo(special)
    with pytest.raises(registry.RegistryError, match="unsafe entry"):
        registry.build_inventory(source)
    special.unlink()

    malicious = tmp_path / "traversal.tar"
    with tarfile.open(malicious, "w", format=tarfile.USTAR_FORMAT) as archive:
        info = registry._tar_info("../outside", 1)
        archive.addfile(info, BytesIO(b"x"))
    with pytest.raises(registry.RegistryError, match="manifest|safe"):
        registry.inspect_archive(malicious)

    valid = tmp_path / "valid.tar"
    monkeypatch.undo()
    registry.apply_archive_plan(
        source, registry.build_archive_plan(source, destination=valid)
    )
    corrupt = tmp_path / "corrupt.tar"
    damaged = bytearray(valid.read_bytes())
    with tarfile.open(valid, "r:") as archive:
        blob = archive.getmembers()[1]
    damaged[blob.offset_data] ^= 0xFF
    corrupt.write_bytes(damaged)
    with pytest.raises(registry.RegistryError):
        registry.build_import_plan(tmp_path / "target", archive_path=corrupt)
    partial = tmp_path / "partial.tar"
    partial.write_bytes(valid.read_bytes()[:600])
    with pytest.raises(registry.RegistryError):
        registry.build_import_plan(tmp_path / "target", archive_path=partial)

    linked_target = tmp_path / "linked-target"
    linked_target.mkdir()
    (linked_target / "operations").symlink_to(source / "operations")
    unsafe_import = registry.build_import_plan(linked_target, archive_path=valid)
    assert any(
        item["reason"] == "unsafe-existing-parent"
        for item in unsafe_import["conflicts"]
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"password": {"nested": "not-allowed"}},
        {"wifi_psk": "not-allowed"},
        {"machine_id": "host-identity"},
        {"argv": ["iii", "deploy", "--token", "not-allowed"]},
        {"argv": ["iii", "deploy", "--api-key=not-allowed"]},
        {"source": "/etc/NetworkManager/system-connections/aircraft.nmconnection"},
    ],
)
def test_secret_bearing_json_is_rejected(payload, tmp_path):
    root = tmp_path / "registry"
    _json(root / "operations/unsafe/state.json", payload)
    with pytest.raises(registry.RegistrySecretError, match="secret-bearing"):
        registry.build_archive_plan(root, destination=tmp_path / "unsafe.tar")


def test_private_key_content_and_secret_paths_are_rejected(tmp_path):
    root = tmp_path / "registry"
    key = root / "captures/capture-1/material.bin"
    key.parent.mkdir(parents=True)
    key.write_bytes(b"prefix\n-----BEGIN PRIVATE KEY-----\nsecret")
    with pytest.raises(registry.RegistrySecretError, match="private-key"):
        registry.build_archive_plan(root, destination=tmp_path / "key.tar")
    key.unlink()
    _json(root / "captures/credentials/state.json", {"safe": True})
    with pytest.raises(registry.RegistrySecretError, match="secret-path"):
        registry.build_archive_plan(root, destination=tmp_path / "path.tar")

    (root / "captures/credentials/state.json").unlink()
    (root / "captures/credentials").rmdir()
    environment = root / "captures/capture-2/runtime.env"
    environment.parent.mkdir()
    environment.write_text("RUNTIME_API_TOKEN=not-allowed\n")
    with pytest.raises(registry.RegistrySecretError, match="secret-assignment"):
        registry.build_archive_plan(root, destination=tmp_path / "env.tar")


def test_prune_shows_and_preserves_protected_records(tmp_path):
    root = tmp_path / "registry"
    _json(root / "backups/restore.json", {"schema": "iii.backup/v1"})
    _json(root / "captures/evidence.json", {"schema": "iii.capture/v1"})
    _json(root / "cache/releases/v1/cache.json", {"schema": "iii.cache/v1"})
    inventory = registry.build_inventory(root)
    ids = {record["domain"]: record["record_id"] for record in inventory["records"]}
    before_archive = registry.build_prune_plan(root, record_ids=list(ids.values()))
    assert before_archive["candidates"] == [
        next(
            record
            for record in before_archive["protected"] + before_archive["candidates"]
            if record["domain"] == "release-cache"
        )
    ]
    protected_reasons = {
        item["domain"]: item["reasons"] for item in before_archive["protected"]
    }
    assert "restore-evidence" in protected_reasons["backups"]
    assert "irreplaceable-not-externally-archived" in protected_reasons["captures"]

    archive = tmp_path / "records.tar"
    registry.apply_archive_plan(
        root, registry.build_archive_plan(root, destination=archive)
    )
    current = registry.build_inventory(root)
    capture_id = next(
        item["record_id"] for item in current["records"] if item["domain"] == "captures"
    )
    plan = registry.build_prune_plan(root, record_ids=[capture_id])
    assert [item["record_id"] for item in plan["candidates"]] == [capture_id]
    result = registry.apply_prune_plan(root, plan)
    assert result["removed_record_ids"] == [capture_id]
    assert not (root / "captures/evidence.json").exists()


def test_prune_reauthenticates_archive_receipts_and_external_media(tmp_path):
    root = tmp_path / "registry"
    _json(root / "captures/evidence.json", {"schema": "iii.capture/v1"})
    archive = tmp_path / "external/records.tar"
    receipt = registry.apply_archive_plan(
        root, registry.build_archive_plan(root, destination=archive)
    )
    record_id = next(
        item["record_id"]
        for item in registry.build_inventory(root)["records"]
        if item["domain"] == "captures"
    )
    plan = registry.build_prune_plan(root, record_ids=[record_id])
    assert plan["candidates"]
    archive.unlink()
    coverage = registry.archive_coverage(root)
    assert coverage["recent"] is True
    assert coverage["archive_available"] is False
    with pytest.raises(registry.RegistryConflict, match="coverage changed"):
        registry.apply_prune_plan(root, plan)
    (root / "archive-receipts" / f"{receipt['receipt_id']}.json").unlink()

    archive.parent.mkdir(exist_ok=True)
    receipt = registry.apply_archive_plan(
        root, registry.build_archive_plan(root, destination=archive)
    )
    receipt_path = root / "archive-receipts" / f"{receipt['receipt_id']}.json"
    spoofed = {
        **receipt,
        "covered_record_ids": [],
        "covered_irreplaceable_record_ids": [],
    }
    unsigned = {key: value for key, value in spoofed.items() if key != "receipt_id"}
    spoofed["receipt_id"] = registry.content_id(unsigned)
    receipt_path.write_bytes(registry.canonical_json(spoofed) + b"\n")
    refused = registry.build_prune_plan(root, record_ids=[record_id])
    assert refused["candidates"] == []
    assert "irreplaceable-not-externally-archived" in refused["protected"][0]["reasons"]


def test_cli_archive_plan_is_structured_and_requires_retained_confirmation(tmp_path):
    root = tmp_path / "registry"
    _mixed_registry(root)
    output = StringIO()
    status = main(
        [
            "records",
            "archive",
            str(tmp_path / "cli.tar"),
            "--registry-root",
            str(root),
            "--dry-run",
            "--operation-id",
            "records-cli-plan",
            "--json",
        ],
        stdout=output,
        stderr=StringIO(),
        environment={"III_REGISTRY_ROOT": str(root)},
    )
    value = json.loads(output.getvalue())
    assert status == 0
    assert value["code"] == "III_OPERATION_PLAN_READY"
    preflight = value["payload"]["plan"]["preflight"]
    assert preflight["schema"] == "iii.record-archive-plan/v1"
    assert preflight["capacity_sufficient"] is True
    assert not (tmp_path / "cli.tar").exists()
    applied = StringIO()
    apply_status = main(
        [
            "records",
            "archive",
            str(tmp_path / "cli.tar"),
            "--registry-root",
            str(root),
            "--operation-id",
            "records-cli-plan",
            "--confirm",
            "--non-interactive",
            "--json",
        ],
        stdout=applied,
        stderr=StringIO(),
        environment={"III_REGISTRY_ROOT": str(root)},
    )
    applied_value = json.loads(applied.getvalue())
    assert apply_status == 0
    assert applied_value["code"] == "III_RECORD_ARCHIVE_VERIFIED"
    assert (tmp_path / "cli.tar").is_file()


def test_cli_prune_replays_exact_plan_without_pruning_its_own_operation(tmp_path):
    root = tmp_path / "registry"
    _json(root / "cache/releases/v1/cache.json", {"schema": "iii.cache/v1"})
    record_id = registry.build_inventory(root)["records"][0]["record_id"]
    argv = [
        "records",
        "prune",
        "--record",
        record_id,
        "--registry-root",
        str(root),
    ]
    planned = StringIO()
    assert (
        main(
            [*argv, "--dry-run", "--operation-id", "records-prune-plan", "--json"],
            stdout=planned,
            stderr=StringIO(),
            environment={"III_REGISTRY_ROOT": str(root)},
        )
        == 0
    )
    assert (
        json.loads(planned.getvalue())["payload"]["plan"]["preflight"]["candidates"][0][
            "record_id"
        ]
        == record_id
    )
    applied = StringIO()
    assert (
        main(
            [
                *argv,
                "--operation-id",
                "records-prune-plan",
                "--confirm",
                "--non-interactive",
                "--json",
            ],
            stdout=applied,
            stderr=StringIO(),
            environment={"III_REGISTRY_ROOT": str(root)},
        )
        == 0
    )
    assert json.loads(applied.getvalue())["code"] == "III_RECORDS_PRUNED"
    assert not (root / "cache/releases/v1").exists()
    assert (root / "operations/records-prune-plan").is_dir()
