from __future__ import annotations

import argparse
from pathlib import Path

from iii import host_maintenance
from iii.__main__ import build_parser
from iii.result import Outcome
from iii.runner import inventory_parser


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        kind="release-status-trust",
        target="real",
        offline=False,
        backup_record=tmp_path / "backup.json",
        boot_profile=None,
        trust_store=tmp_path / "trust.json",
        release_status_index=tmp_path / "index.json",
        retire_signer=["a" * 64],
        replacement_proof=[tmp_path / "proof.json"],
        policy=tmp_path / "policy.json",
    )


def test_host_maintenance_parser_inventory_declares_each_leaf_semantics() -> None:
    inventory = inventory_parser(build_parser())
    assert inventory[("host", "maintenance", "check")].mutating is False
    assert inventory[("host", "maintenance", "apply")].mutating is True
    assert inventory[("host", "maintenance", "reboot")].mutating is True
    assert inventory[("host", "maintenance", "status")].mutating is False
    assert inventory[("host", "maintenance", "apply")].plan_provider is not None
    assert inventory[("host", "maintenance", "reboot")].plan_provider is not None


def test_check_preserves_every_rotation_input_in_universal_next_action(
    monkeypatch, tmp_path: Path
) -> None:
    args = _args(tmp_path)
    monkeypatch.setattr(
        host_maintenance,
        "_plan",
        lambda _args, operation_id: {
            "plan": {
                "operation_id": operation_id,
                "target": {"logical_id": "drone", "profile": "real"},
                "parameters": {
                    "no_change": False,
                    "mutations": ["replace trust"],
                },
            },
            "nonce": "b" * 64,
        },
    )

    result = host_maintenance.check(args)

    assert result.outcome == Outcome.SUCCESS
    command = result.next_actions[0].command
    assert command[-1] == "--dry-run"
    for option in (
        "--backup-record",
        "--trust-store",
        "--release-status-index",
        "--retire-signer",
        "--replacement-proof",
        "--policy",
    ):
        assert option in command


def test_boot_maintenance_parser_and_next_action_preserve_exact_profile(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "boot-profile.json"
    parsed = build_parser().parse_args(
        [
            "host",
            "maintenance",
            "check",
            "--kind",
            "boot-settings",
            "--boot-profile",
            str(profile),
        ]
    )
    assert parsed.kind == "boot-settings"
    assert parsed.boot_profile == profile

    args = _args(tmp_path)
    args.kind = "boot-settings"
    args.boot_profile = profile
    args.trust_store = None
    args.release_status_index = None
    args.retire_signer = []
    args.replacement_proof = []
    command = host_maintenance._apply_command(args)
    index = command.index("--boot-profile")
    assert command[index + 1] == str(profile)


def test_apply_uses_only_exact_retained_receiver_plan(
    monkeypatch, tmp_path: Path
) -> None:
    args = _args(tmp_path)
    receiver_plan = {
        "operation_id": "host-maintenance-apply",
        "target": {"logical_id": "drone", "profile": "real"},
    }
    args._iii_retained_plan = {"preflight": {"plan": receiver_plan, "nonce": "c" * 64}}
    calls = []

    class Manager:
        client_id = "d" * 64

        def receiver_request(self, request):
            calls.append(request)
            return {"operation": {"state": "accepted"}}

    monkeypatch.setattr(host_maintenance, "_manager", lambda _args: Manager())
    monkeypatch.setattr(
        host_maintenance,
        "_await_operation",
        lambda _manager, operation_id: {
            "state": "completed",
            "result": {
                "maintenance_id": "e" * 64,
                "transaction_id": "f" * 64,
                "reboot_required": True,
                "commissioning": {"state": "unchanged", "reasons": []},
            },
        },
    )

    result = host_maintenance.apply(args)

    assert result.outcome == Outcome.SUCCESS
    assert result.code == "III_HOST_MAINTENANCE_REBOOT_REQUIRED"
    assert calls == [
        {
            "protocol_version": "1",
            "action": "host-maintenance",
            "operation_id": "host-maintenance-apply",
            "client_id": "d" * 64,
            "payload": {"plan": receiver_plan},
            "nonce": "c" * 64,
        }
    ]


def test_reboot_does_not_hide_authenticated_terminal_failure(
    monkeypatch, tmp_path: Path
) -> None:
    args = argparse.Namespace(
        maintenance_id="e" * 64,
        target="real",
        _iii_retained_plan={
            "preflight": {
                "plan": {
                    "operation_id": "host-reboot-apply",
                    "target": {"logical_id": "drone", "profile": "real"},
                },
                "nonce": "c" * 64,
            }
        },
    )

    class Manager:
        client_id = "d" * 64

        def receiver_request(self, _request):
            return {"operation": {"state": "accepted"}}

    monkeypatch.setattr(host_maintenance, "_manager", lambda _args: Manager())
    monkeypatch.setattr(
        host_maintenance,
        "_await_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            host_maintenance.HostOperationFailed("postboot validation failed")
        ),
    )

    result = host_maintenance.reboot(args)

    assert result.outcome == Outcome.REJECTED
    assert "postboot validation failed" in result.findings[0].message


def test_status_surfaces_retained_recovery_recommendation(monkeypatch) -> None:
    args = argparse.Namespace(target="real")

    class Manager:
        client_id = "d" * 64

        def receiver_request(self, _request):
            return {
                "maintenance": {
                    "schema": "iii.host-maintenance-status/v1",
                    "transaction": {"phase": "failed"},
                    "mutation_blocked": False,
                    "recovery_recommendation": "restore the anchor or reprovision",
                }
            }

    monkeypatch.setattr(host_maintenance, "_manager", lambda _args: Manager())

    result = host_maintenance.status(args)

    assert result.outcome == Outcome.WARNING
    assert result.code == "III_HOST_MAINTENANCE_RECOVERY_REQUIRED"
    assert "reprovision" in result.findings[0].message
