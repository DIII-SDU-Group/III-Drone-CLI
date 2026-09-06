from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace

from iii.__main__ import main


def test_host_image_cli_has_no_alternate_source_or_profile_override(
    tmp_path: Path,
) -> None:
    output = StringIO()
    status = main(
        [
            "host",
            "image",
            "inspect",
            "--image",
            str(tmp_path / "image"),
            "--bootstrap-input",
            str(tmp_path / "input"),
            "--source",
            str(tmp_path / "alternate-source.json"),
            "--json",
        ],
        stdout=output,
        stderr=StringIO(),
    )
    value = json.loads(output.getvalue())
    assert status == 64
    assert value["code"] == "III_USAGE_ERROR"
    assert "unrecognized arguments: --source" in value["findings"][0]["message"]


def test_host_image_inspection_uses_canonical_result_without_secret_values(
    monkeypatch, tmp_path: Path
) -> None:
    import iii.host as host
    import iii_deployment.host_imaging as imaging

    monkeypatch.setattr(
        host,
        "_paths",
        lambda _args: {"schema": tmp_path, "source": tmp_path, "profile": tmp_path},
    )
    monkeypatch.setattr(
        imaging,
        "load_contract",
        lambda path, **kwargs: (
            {"schema": "iii.host-image-source/v1", "release": "24.04.4"}
            if kwargs["schema_name"] == "host-image-source"
            else {"schema": "iii.cloud-init-profile/v1"}
        ),
    )
    monkeypatch.setattr(
        imaging,
        "load_bootstrap_input",
        lambda *_args: {
            "network": {"wifi": [{"ssid": "secret-ssid", "password": "secret-pass"}]}
        },
    )
    monkeypatch.setattr(
        imaging,
        "inspect_image",
        lambda *_args: {"verified": True, "minimum_target_bytes": 1},
    )
    monkeypatch.setattr(
        imaging,
        "render_nocloud_seed",
        lambda **_kwargs: {
            "profile_id": "profile-test",
            "instance_id": "instance-test",
            "file_evidence": [],
            "contains_network_secret": True,
        },
    )
    monkeypatch.setattr(
        imaging,
        "inspect_devices",
        lambda **_kwargs: [{"stable_path": "/dev/disk/by-id/test", "eligible": True}],
    )
    output = StringIO()
    status = main(
        [
            "host",
            "image",
            "inspect",
            "--image",
            str(tmp_path / "image"),
            "--bootstrap-input",
            str(tmp_path / "input"),
            "--json",
        ],
        stdout=output,
        stderr=StringIO(),
    )
    value = json.loads(output.getvalue())
    assert status == 0
    assert value["code"] == "III_HOST_IMAGE_INSPECTED"
    assert value["payload"]["cloud_init"]["secret_values_rendered"] is False
    assert "secret-ssid" not in output.getvalue()
    assert "secret-pass" not in output.getvalue()


def test_host_image_write_cannot_bypass_typed_proof_noninteractively(
    monkeypatch, tmp_path: Path
) -> None:
    import iii.host as host
    import iii_deployment.host_imaging as imaging

    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path / "operations"))
    monkeypatch.setattr(
        host,
        "_paths",
        lambda _args: {"schema": tmp_path, "source": tmp_path, "profile": tmp_path},
    )
    monkeypatch.setattr(
        host, "image_write_preflight", lambda _args: {"schema": "fixture-plan"}
    )

    def requires_proof(*_args, **_kwargs):
        input("Type exact physical device proof: ")

    monkeypatch.setattr(imaging, "apply_image_plan", requires_proof)
    output = StringIO()
    status = main(
        [
            "host",
            "image",
            "write",
            "--image",
            str(tmp_path / "image"),
            "--bootstrap-input",
            str(tmp_path / "input"),
            "--device",
            "/dev/disk/by-id/test",
            "--evidence-directory",
            str(tmp_path / "evidence"),
            "--accept-data-loss",
            "--confirm",
            "--non-interactive",
            "--json",
        ],
        stdout=output,
        stderr=StringIO(),
    )
    value = json.loads(output.getvalue())
    assert status == 20
    assert value["code"] == "III_REQUIRED_INPUT"
    assert value["findings"][0]["field"] == "input"


def test_host_image_dry_run_retains_exact_plan_without_calling_apply(
    monkeypatch, tmp_path: Path
) -> None:
    import iii.host as host

    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path / "operations"))
    monkeypatch.setattr(
        host,
        "image_write_preflight",
        lambda _args: {"schema": "fixture-plan", "device": "fingerprint"},
    )
    output = StringIO()
    status = main(
        [
            "host",
            "image",
            "write",
            "--image",
            str(tmp_path / "image"),
            "--bootstrap-input",
            str(tmp_path / "input"),
            "--device",
            "/dev/disk/by-id/test",
            "--evidence-directory",
            str(tmp_path / "evidence"),
            "--accept-data-loss",
            "--dry-run",
            "--operation-id",
            "iii-host-image-dry-run",
            "--json",
        ],
        stdout=output,
        stderr=StringIO(),
    )
    value = json.loads(output.getvalue())
    assert status == 0
    assert value["code"] == "III_OPERATION_PLAN_READY"
    assert value["payload"]["plan"]["preflight"]["device"] == "fingerprint"


def test_host_provision_apply_retains_exact_preflight_without_connecting(
    monkeypatch, tmp_path: Path
) -> None:
    import iii.host as host

    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path / "operations"))
    monkeypatch.setattr(
        host,
        "provision_apply_preflight",
        lambda _args: {
            "schema": "iii.host-provisioning-plan/v1",
            "target": "iii.local",
            "profile": "real",
            "content_id": "a" * 64,
        },
    )
    output = StringIO()
    status = main(
        [
            "host",
            "provision",
            "apply",
            "--target",
            "iii.local",
            "--inventory",
            str(tmp_path / "inventory.yml"),
            "--inputs",
            str(tmp_path / "inputs.json"),
            "--dry-run",
            "--operation-id",
            "iii-host-provision-dry-run",
            "--json",
        ],
        stdout=output,
        stderr=StringIO(),
    )
    value = json.loads(output.getvalue())
    assert status == 0
    assert value["code"] == "III_OPERATION_PLAN_READY"
    assert value["payload"]["plan"]["preflight"]["content_id"] == "a" * 64


def test_host_provision_check_is_declared_read_only(
    monkeypatch, tmp_path: Path
) -> None:
    import iii.host as host
    import iii_deployment.host_provision as provision

    monkeypatch.setattr(
        host,
        "_provision_plan",
        lambda _args, operation_id: {
            "schema": "iii.host-provisioning-plan/v1",
            "operation_id": operation_id,
            "target": "iii.local",
            "profile": "real",
        },
    )
    monkeypatch.setattr(
        host,
        "_provision_paths",
        lambda _args: {"schema": tmp_path},
    )
    monkeypatch.setattr(
        provision,
        "check_plan",
        lambda *_args, **_kwargs: {
            "schema": "iii.ansible-run-result/v1",
            "totals": {"changed": 7},
        },
    )
    output = StringIO()
    status = main(
        [
            "host",
            "provision",
            "check",
            "--target",
            "iii.local",
            "--inventory",
            str(tmp_path / "inventory.yml"),
            "--inputs",
            str(tmp_path / "inputs.json"),
            "--json",
        ],
        stdout=output,
        stderr=StringIO(),
    )
    value = json.loads(output.getvalue())
    assert status == 0
    assert value["code"] == "III_HOST_PROVISION_CHECKED"
    assert value["payload"]["mutation_performed"] is False
    assert value["payload"]["ansible"]["totals"]["changed"] == 7


def test_host_provision_success_routes_field_check_to_profile_not_inventory_host(
    monkeypatch, tmp_path: Path
) -> None:
    import iii.host as host
    import iii_deployment.host_provision as provision

    monkeypatch.setattr(
        provision,
        "apply_plan",
        lambda *_args, **_kwargs: {
            "schema": "iii.host-provisioning-run/v1",
            "report_id": "a" * 64,
        },
    )
    monkeypatch.setattr(host, "_provision_paths", lambda _args: {"schema": tmp_path})
    args = SimpleNamespace(
        target="10.42.0.15",
        _iii_operation_id=None,
        _iii_retained_plan={"preflight": {"profile": "real"}},
    )

    result = host.provision_apply(args)

    assert result.next_actions[0].command == (
        "iii",
        "field",
        "check",
        "--target",
        "real",
    )
    assert result.next_actions[0].target == "real"
