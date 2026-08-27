from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from iii import gc_application
from iii.__main__ import build_parser
from iii.runner import inventory_parser


COMPONENT_FILES = {
    "bundle.manifest.json",
    "release-manifest.json",
    "bundle.sig.json",
    "bundle.sha256",
    "bundle.tar.zst",
}


def _environment(tmp_path: Path):
    trust = tmp_path / "trusted-signers.json"
    trust.write_text("{}")
    return {
        "III_GC_APPLICATION_ROOT": str(tmp_path / "applications"),
        "III_GC_APPLICATION_STATE_ROOT": str(tmp_path / "state"),
        "III_GC_APPLICATION_CACHE_ROOT": str(tmp_path / "cache"),
        "III_GC_TRUSTED_SIGNERS": str(trust),
    }


def _stage_args(tmp_path: Path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in COMPONENT_FILES:
        (bundle / name).write_bytes((name + "\n").encode())
    return SimpleNamespace(
        application_action="stage",
        bundle=bundle,
        protect_offline=False,
        _iii_environment=_environment(tmp_path),
        _iii_retained_plan=None,
    )


def test_parser_inventory_covers_every_application_leaf():
    inventory = inventory_parser(build_parser())
    assert inventory[("gc", "application", "status")].mutating is False
    for action in ("stage", "activate", "rollback", "reconcile", "prune"):
        spec = inventory[("gc", "application", action)]
        assert spec.mutating is True
        assert spec.plan_provider is gc_application.preflight


def test_stage_plan_binds_every_component_byte_and_detects_change(tmp_path):
    args = _stage_args(tmp_path)
    first = gc_application.preflight(args)
    assert set(first["bundle"]["files"]) == COMPONENT_FILES
    assert first["state"] is None
    assert first["journal"] is None
    assert first["trusted_signers"]["sha256"]
    assert first["protect_offline"] is False

    (args.bundle / "bundle.sha256").write_text("changed\n")
    second = gc_application.preflight(args)
    assert first["plan_id"] != second["plan_id"]
    assert first["bundle"]["content_id"] != second["bundle"]["content_id"]


def test_mutation_rejects_stale_bundle_before_store_call(tmp_path, monkeypatch):
    args = _stage_args(tmp_path)
    planned = gc_application.preflight(args)
    args._iii_retained_plan = {"preflight": planned}
    (args.bundle / "bundle.tar.zst").write_bytes(b"changed")
    called = []
    monkeypatch.setattr(
        gc_application, "_store", lambda *_args, **_kwargs: called.append(True)
    )

    result = gc_application.mutate(args)
    assert result.outcome.value == "rejected"
    assert "changed after planning" in result.findings[0].message
    assert called == []


def test_status_is_read_only_and_refuses_missing_baseline_roots(tmp_path):
    args = SimpleNamespace(_iii_environment=_environment(tmp_path))
    application = Path(args._iii_environment["III_GC_APPLICATION_ROOT"])
    state = Path(args._iii_environment["III_GC_APPLICATION_STATE_ROOT"])
    cache = Path(args._iii_environment["III_GC_APPLICATION_CACHE_ROOT"])

    result = gc_application.status(args)

    assert result.outcome.value == "rejected"
    assert not application.exists()
    assert not state.exists()
    assert not cache.exists()


def test_explicit_disconnected_and_sim_safety_are_distinct():
    disconnected, disconnected_evidence = gc_application._safety(
        SimpleNamespace(disconnected=True, sim=False, safety_file=None)
    )
    sim, sim_evidence = gc_application._safety(
        SimpleNamespace(disconnected=False, sim=True, safety_file=None)
    )
    assert disconnected == {
        "connected": False,
        "source": "operator-explicit-disconnected",
    }
    assert sim["profile"] == "sim"
    assert disconnected_evidence["sha256"] != sim_evidence["sha256"]
