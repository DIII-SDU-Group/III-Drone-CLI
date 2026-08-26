from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass
from io import StringIO
import json
from pathlib import Path

from iii.__main__ import main
from iii.result import Outcome
import iii.release as release


@dataclass
class Cached:
    root: Path
    publication: dict
    notes: dict
    record: dict
    status: dict
    status_index: dict


def _cached(tmp_path: Path, status: str = "qualified") -> Cached:
    return Cached(
        tmp_path / "v1.2.3" / "release-id",
        {"version": "v1.2.3", "release_id": "release-id", "publication_id": "publication-id"},
        {"markdown": "# v1.2.3 deployment notes\n"},
        {"record_id": "release-record-id", "source_commit": "a" * 40},
        {"status": status, "reason": "verified", "statement_id": f"statement-{status}"},
        {"index_id": "c" * 64, "schema": "iii.release-status-index/v1"},
    )


def _runtime(tmp_path: Path, cached: Cached | None = None) -> dict:
    cached = cached or _cached(tmp_path)
    source = type("Source", (), {"latest_status_index": lambda self: b"index"})()
    return {
        "cache": tmp_path,
        "bundle_trust": tmp_path / "bundle-trust.json",
        "status_trust": tmp_path / "status-trust.json",
        "registry": object(),
        "limits": {"entries": 1},
        "source": source,
        "list_remote_releases": lambda *a, **k: [{
            "version": "v1.2.3", "status": "qualified", "release_id": "release-id",
            "source_commit": "a" * 40,
        }],
        "inspect_remote_release": lambda *a, **k: (
            cached.publication, cached.notes, cached.record, cached.status, {"index_id": "index-id"}
        ),
        "fetch_release": lambda *a, **k: cached,
        "load_cached_release": lambda *a, **k: cached,
        "materialize_cached_release": lambda *a, **k: Path(k.get("destination", a[1])),
        "refresh_cached_status": lambda *a, **k: cached.status,
    }


def _args(**values):
    defaults = {
        "repository": release.DEFAULT_REPOSITORY,
        "schema_root": None,
        "policy": None,
        "trusted_signers": None,
        "status_trusted_signers": None,
        "cache_root": None,
    }
    defaults.update(values)
    return Namespace(**defaults)


def test_list_and_show_use_canonical_verified_payload(monkeypatch, tmp_path):
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(release, "_runtime", lambda _args: runtime)
    listed = release.list_releases(_args())
    shown = release.show_release(_args(version="v1.2.3", offline=False))
    assert listed.outcome is Outcome.SUCCESS
    assert listed.payload["releases"][0]["status"] == "qualified"
    assert shown.outcome is Outcome.SUCCESS
    assert shown.release_id == "release-id"
    assert shown.payload["notes"]["markdown"] == "# v1.2.3 deployment notes\n"
    assert shown.payload["record"]["record_id"] == "release-record-id"


def test_fetch_and_cache_report_verified_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("III_REGISTRY_ROOT", str(tmp_path / "registry"))
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(release, "_runtime", lambda _args: runtime)
    monkeypatch.setattr(release, "_cached_root", lambda *_args: tmp_path / "cached")
    fetched = release.fetch(_args(version="v1.2.3"))
    cached = release.cache(_args(version="v1.2.3"))
    assert fetched.code == "III_RELEASE_FETCHED"
    assert cached.code == "III_RELEASE_CACHE_VERIFIED"
    assert "publication-id" in fetched.evidence
    assert (tmp_path / "registry/status-indexes" / ("c" * 64 + ".json")).is_file()
    assert (tmp_path / "registry/release-evidence/release-record-id.json").is_file()


def test_default_release_cache_is_inside_the_portable_registry(tmp_path):
    args = _args(
        schema_root=Path(__file__).resolve().parents[3] / "deployment/schemas/v1",
        policy=Path(__file__).resolve().parents[3] / "deployment/operational-policy.json",
    )
    args._iii_environment = {"III_REGISTRY_ROOT": str(tmp_path / "registry")}
    assert release._paths(args)["cache"] == tmp_path / "registry/cache/releases"


def test_verify_checks_remote_or_explicit_offline_cache(monkeypatch, tmp_path):
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(release, "_runtime", lambda _args: runtime)
    monkeypatch.setattr(release, "_load_cached", lambda *_args: _cached(tmp_path))
    online = release.verify(_args(version="v1.2.3", offline=False))
    offline = release.verify(_args(version="v1.2.3", offline=True))
    assert online.code == offline.code == "III_RELEASE_VERIFIED"
    assert online.payload["record_id"] == "release-record-id"
    assert online.payload["offline"] is False
    assert offline.payload["offline"] is True


def test_online_deploy_refreshes_status_before_materialising(monkeypatch, tmp_path):
    monkeypatch.setenv("III_REGISTRY_ROOT", str(tmp_path / "registry"))
    calls = []
    cached = _cached(tmp_path)
    runtime = _runtime(tmp_path, cached)
    runtime["refresh_cached_status"] = lambda *a, **k: calls.append("refresh")
    runtime["materialize_cached_release"] = lambda *a, **k: calls.append("materialize") or Path(a[1])
    monkeypatch.setattr(release, "_runtime", lambda _args: runtime)
    monkeypatch.setattr(release, "_load_cached", lambda *_args: cached)
    result = release.deploy(_args(version="v1.2.3", destination=tmp_path / "handoff", offline=False))
    assert result.outcome is Outcome.SUCCESS
    assert calls == ["refresh", "materialize"]
    assert result.payload["offline"] is False


def test_offline_deploy_is_explicit_and_does_not_hide_cached_status(monkeypatch, tmp_path):
    monkeypatch.setenv("III_REGISTRY_ROOT", str(tmp_path / "registry"))
    calls = []
    cached = _cached(tmp_path)
    runtime = _runtime(tmp_path, cached)
    runtime["refresh_cached_status"] = lambda *a, **k: calls.append("refresh")
    runtime["materialize_cached_release"] = lambda *a, **k: Path(a[1])
    monkeypatch.setattr(release, "_runtime", lambda _args: runtime)
    monkeypatch.setattr(release, "_load_cached", lambda *_args: cached)
    result = release.deploy(_args(version="v1.2.3", destination=tmp_path / "handoff", offline=True))
    assert result.outcome is Outcome.SUCCESS
    assert calls == []
    assert result.payload["status"]["statement_id"] == "statement-qualified"


def test_status_set_validates_current_signed_state_and_dispatches_release_branch(monkeypatch, tmp_path):
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(release, "_runtime", lambda _args: runtime)
    calls = []

    class Process:
        returncode = 0
        stdout = "dispatched\n"
        stderr = ""

    monkeypatch.setattr(release.subprocess, "run", lambda command, **_kwargs: calls.append(command) or Process())
    result = release.set_status(
        _args(
            version="v1.2.3", status="unsafe", reason="fleet safety bulletin",
            superseding_version="v1.2.4", _iii_operation_id="iii-op-1",
        )
    )
    assert result.code == "III_RELEASE_STATUS_DISPATCHED"
    command = calls[0]
    assert command[command.index("--ref") + 1] == "release"
    assert "expected_statement_id=statement-qualified" in command
    assert "client_operation_id=iii-op-1" in command


def test_status_set_refuses_non_monotonic_request_without_dispatch(monkeypatch, tmp_path):
    runtime = _runtime(tmp_path, _cached(tmp_path, status="unsafe"))
    monkeypatch.setattr(release, "_runtime", lambda _args: runtime)
    monkeypatch.setattr(release.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not dispatch")))
    result = release.set_status(
        _args(version="v1.2.3", status="withdrawn", reason="invalid", superseding_version=None)
    )
    assert result.outcome is Outcome.REJECTED
    assert result.code == "III_RELEASE_CONTRACT_REJECTED"


def test_mutating_release_leaf_requires_retained_confirmation(monkeypatch, tmp_path):
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(release, "fetch", lambda _args: (_ for _ in ()).throw(AssertionError("must not execute")))
    stdout = StringIO()
    status = main(
        ["release", "fetch", "v1.2.3", "--non-interactive", "--json"],
        stdout=stdout,
        stderr=StringIO(),
    )
    value = json.loads(stdout.getvalue())
    assert status == Outcome.REJECTED.exit_code
    assert value["code"] == "III_REQUIRED_INPUT"
    assert value["operation"]["state"] == "planned"
