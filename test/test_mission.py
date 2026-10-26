import json
from argparse import Namespace
from io import StringIO

import iii.mission as mission
from iii.__main__ import main
from iii.result import Outcome


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def command(self, command_id, parameters):
        self.calls.append((command_id, parameters))
        return self.response


def _catalog():
    return {
        "schema": "iii.mission-catalog/v1",
        "catalog_hash": "sha256:" + "a" * 64,
        "scope": "local",
        "active_profile": "sim",
        "entries": [
            {"id": "inspection-production", "classification": "production", "available": True},
            {
                "id": "flight-test",
                "classification": "test",
                "available": False,
                "unavailable_reason": "incompatible with profile sim",
            },
        ],
    }


def test_list_and_show_use_runtime_commands_and_never_emit_absolute_paths(monkeypatch):
    client = FakeClient({"accepted": True, "result": {"catalog": _catalog()}})
    monkeypatch.setattr(mission, "_client", lambda: client)
    listed = mission.list_entries(Namespace(all=True))
    assert listed.outcome is Outcome.SUCCESS
    assert client.calls == [(mission.LIST_COMMAND, {"all": True})]
    assert "unavailable: incompatible" in listed.payload["display"]
    assert "/home/" not in str(listed.payload)

    client.response = {
        "accepted": True,
        "result": {"entry": _catalog()["entries"][0], "catalog_hash": "sha256:" + "a" * 64},
    }
    shown = mission.show(Namespace(catalog_id="inspection-production"))
    assert shown.code == "III_MISSION_CATALOG_SHOW"
    assert client.calls[-1] == (
        mission.SHOW_COMMAND,
        {"catalog_id": "inspection-production", "all": True},
    )


def test_status_reports_installed_identity(monkeypatch):
    specification = {
        "catalog_id": "inspection-production",
        "catalog_hash": "sha256:" + "a" * 64,
        "entry_hash": "sha256:" + "b" * 64,
        "default_catalog_id": "inspection-production",
        "active_profile": "real",
        "classification": "production",
        "temporary_override": False,
        "catalog_ready": True,
    }
    client = FakeClient({"accepted": True, "result": {"status": {"specification": specification}}})
    monkeypatch.setattr(mission, "_client", lambda: client)
    result = mission.status(Namespace())
    assert result.outcome is Outcome.SUCCESS
    assert result.payload["status"]["specification"]["catalog_id"] == "inspection-production"


def test_selection_is_explicit_and_preserves_experimental_warning(monkeypatch):
    response = {
        "accepted": True,
        "result": {
            "message": "selected",
            "active_catalog_id": "reach-charge-leave-experimental",
            "active_entry_hash": "sha256:" + "b" * 64,
            "temporary_override": True,
            "warning": "EXPERIMENTAL mission selected for this runtime session",
        },
    }
    client = FakeClient(response)
    monkeypatch.setattr(mission, "_client", lambda: client)
    selected = mission.select(Namespace(catalog_id="reach-charge-leave-experimental", default=False))
    assert selected.outcome is Outcome.WARNING
    assert selected.findings[0].code == "III_MISSION_EXPERIMENTAL_WARNING"
    assert client.calls == [
        (
            mission.SELECT_COMMAND,
            {"default": False, "catalog_id": "reach-charge-leave-experimental"},
        )
    ]
    assert mission.select(Namespace(catalog_id=None, default=False)).outcome is Outcome.REJECTED
    assert mission.select(Namespace(catalog_id="id", default=True)).outcome is Outcome.REJECTED


def test_default_selection_and_runtime_rejection(monkeypatch):
    client = FakeClient(
        {
            "accepted": True,
            "result": {
                "message": "restored default",
                "active_catalog_id": "inspection-production",
                "active_entry_hash": "sha256:" + "b" * 64,
                "temporary_override": False,
                "warning": None,
            },
        }
    )
    monkeypatch.setattr(mission, "_client", lambda: client)
    selected = mission.select(Namespace(catalog_id=None, default=True))
    assert selected.outcome is Outcome.SUCCESS
    assert client.calls[-1] == (mission.SELECT_COMMAND, {"default": True})

    client.response = {"accepted": False, "rejection": {"message": "vehicle is not confirmed disarmed"}}
    rejected = mission.select(Namespace(catalog_id="inspection-production", default=False))
    assert rejected.outcome is Outcome.REJECTED
    assert "disarmed" in rejected.findings[0].message


def test_absolute_runtime_path_is_fail_closed(monkeypatch):
    client = FakeClient({"accepted": True, "result": {"entry": {"id": "bad", "source": "/tmp/bad.yaml"}}})
    monkeypatch.setattr(mission, "_client", lambda: client)
    result = mission.show(Namespace(catalog_id="bad"))
    assert result.code == "III_MISSION_CATALOG_PATH_LEAK"
    assert result.outcome is Outcome.REJECTED


def test_ros_graph_names_are_not_misclassified_as_filesystem_paths(monkeypatch):
    client = FakeClient(
        {
            "accepted": True,
            "result": {
                "status": {
                    "specification": {"catalog_ready": True},
                    "latest": {
                        "intents": [
                            {
                                "service_name": "/mission/inspection_demo/trigger_recharge_now"
                            }
                        ]
                    },
                }
            },
        }
    )
    monkeypatch.setattr(mission, "_client", lambda: client)

    result = mission.status(Namespace())

    assert result.code == "III_MISSION_CATALOG_STATUS"
    assert result.outcome is Outcome.SUCCESS


def test_parser_routes_read_only_and_retains_selection_before_mutation(monkeypatch, tmp_path):
    client = FakeClient({"accepted": True, "result": {"catalog": _catalog()}})
    monkeypatch.setattr(mission, "_client", lambda: client)
    stdout = StringIO()
    assert main(["mission", "list", "--all", "--json"], stdout=stdout, stderr=StringIO()) == 0
    payload = json.loads(stdout.getvalue())
    assert payload["code"] == "III_MISSION_CATALOG_LIST"
    assert client.calls == [(mission.LIST_COMMAND, {"all": True})]

    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path / "operations"))
    client.calls.clear()
    stdout = StringIO()
    status = main(
        ["mission", "select", "inspection-production", "--non-interactive", "--json"],
        stdout=stdout,
        stderr=StringIO(),
    )
    planned = json.loads(stdout.getvalue())
    assert status == Outcome.REJECTED.exit_code
    assert planned["code"] == "III_REQUIRED_INPUT"
    assert planned["operation"]["state"] == "planned"
    assert client.calls == []

    operation_id = planned["operation"]["id"]
    client.response = {
        "accepted": True,
        "result": {
            "message": "selected",
            "active_catalog_id": "inspection-production",
            "active_entry_hash": "sha256:" + "b" * 64,
            "temporary_override": False,
            "warning": None,
        },
    }
    stdout = StringIO()
    status = main(
        [
            "mission", "select", "inspection-production",
            "--operation-id", operation_id, "--confirm", "--non-interactive", "--json",
        ],
        stdout=stdout,
        stderr=StringIO(),
    )
    applied = json.loads(stdout.getvalue())
    assert status == 0
    assert applied["code"] == "III_MISSION_CATALOG_SELECTED"
    assert client.calls == [
        (mission.SELECT_COMMAND, {"default": False, "catalog_id": "inspection-production"})
    ]
