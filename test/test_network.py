from __future__ import annotations

import argparse
from pathlib import Path

from iii import network
from iii.__main__ import build_parser
from iii.result import Outcome
from iii.runner import inventory_parser


def _profile() -> dict:
    return {
        "schema": "iii.operator-network-input/v1",
        "ethernet_dhcp4": True,
        "wifi": [{"ssid": "private-field", "password": "private-password"}],
    }


def test_network_parser_inventory_declares_leaf_semantics() -> None:
    inventory = inventory_parser(build_parser())
    assert inventory[("host", "network", "apply")].mutating is True
    assert inventory[("host", "network", "apply")].plan_provider is not None
    assert inventory[("host", "network", "confirm")].mutating is True
    assert inventory[("host", "network", "confirm")].plan_provider is not None
    assert inventory[("host", "network", "status")].mutating is False


def test_apply_sends_secret_only_in_receiver_request_and_returns_redacted_result(
    monkeypatch, tmp_path: Path
) -> None:
    profile = _profile()
    plan = {
        "plan_id": "1" * 64,
        "operation_id": "network-operation-0001",
        "target": {"logical_id": "drone", "profile": "real"},
        "parameters": {
            "network_id": "2" * 64,
            "no_change": False,
            "connectivity_impacting": True,
            "confirmation_deadline_s": 90,
            "profile": {
                "ethernet_dhcp4": True,
                "wifi_profile_ids": ["3" * 64],
                "wifi_profile_count": 1,
                "onboard_access_point": False,
            },
        },
    }
    args = argparse.Namespace(
        input=tmp_path / "network.json",
        target="real",
        _iii_retained_plan={"preflight": {"plan": plan, "nonce": "4" * 64}},
    )
    calls = []

    class Manager:
        client_id = "5" * 64

        def receiver_request(self, request):
            calls.append(request)
            return {"operation": {"state": "accepted"}}

    monkeypatch.setattr(network, "_input", lambda _args: profile)
    monkeypatch.setattr(network, "_manager", lambda _args: Manager())
    result = network.apply(args)

    assert result.outcome == Outcome.SUCCESS
    assert result.code == "III_NETWORK_CONFIRMATION_REQUIRED"
    assert "private-field" not in str(result.payload)
    assert "private-password" not in str(result.payload)
    assert calls[0]["payload"]["profile"] == profile
    assert calls[0]["nonce"] == "4" * 64
    assert result.next_actions[0].command[-1] == "--dry-run"


def test_confirmation_consumes_only_bound_retained_confirmation(monkeypatch) -> None:
    confirmation = {
        "confirmation_id": "6" * 64,
        "operation_id": "network-confirm-0001",
        "target_operation_id": "network-operation-0001",
        "network_id": "2" * 64,
    }
    args = argparse.Namespace(
        target="real",
        network_operation_id="network-operation-0001",
        _iii_retained_plan={
            "preflight": {"confirmation": confirmation, "nonce": "7" * 64}
        },
    )
    calls = []

    class Manager:
        client_id = "5" * 64

        def receiver_request(self, request):
            calls.append(request)
            return {
                "network": {
                    "kind": "network-confirm",
                    "network_id": "2" * 64,
                    "state": "confirmed",
                }
            }

    monkeypatch.setattr(network, "_manager", lambda _args: Manager())
    result = network.confirm(args)

    assert result.outcome == Outcome.SUCCESS
    assert result.code == "III_NETWORK_CONFIRMED"
    assert calls[0]["payload"] == {"confirmation": confirmation}
    assert calls[0]["nonce"] == "7" * 64
