from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess
import struct

import pytest
from jsonschema import Draft7Validator

from iii.ssh_manager import (
    COMPONENT_FILES,
    SSHAdapterError,
    SSHManager,
    canonical_json,
)

RELEASE = "a" * 64


def _identity(tmp_path: Path) -> tuple[Path, Path]:
    private = tmp_path / "credentials/ssh_ed25519"
    private.parent.mkdir(parents=True)
    private.write_text(
        "private material must never enter argv or output", encoding="ascii"
    )
    private.chmod(0o600)
    public = Path(str(private) + ".pub")
    wire_key = (
        struct.pack(">I", len(b"ssh-ed25519"))
        + b"ssh-ed25519"
        + struct.pack(">I", 32)
        + b"k" * 32
    )
    public.write_text(
        "ssh-ed25519 " + base64.b64encode(wire_key).decode("ascii") + " workstation\n",
        encoding="ascii",
    )
    return private, public


def _component(tmp_path: Path) -> Path:
    root = tmp_path / 'bundle path "quoted"/drone'
    root.mkdir(parents=True)
    for name in COMPONENT_FILES:
        content = (
            canonical_json({"release_id": RELEASE}) + b"\n"
            if name == "release-manifest.json"
            else f"content:{name}\n".encode()
        )
        (root / name).write_bytes(content)
    return root


def test_default_identity_matches_gc_provisioning_contract(tmp_path: Path) -> None:
    private, public = _identity(tmp_path)
    expected = tmp_path / "config/iii/keys/ssh/id_ed25519"
    expected.parent.mkdir(parents=True)
    private.replace(expected)
    public.replace(Path(str(expected) + ".pub"))

    manager = SSHManager(environment={"XDG_CONFIG_HOME": str(tmp_path / "config")})

    assert manager.identity_file == expected
    assert manager.public_key_file == Path(str(expected) + ".pub")


class GatewayRunner:
    def __init__(
        self,
        *,
        profile: str = "real",
        sftp_returncode: int = 0,
        resumed: bool = False,
        completed_paths: set[str] | None = None,
        begin_upload_id: str | None = None,
    ) -> None:
        self.profile = profile
        self.sftp_returncode = sftp_returncode
        self.resumed = resumed
        self.completed_paths = completed_paths or set()
        self.begin_upload_id = begin_upload_id
        self.calls = []
        self.manifest = None

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[0] == "sftp":
            return subprocess.CompletedProcess(
                argv,
                self.sftp_returncode,
                b"",
                b"connection lost" if self.sftp_returncode else b"",
            )
        command = argv[-1] if argv[-1].startswith("iii-upload ") else None
        if command is None:
            request = json.loads(kwargs["input"])
            response = {
                "schema": "iii.receiver-response/v1",
                "ok": True,
                "result": {
                    "schema": "iii.receiver-result/v1",
                    "target": {"logical_id": "drone", "profile": self.profile},
                    "operation_id": request["operation_id"],
                },
            }
        elif command == "iii-upload cleanup":
            response = {
                "schema": "iii.bundle-upload-result/v1",
                "state": "cleanup-complete",
                "removed_release_ids": [],
                "retained_release_ids": [],
            }
        elif command.startswith("iii-upload begin "):
            self.manifest = json.loads(kwargs["input"])
            response = self._upload_status(complete=False)
            response["resumed"] = self.resumed
            if self.begin_upload_id is not None:
                response["upload_id"] = self.begin_upload_id
        elif command.startswith("iii-upload inspect "):
            response = self._upload_status(complete=True)
            response["state"] = "partial"
        elif command.startswith("iii-upload touch "):
            response = self._upload_status(complete=False)
        elif command.startswith("iii-upload finalize "):
            response = self._upload_status(complete=True)
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(
            argv, 0, canonical_json(response) + b"\n", b""
        )

    def _upload_status(self, *, complete: bool):
        assert self.manifest is not None
        return {
            "schema": "iii.bundle-upload-result/v1",
            "release_id": RELEASE,
            "upload_id": self.manifest["upload_id"],
            "state": "complete" if complete else "partial",
            "resumed": True,
            "files": {
                item["path"]: {
                    "size": (
                        item["size"]
                        if complete or item["path"] in self.completed_paths
                        else 0
                    ),
                    "sha256": (
                        item["sha256"]
                        if complete or item["path"] in self.completed_paths
                        else None
                    ),
                }
                for item in self.manifest["files"]
            },
        }


def test_key_only_fixed_endpoint_options_never_forward_agent_or_use_password(
    tmp_path: Path,
) -> None:
    private, public = _identity(tmp_path)
    runner = GatewayRunner()
    manager = SSHManager(
        identity_file=private,
        public_key_file=public,
        runner=runner,
    )
    manager.verify_logical_target(profile="real", operation_id="target-probe-0001")
    argv = runner.calls[0][0]
    serialized = " ".join(argv)
    assert argv[0] == "ssh" and argv[-1] == "iii-deploy@iii.local"
    assert "BatchMode=yes" in argv
    assert "PasswordAuthentication=no" in argv
    assert "KbdInteractiveAuthentication=no" in argv
    assert "ForwardAgent=no" in argv and "ClearAllForwardings=yes" in argv
    assert "StrictHostKeyChecking=no" in argv
    assert "UserKnownHostsFile=/dev/null" in argv
    assert "sshpass" not in serialized
    assert private.read_text() not in serialized
    assert "not authenticated" in manager.accepted_host_risk
    with pytest.raises(SSHAdapterError, match="fixed"):
        SSHManager(
            identity_file=private,
            public_key_file=public,
            environment={"III_SSH_HOST": "attacker.local", "III_SSH_USER": "iii"},
        )


def test_complete_bundle_upload_is_resumable_fixed_root_and_records_budget(
    tmp_path: Path,
) -> None:
    private, public = _identity(tmp_path)
    runner = GatewayRunner()
    ticks = iter((10.0, 25.0))
    manager = SSHManager(
        identity_file=private,
        public_key_file=public,
        runner=runner,
        monotonic=lambda: next(ticks),
    )
    result = manager.upload_bundle(
        _component(tmp_path), release_id=RELEASE, profile="real"
    )
    assert result.endpoint == "iii-deploy@iii.local"
    assert result.elapsed_s == 15.0 and result.target_met is True
    assert result.content_addressed_optimization_justified is False
    assert result.optimization_assessment == "not-justified-target-met"
    assert result.bytes_transferred == result.bytes_total
    assert result.server_host_authentication == "accepted-risk-none"
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "iii/schemas/ssh-bundle-transfer-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft7Validator(schema).validate(result.as_dict())
    sftp = next(call for call in runner.calls if call[0][0] == "sftp")
    batch = sftp[1]["input"].decode("utf-8")
    assert batch.count("reput -f ") == len(COMPONENT_FILES)
    assert f'"{RELEASE}.partial/drone/' in batch
    assert 'bundle path \\"quoted\\"' in batch
    assert all(call[0][0] in {"ssh", "sftp"} for call in runner.calls)
    commands = [call[0][-1] for call in runner.calls if call[0][0] == "ssh"]
    assert commands[-1] == f"iii-upload finalize {RELEASE}"


def test_interrupted_sftp_preserves_partial_and_classifies_unreachable(
    tmp_path: Path,
) -> None:
    private, public = _identity(tmp_path)
    runner = GatewayRunner(sftp_returncode=255)
    manager = SSHManager(
        identity_file=private,
        public_key_file=public,
        runner=runner,
    )
    with pytest.raises(SSHAdapterError) as failure:
        manager.upload_bundle(_component(tmp_path), release_id=RELEASE, profile="real")
    assert failure.value.code == "III_SSH_UNREACHABLE"
    commands = [call[0][-1] for call in runner.calls if call[0][0] == "ssh"]
    assert f"iii-upload begin {RELEASE}" in commands
    assert f"iii-upload finalize {RELEASE}" not in commands


def test_single_transfer_budget_miss_requires_repeated_commissioning_evidence(
    tmp_path: Path,
) -> None:
    private, public = _identity(tmp_path)
    runner = GatewayRunner()
    ticks = iter((10.0, 131.0))
    manager = SSHManager(
        identity_file=private,
        public_key_file=public,
        runner=runner,
        monotonic=lambda: next(ticks),
    )
    result = manager.upload_bundle(
        _component(tmp_path), release_id=RELEASE, profile="real"
    )
    assert result.elapsed_s == 121.0 and result.target_met is False
    assert result.content_addressed_optimization_justified is False
    assert result.optimization_assessment == "not-justified-repeat-measurement-required"


def test_matching_partial_resumes_and_mismatched_identity_is_rejected(
    tmp_path: Path,
) -> None:
    private, public = _identity(tmp_path)
    completed = {"drone/bundle.sha256"}
    runner = GatewayRunner(resumed=True, completed_paths=completed)
    ticks = iter((10.0, 11.0))
    manager = SSHManager(
        identity_file=private,
        public_key_file=public,
        runner=runner,
        monotonic=lambda: next(ticks),
    )
    component = _component(tmp_path)
    result = manager.upload_bundle(component, release_id=RELEASE, profile="real")
    assert result.resumed is True
    assert (
        result.bytes_transferred
        == result.bytes_total - (component / "bundle.sha256").stat().st_size
    )
    sftp = next(call for call in runner.calls if call[0][0] == "sftp")
    assert "bundle.sha256" not in sftp[1]["input"].decode("utf-8")

    mismatch = SSHManager(
        identity_file=private,
        public_key_file=public,
        runner=GatewayRunner(begin_upload_id="f" * 64),
    )
    with pytest.raises(SSHAdapterError) as failure:
        mismatch.upload_bundle(component, release_id=RELEASE, profile="real")
    assert failure.value.code == "III_SSH_PARTIAL_MISMATCH"


def test_portable_backup_upload_uses_fixed_resumable_sftp_boundary(
    tmp_path: Path,
) -> None:
    private, public = _identity(tmp_path)
    archive = tmp_path / "portable-state.tar"
    archive.write_bytes(b"portable archive fixture" * 100)
    backup_id = "d" * 64

    class BackupRunner:
        def __init__(self) -> None:
            self.calls = []
            self.manifest = None

        def __call__(self, argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            if argv[0] == "sftp":
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            command = argv[-1] if argv[-1].startswith("iii-backup-upload ") else None
            if command is None:
                request = json.loads(kwargs["input"])
                response = {
                    "schema": "iii.receiver-response/v1",
                    "ok": True,
                    "result": {
                        "schema": "iii.receiver-result/v1",
                        "target": {"logical_id": "drone", "profile": "real"},
                        "operation_id": request["operation_id"],
                    },
                }
            elif command.startswith("iii-backup-upload begin "):
                self.manifest = json.loads(kwargs["input"])
                response = self.status(complete=False)
            elif command.startswith("iii-backup-upload finalize "):
                response = self.status(complete=True)
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(
                argv, 0, canonical_json(response) + b"\n", b""
            )

        def status(self, *, complete):
            assert self.manifest is not None
            expected = self.manifest["archive"]
            return {
                "schema": "iii.portable-backup-upload-result/v1",
                "backup_id": self.manifest["backup_id"],
                "upload_id": self.manifest["upload_id"],
                "state": "complete" if complete else "partial",
                "resumed": False,
                "archive": (expected if complete else {"size": 0, "sha256": None}),
            }

    runner = BackupRunner()
    ticks = iter((10.0, 12.0))
    manager = SSHManager(
        identity_file=private,
        public_key_file=public,
        runner=runner,
        monotonic=lambda: next(ticks),
    )
    result = manager.upload_backup(
        archive,
        backup_id=backup_id,
        profile="real",
        operation_id="backup-upload-probe",
    )
    assert result["backup_id"] == backup_id
    assert result["bytes_transferred"] == archive.stat().st_size
    assert result["target_met"] is True
    sftp = next(call for call in runner.calls if call[0][0] == "sftp")
    assert (
        f'"backup-{backup_id}.partial/portable-state.tar"' in sftp[1]["input"].decode()
    )
    commands = [call[0][-1] for call in runner.calls if call[0][0] == "ssh"]
    assert commands[-1] == f"iii-backup-upload finalize {backup_id}"


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (b"Permission denied (publickey).", "III_SSH_UNAUTHORIZED"),
        (b"No route to host", "III_SSH_UNREACHABLE"),
    ],
)
def test_authentication_and_connectivity_failures_are_distinct_and_redacted(
    tmp_path: Path, stderr: bytes, code: str
) -> None:
    private, public = _identity(tmp_path)

    def fail(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 255, b"", stderr + str(private).encode()
        )

    manager = SSHManager(
        identity_file=private,
        public_key_file=public,
        runner=fail,
    )
    with pytest.raises(SSHAdapterError) as failure:
        manager.verify_logical_target(profile="real", operation_id="target-probe-0002")
    assert failure.value.code == code
    assert str(private) not in str(failure.value)
    assert private.read_text() not in str(failure.value)


def test_receiver_rejection_uses_canonical_stdout_despite_remote_warning(
    tmp_path: Path,
) -> None:
    private, public = _identity(tmp_path)
    response = {
        "schema": "iii.receiver-response/v1",
        "ok": False,
        "error": {
            "code": "III_RECEIVER_STATE_UNAVAILABLE",
            "message": "no active release is selected",
        },
    }

    def reject(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv,
            1,
            canonical_json(response) + b"\n",
            b"bash: warning: setlocale: LC_ALL: cannot change locale\n",
        )

    manager = SSHManager(
        identity_file=private,
        public_key_file=public,
        runner=reject,
    )
    with pytest.raises(SSHAdapterError) as failure:
        manager.verify_logical_target(profile="real", operation_id="target-probe-0004")
    assert failure.value.code == "III_RECEIVER_REJECTED"
    assert str(failure.value) == "no active release is selected"


@pytest.mark.parametrize(
    "response",
    [
        {"schema": "iii.receiver-response/v1", "ok": True, "result": {}},
        {
            "schema": "iii.bundle-upload-result/v1",
            "ok": False,
            "error": {"message": "not a receiver response"},
        },
        {"schema": "iii.receiver-response/v1", "ok": False},
    ],
)
def test_nonzero_ssh_accepts_only_a_canonical_receiver_rejection(
    tmp_path: Path, response: dict[str, object]
) -> None:
    private, public = _identity(tmp_path)

    def reject(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 1, canonical_json(response) + b"\n", b"remote command failed"
        )

    manager = SSHManager(
        identity_file=private,
        public_key_file=public,
        runner=reject,
    )
    with pytest.raises(SSHAdapterError) as failure:
        manager.verify_logical_target(profile="real", operation_id="target-probe-0005")
    assert failure.value.code == "III_SSH_REMOTE_REJECTED"


def test_receiver_update_transfer_uses_fixed_gateway_and_resumable_sftp(
    tmp_path: Path,
) -> None:
    private, public = _identity(tmp_path)
    bundle = tmp_path / "receiver"
    bundle.mkdir()
    receiver_id = "a" * 64
    for name in (
        "receiver-update.manifest.json",
        "receiver-update.sig.json",
        "receiver-update.tar",
    ):
        (bundle / name).write_text(f"{name}\n", encoding="utf-8")

    class Runner:
        def __init__(self):
            self.calls = []
            self.manifest = None

        def __call__(self, argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            if argv[0] == "sftp":
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            command = argv[-1] if argv[-1].startswith("iii-receiver-upload ") else None
            if command is None:
                response = {
                    "schema": "iii.receiver-response/v1",
                    "ok": True,
                    "result": {
                        "schema": "iii.receiver-result/v1",
                        "target": {"logical_id": "drone", "profile": "real"},
                    },
                }
            elif command.startswith("iii-receiver-upload begin "):
                self.manifest = json.loads(kwargs["input"])
                response = self.status(complete=False, resumed=True)
            elif command.startswith("iii-receiver-upload finalize "):
                response = self.status(complete=True, resumed=False)
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(
                argv, 0, canonical_json(response) + b"\n", b""
            )

        def status(self, *, complete, resumed):
            return {
                "schema": "iii.receiver-update-upload-result/v1",
                "receiver_id": receiver_id,
                "upload_id": self.manifest["upload_id"],
                "state": "complete" if complete else "partial",
                "resumed": resumed,
                "files": {
                    item["path"]: (
                        {"size": item["size"], "sha256": item["sha256"]}
                        if complete
                        else {"size": 0, "sha256": None}
                    )
                    for item in self.manifest["files"]
                },
            }

    runner = Runner()
    manager = SSHManager(identity_file=private, public_key_file=public, runner=runner)
    transfer = manager.upload_receiver_update(
        bundle,
        receiver_id=receiver_id,
        profile="real",
        operation_id="receiver-update-0001",
    )
    assert transfer.receiver_id == receiver_id
    assert transfer.resumed is True
    commands = [call[0][-1] for call in runner.calls if call[0][0] == "ssh"]
    assert commands[-2:] == [
        f"iii-receiver-upload begin {receiver_id}",
        f"iii-receiver-upload finalize {receiver_id}",
    ]
    sftp = next(call for call in runner.calls if call[0][0] == "sftp")
    assert (
        f'"receiver-{receiver_id}.partial/bundle/receiver-update.tar"'
        in sftp[1]["input"].decode()
    )


def test_unexpected_logical_runtime_and_arbitrary_commands_fail_closed(
    tmp_path: Path,
) -> None:
    private, public = _identity(tmp_path)
    manager = SSHManager(
        identity_file=private,
        public_key_file=public,
        runner=GatewayRunner(profile="sim"),
    )
    with pytest.raises(SSHAdapterError) as failure:
        manager.verify_logical_target(profile="real", operation_id="target-probe-0003")
    assert failure.value.code == "III_SSH_LOGICAL_TARGET_MISMATCH"
    with pytest.raises(SSHAdapterError) as retired:
        manager.execute("id; touch /tmp/injected")
    assert retired.value.code == "III_SSH_ARBITRARY_COMMAND_RETIRED"


def test_identity_and_bundle_symlinks_fail_closed(tmp_path: Path) -> None:
    private, public = _identity(tmp_path)
    linked_private = tmp_path / "linked-private"
    linked_private.symlink_to(private)
    with pytest.raises(SSHAdapterError) as unsafe_identity:
        SSHManager(identity_file=linked_private, public_key_file=public)
    assert unsafe_identity.value.code == "III_SSH_IDENTITY_UNSAFE"

    manager = SSHManager(
        identity_file=private,
        public_key_file=public,
        runner=GatewayRunner(),
    )
    component = _component(tmp_path)
    linked_component = tmp_path / "linked-component"
    linked_component.symlink_to(component, target_is_directory=True)
    with pytest.raises(SSHAdapterError) as unsafe_bundle:
        manager.upload_bundle(linked_component, release_id=RELEASE, profile="real")
    assert unsafe_bundle.value.code == "III_SSH_BUNDLE_INVALID"
