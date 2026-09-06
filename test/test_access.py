from __future__ import annotations

import argparse
import json
from pathlib import Path

from iii import access
from iii.result import Outcome


def test_access_prepare_generates_fresh_independent_private_material_without_leak(
    tmp_path: Path,
) -> None:
    passphrase = tmp_path / "passphrase"
    passphrase.write_text("correct horse battery staple\n")
    passphrase.chmod(0o600)
    directory = tmp_path / "machine"
    args = argparse.Namespace(
        directory=directory,
        label="gc-primary",
        signer_passphrase_file=passphrase,
        keyring_account=None,
    )
    retained = access.prepare_preflight(args)
    args._iii_retained_plan = {"preflight": retained}

    result = access.prepare(args)

    assert result.outcome == Outcome.SUCCESS
    assert result.payload["private_material_exported"] is False
    assert (directory / "ssh_ed25519").stat().st_mode & 0o077 == 0
    assert (directory / "enrollment.json").stat().st_mode & 0o077 == 0
    assert (
        b"ENCRYPTED PRIVATE KEY" in (directory / "field-signing-key.pem").read_bytes()
    )
    token = (directory / "runtime-api-token").read_text().strip()
    enrollment = json.loads((directory / "enrollment.json").read_text())
    rendered = result.render_json()
    assert token not in rendered
    assert "PRIVATE KEY" not in rendered
    assert enrollment["runtime_api"]["token_sha256"] not in token


def test_access_parser_inventory_declares_mutation_and_terminal_semantics() -> None:
    from iii.__main__ import build_parser
    from iii.runner import inventory_parser

    inventory = inventory_parser(build_parser())
    assert inventory[("access", "list")].mutating is False
    assert inventory[("access", "enroll", "prepare")].mutating is True
    assert inventory[("access", "enroll", "add")].mutating is True
    assert inventory[("access", "enroll", "prove")].mutating is True
    assert inventory[("access", "revoke")].mutating is True
    assert inventory[("access", "signer", "revoke")].mutating is True


def test_access_prepare_rejects_symlink_parent_into_repository(tmp_path: Path) -> None:
    passphrase = tmp_path / "passphrase"
    passphrase.write_text("correct horse battery staple\n")
    passphrase.chmod(0o600)
    linked = tmp_path / "linked-workspace"
    linked.symlink_to(access._workspace(), target_is_directory=True)
    args = argparse.Namespace(
        directory=linked / ".private-credentials",
        label="unsafe-parent",
        signer_passphrase_file=passphrase,
        keyring_account=None,
    )

    try:
        access.prepare_preflight(args)
    except ValueError as exc:
        assert "outside the repository" in str(exc)
    else:
        raise AssertionError("symlinked repository credential destination was accepted")


def test_access_waits_for_authenticated_remote_completion() -> None:
    class Manager:
        client_id = "a" * 64

        def __init__(self):
            self.calls = 0

        def receiver_request(self, request):
            self.calls += 1
            state = "running" if self.calls == 1 else "completed"
            return {"operation": {"state": state, "result": {"kind": "access"}}}

    manager = Manager()
    result = access._await_operation(manager, "access-operation-0001")
    assert result["state"] == "completed"
    assert manager.calls == 2


def test_pending_machine_plans_proof_without_active_status_probe(
    monkeypatch, tmp_path: Path
) -> None:
    enrollment = {
        "schema": "iii.machine-enrollment/v1",
        "machine_id": "b" * 64,
    }

    class Manager:
        client_id = "a" * 64

        def __init__(self):
            self.actions = []

        def receiver_request(self, request):
            self.actions.append(request["action"])
            assert request["action"] == "plan-access"
            assert request["payload"]["target"] == {
                "logical_id": "drone",
                "profile": "real",
            }
            return {
                "plan": {
                    "operation_id": request["operation_id"],
                    "target": request["payload"]["target"],
                },
                "nonce": {"nonce_id": "c" * 64},
            }

    manager = Manager()
    monkeypatch.setattr(access, "_manager", lambda _args: manager)
    monkeypatch.setattr(access, "_load_enrollment", lambda _path: enrollment)
    args = argparse.Namespace(
        enrollment=tmp_path / "enrollment.json",
        phase="prove",
        target="real",
        _iii_operation_id="access-prove-0001",
    )

    planned = access.enroll_preflight(args)

    assert planned["plan"]["target"]["logical_id"] == "drone"
    assert manager.actions == ["plan-access"]


def test_access_preflights_reuse_retained_receiver_nonce(monkeypatch, tmp_path: Path) -> None:
    retained = {
        "plan": {"operation_id": "access-operation-0001"},
        "nonce": {"nonce_id": "c" * 64},
    }
    monkeypatch.setattr(access, "_retained_preflight", lambda _args: retained)
    monkeypatch.setattr(
        access,
        "_manager",
        lambda _args: (_ for _ in ()).throw(AssertionError("receiver was replanned")),
    )
    args = argparse.Namespace(
        directory=tmp_path / "already-created",
        enrollment=tmp_path / "enrollment.json",
        label="gc-primary",
        phase="add",
        target="hil",
        signer_passphrase_file=tmp_path / "missing-passphrase",
        keyring_account=None,
        authority="machine",
        machine_id="d" * 64,
        field_signer_id=None,
    )

    assert access.prepare_preflight(args) == retained
    assert access.enroll_preflight(args) == retained
    assert access.revoke_preflight(args) == retained
