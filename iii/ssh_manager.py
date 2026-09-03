"""Key-only SSH/SFTP adapter for the single accepted ``iii.local`` endpoint."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import time
from typing import Any, Callable, Mapping

HOST = "iii.local"
USER = "iii-deploy"
PROFILE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
IDENTITY = re.compile(r"^[a-f0-9]{64}$")
OPERATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
PUBLIC_KEY = re.compile(r"^ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI[A-Za-z0-9+/]{43}$")
UPLOAD_SCHEMA = "iii.bundle-upload/v1"
UPLOAD_RESULT_SCHEMA = "iii.bundle-upload-result/v1"
COMPONENT_FILES = frozenset(
    {
        "bundle.manifest.json",
        "bundle.sha256",
        "bundle.sig.json",
        "bundle.tar.zst",
        "release-manifest.json",
    }
)
STATUS_INDEX_NAME = "release-status-index.json"
TRANSFER_TARGET_S = 120.0
BACKUP_UPLOAD_SCHEMA = "iii.portable-backup-upload/v1"
BACKUP_UPLOAD_RESULT_SCHEMA = "iii.portable-backup-upload-result/v1"
RECEIVER_UPDATE_UPLOAD_SCHEMA = "iii.receiver-update-upload/v1"
RECEIVER_UPDATE_UPLOAD_RESULT_SCHEMA = "iii.receiver-update-upload-result/v1"
RECEIVER_UPDATE_FILES = frozenset(
    {
        "receiver-update.manifest.json",
        "receiver-update.sig.json",
        "receiver-update.tar",
    }
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_identity(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


class SSHAdapterError(RuntimeError):
    """A stable transport classification without private-key material."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TransferResult:
    release_id: str
    upload_id: str
    transfer_id: str
    endpoint: str
    expected_profile: str
    resumed: bool
    bytes_total: int
    bytes_transferred: int
    elapsed_s: float
    target_s: float
    target_met: bool
    content_addressed_optimization_justified: bool
    optimization_assessment: str
    server_host_authentication: str
    logical_identity_checked: bool
    physical_host_authenticated: bool

    def as_dict(self) -> dict[str, Any]:
        return {"schema": "iii.ssh-bundle-transfer-result/v1", **self.__dict__}


@dataclass(frozen=True)
class ReceiverUpdateTransferResult:
    receiver_id: str
    upload_id: str
    transfer_id: str
    endpoint: str
    expected_profile: str
    resumed: bool
    bytes_total: int
    bytes_transferred: int
    elapsed_s: float
    target_s: float
    target_met: bool
    server_host_authentication: str
    logical_identity_checked: bool
    physical_host_authenticated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "iii.ssh-receiver-update-transfer-result/v1",
            **self.__dict__,
        }


class SSHManager:
    """Invoke only the forced receiver gateway and its fixed-root SFTP subsystem."""

    def __init__(
        self,
        *,
        identity_file: Path | None = None,
        public_key_file: Path | None = None,
        environment: Mapping[str, str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        environment = os.environ if environment is None else environment
        host = environment.get("III_SSH_HOST", HOST)
        user = environment.get("III_SSH_USER", USER)
        if host != HOST or user != USER:
            raise SSHAdapterError(
                "III_SSH_TARGET_REJECTED",
                "deployment SSH is fixed to the unprivileged iii-deploy@iii.local endpoint",
            )
        default_identity = Path(
            environment.get(
                "III_SSH_IDENTITY_FILE",
                str(
                    Path(
                        environment.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
                    )
                    / "iii/keys/ssh/id_ed25519"
                ),
            )
        )
        self.identity_file = Path(
            os.path.abspath((identity_file or default_identity).expanduser())
        )
        selected_public_key = public_key_file or Path(
            environment.get("III_SSH_PUBLIC_KEY_FILE", str(self.identity_file) + ".pub")
        )
        self.public_key_file = Path(os.path.abspath(selected_public_key.expanduser()))
        self._validate_private_key_path()
        self.public_key = self._read_public_key()
        self.client_id = hashlib.sha256(self.public_key.encode("ascii")).hexdigest()
        self.runner = runner
        self.monotonic = monotonic
        self.endpoint = f"{USER}@{HOST}"

    @property
    def accepted_host_risk(self) -> str:
        return (
            "iii.local server host keys are intentionally not authenticated; local "
            "endpoint spoofing or MITM remains an accepted initial risk"
        )

    def _validate_private_key_path(self) -> None:
        try:
            observed = self.identity_file.lstat()
        except OSError as exc:
            raise SSHAdapterError(
                "III_SSH_IDENTITY_UNAVAILABLE",
                "the configured per-computer SSH identity is unavailable",
            ) from exc
        if (
            self.identity_file.is_symlink()
            or not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or observed.st_mode & 0o077
        ):
            raise SSHAdapterError(
                "III_SSH_IDENTITY_UNSAFE",
                "the per-computer SSH private-key file must be regular and user-only",
            )

    def _read_public_key(self) -> str:
        try:
            observed = self.public_key_file.lstat()
            raw = self.public_key_file.read_text(encoding="ascii").strip().split()
        except (OSError, UnicodeDecodeError) as exc:
            raise SSHAdapterError(
                "III_SSH_PUBLIC_KEY_UNAVAILABLE",
                "the configured per-computer SSH public key is unavailable",
            ) from exc
        if (
            self.public_key_file.is_symlink()
            or not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
        ):
            raise SSHAdapterError(
                "III_SSH_PUBLIC_KEY_UNSAFE",
                "the per-computer SSH public-key path is not a regular file",
            )
        if len(raw) < 2:
            raise SSHAdapterError(
                "III_SSH_PUBLIC_KEY_INVALID", "the SSH public key is malformed"
            )
        key = f"{raw[0]} {raw[1]}"
        if not PUBLIC_KEY.fullmatch(key):
            raise SSHAdapterError(
                "III_SSH_PUBLIC_KEY_INVALID",
                "deployment requires one canonical Ed25519 public key",
            )
        try:
            decoded = base64.b64decode(raw[1], validate=True)
            prefix = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
            if len(decoded) != len(prefix) + 32 or not decoded.startswith(prefix):
                raise ValueError
        except (ValueError, binascii.Error) as exc:
            raise SSHAdapterError(
                "III_SSH_PUBLIC_KEY_INVALID",
                "deployment requires a 32-byte Ed25519 public key",
            ) from exc
        return key

    def _options(self) -> list[str]:
        return [
            "-o",
            "BatchMode=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ConnectTimeout=10",
            "-i",
            str(self.identity_file),
        ]

    def _run(
        self,
        argv: list[str],
        *,
        input_bytes: bytes | None = None,
        timeout: float = 900.0,
        accept_receiver_response: bool = False,
    ) -> subprocess.CompletedProcess:
        try:
            result = self.runner(
                argv,
                input=input_bytes,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SSHAdapterError(
                "III_SSH_UNREACHABLE", "iii.local did not complete the SSH operation"
            ) from exc
        if result.returncode == 0:
            return result
        if accept_receiver_response:
            stdout = (
                result.stdout
                if isinstance(result.stdout, bytes)
                else str(result.stdout or "").encode()
            )
            try:
                response = json.loads(stdout)
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = None
            if (
                isinstance(response, dict)
                and response.get("schema") == "iii.receiver-response/v1"
                and response.get("ok") is False
                and isinstance(response.get("error"), dict)
                and stdout == canonical_json(response) + b"\n"
            ):
                return result
        stderr = result.stderr
        if isinstance(stderr, bytes):
            detail = stderr.decode("utf-8", errors="replace")
        else:
            detail = str(stderr or "")
        detail = detail.replace(str(self.identity_file), "<identity-file>").strip()
        lowered = detail.lower()
        if "permission denied" in lowered or "no supported authentication" in lowered:
            code = "III_SSH_UNAUTHORIZED"
            message = "the per-computer SSH key is not authorized by iii.local"
        elif result.returncode == 255:
            code = "III_SSH_UNREACHABLE"
            message = "iii.local is unreachable over key-only SSH"
        else:
            code = "III_SSH_REMOTE_REJECTED"
            message = "the fixed remote deployment gateway rejected the operation"
        if detail:
            message += f": {detail[-1000:]}"
        raise SSHAdapterError(code, message)

    def _ssh(
        self,
        *,
        original_command: str | None,
        input_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        argv = ["ssh", *self._options(), self.endpoint]
        if original_command is not None:
            argv.append(original_command)
        result = self._run(
            argv,
            input_bytes=input_bytes,
            accept_receiver_response=original_command is None,
        )
        raw = (
            result.stdout
            if isinstance(result.stdout, bytes)
            else str(result.stdout).encode()
        )
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SSHAdapterError(
                "III_SSH_RESPONSE_INVALID",
                "the fixed deployment gateway returned invalid JSON",
            ) from exc
        if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
            raise SSHAdapterError(
                "III_SSH_RESPONSE_INVALID",
                "the fixed deployment gateway returned a non-canonical response",
            )
        return value

    def receiver_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(request)
        if value.get("client_id") != self.client_id:
            raise SSHAdapterError(
                "III_SSH_CLIENT_ID_MISMATCH",
                "receiver request client ID differs from the selected SSH key",
            )
        response = self._ssh(
            original_command=None, input_bytes=canonical_json(value) + b"\n"
        )
        if response.get("schema") != "iii.receiver-response/v1":
            raise SSHAdapterError(
                "III_SSH_RESPONSE_INVALID", "receiver response schema is unsupported"
            )
        if response.get("ok") is not True:
            error = response.get("error") or {}
            raise SSHAdapterError(
                "III_RECEIVER_REJECTED",
                str(error.get("message", "receiver rejected the request")),
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise SSHAdapterError(
                "III_SSH_RESPONSE_INVALID", "receiver result is malformed"
            )
        return result

    def verify_logical_target(
        self, *, profile: str, operation_id: str
    ) -> dict[str, Any]:
        if not PROFILE.fullmatch(profile) or not OPERATION_ID.fullmatch(operation_id):
            raise SSHAdapterError(
                "III_SSH_TARGET_REJECTED", "logical target probe arguments are invalid"
            )
        result = self.receiver_request(
            {
                "protocol_version": "1",
                "action": "status",
                "operation_id": operation_id,
                "client_id": self.client_id,
                "payload": {},
                "nonce": None,
            }
        )
        expected = {"logical_id": "drone", "profile": profile}
        if result.get("target") != expected:
            raise SSHAdapterError(
                "III_SSH_LOGICAL_TARGET_MISMATCH",
                "the responding endpoint advertises an unexpected logical runtime target; "
                "this check does not authenticate the physical host",
            )
        return result

    def _upload_control(
        self,
        action: str,
        release_id: str | None = None,
        *,
        document: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if action == "cleanup":
            command = "iii-upload cleanup"
        elif action in {"begin", "inspect", "touch", "finalize"} and (
            release_id is not None and IDENTITY.fullmatch(release_id)
        ):
            command = f"iii-upload {action} {release_id}"
        else:
            raise SSHAdapterError(
                "III_SSH_UPLOAD_ARGUMENT_INVALID",
                "upload control arguments are invalid",
            )
        result = self._ssh(
            original_command=command,
            input_bytes=(
                canonical_json(document) + b"\n" if document is not None else None
            ),
        )
        if result.get("schema") != UPLOAD_RESULT_SCHEMA:
            raise SSHAdapterError(
                "III_SSH_RESPONSE_INVALID",
                "upload gateway result schema is unsupported",
            )
        return result

    @staticmethod
    def _local_file(path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise SSHAdapterError(
                "III_SSH_BUNDLE_INVALID",
                f"bundle file is missing or linked: {path.name}",
            )
        digest = hashlib.sha256()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            observed = os.fstat(descriptor)
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
        finally:
            os.close(descriptor)
        return {"size": observed.st_size, "sha256": digest.hexdigest()}

    def _upload_manifest(
        self,
        component: Path,
        *,
        release_id: str,
        status_index: Path | None,
    ) -> tuple[dict[str, Any], dict[str, Path]]:
        if not IDENTITY.fullmatch(release_id):
            raise SSHAdapterError(
                "III_SSH_BUNDLE_INVALID", "bundle release identity is invalid"
            )
        component = component.expanduser()
        if component.is_symlink() or not component.is_dir():
            raise SSHAdapterError(
                "III_SSH_BUNDLE_INVALID", "bundle component directory is unavailable"
            )
        component = component.resolve()
        if {path.name for path in component.iterdir()} != COMPONENT_FILES:
            raise SSHAdapterError(
                "III_SSH_BUNDLE_INVALID", "bundle component file set is not exact"
            )
        paths = {f"drone/{name}": component / name for name in COMPONENT_FILES}
        if status_index is not None:
            status_index = status_index.expanduser()
            if status_index.is_symlink():
                raise SSHAdapterError(
                    "III_SSH_BUNDLE_INVALID", "release status index is linked"
                )
            status_index = status_index.resolve()
            paths[STATUS_INDEX_NAME] = status_index
        files = [
            {"path": relative, **self._local_file(path)}
            for relative, path in sorted(paths.items())
        ]
        try:
            raw = (component / "release-manifest.json").read_bytes()
            release = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SSHAdapterError(
                "III_SSH_BUNDLE_INVALID", "release manifest is unreadable"
            ) from exc
        if (
            raw != canonical_json(release) + b"\n"
            or release.get("release_id") != release_id
        ):
            raise SSHAdapterError(
                "III_SSH_BUNDLE_INVALID",
                "release manifest identity differs from upload",
            )
        manifest: dict[str, Any] = {
            "schema": UPLOAD_SCHEMA,
            "upload_id": "0" * 64,
            "release_id": release_id,
            "client_id": self.client_id,
            "files": files,
        }
        manifest["upload_id"] = content_identity(
            {key: item for key, item in manifest.items() if key != "upload_id"}
        )
        return manifest, paths

    @staticmethod
    def _sftp_quote(path: str) -> str:
        if any(ord(character) < 32 for character in path):
            raise SSHAdapterError(
                "III_SSH_BUNDLE_INVALID", "bundle path contains control characters"
            )
        return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _sftp(self, commands: list[str]) -> None:
        batch = ("\n".join(commands) + "\n").encode("utf-8")
        argv = ["sftp", "-q", "-b", "-", *self._options(), self.endpoint]
        self._run(argv, input_bytes=batch)

    @staticmethod
    def _validate_remote_status(
        remote: Mapping[str, Any], manifest: Mapping[str, Any]
    ) -> int:
        if set(remote) != {
            "schema",
            "release_id",
            "upload_id",
            "state",
            "resumed",
            "files",
        } or not isinstance(remote.get("resumed"), bool):
            raise SSHAdapterError(
                "III_SSH_PARTIAL_MISMATCH", "remote upload status is malformed"
            )
        if remote.get("release_id") != manifest["release_id"]:
            raise SSHAdapterError(
                "III_SSH_PARTIAL_MISMATCH",
                "remote partial has another release identity",
            )
        if remote.get("upload_id") != manifest["upload_id"]:
            raise SSHAdapterError(
                "III_SSH_PARTIAL_MISMATCH", "remote partial has another upload identity"
            )
        observed = remote.get("files")
        expected = {item["path"]: item for item in manifest["files"]}
        if not isinstance(observed, dict) or set(observed) != set(expected):
            raise SSHAdapterError(
                "III_SSH_PARTIAL_MISMATCH", "remote partial file inventory differs"
            )
        remaining = 0
        for relative, local in expected.items():
            value = observed[relative]
            if not isinstance(value, dict) or set(value) != {"size", "sha256"}:
                raise SSHAdapterError(
                    "III_SSH_PARTIAL_MISMATCH",
                    "remote partial size evidence is malformed",
                )
            size = value["size"]
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or not 0 <= size <= local["size"]
            ):
                raise SSHAdapterError(
                    "III_SSH_PARTIAL_MISMATCH",
                    "remote partial size exceeds local identity",
                )
            if size == local["size"] and value["sha256"] != local["sha256"]:
                raise SSHAdapterError(
                    "III_SSH_PARTIAL_MISMATCH", "remote completed file hash differs"
                )
            if size < local["size"] and value["sha256"] is not None:
                raise SSHAdapterError(
                    "III_SSH_PARTIAL_MISMATCH", "remote partial hash claim is invalid"
                )
            remaining += local["size"] - size
        return remaining

    def upload_bundle(
        self,
        component: Path,
        *,
        release_id: str,
        profile: str,
        status_index: Path | None = None,
        operation_id: str | None = None,
    ) -> TransferResult:
        operation_id = operation_id or f"transfer-{release_id[:16]}"
        self.verify_logical_target(profile=profile, operation_id=operation_id)
        manifest, paths = self._upload_manifest(
            component, release_id=release_id, status_index=status_index
        )
        started = self.monotonic()
        cleanup = self._upload_control("cleanup")
        if (
            set(cleanup)
            != {
                "schema",
                "state",
                "removed_release_ids",
                "retained_release_ids",
            }
            or cleanup.get("state") != "cleanup-complete"
            or not all(
                isinstance(values, list)
                and all(
                    isinstance(value, str) and IDENTITY.fullmatch(value)
                    for value in values
                )
                and len(values) == len(set(values))
                for values in (
                    cleanup.get("removed_release_ids"),
                    cleanup.get("retained_release_ids"),
                )
            )
        ):
            raise SSHAdapterError(
                "III_SSH_RESPONSE_INVALID", "upload cleanup result is malformed"
            )
        remote = self._upload_control("begin", release_id, document=manifest)
        total = sum(item["size"] for item in manifest["files"])
        initial_remaining = 0
        if remote.get("state") == "complete":
            if self._validate_remote_status(remote, manifest) != 0:
                raise SSHAdapterError(
                    "III_SSH_PARTIAL_MISMATCH",
                    "completed remote upload does not match the local bundle",
                )
        elif remote.get("state") == "partial":
            initial_remaining = self._validate_remote_status(remote, manifest)
            commands = []
            observed = remote["files"]
            indexed = {item["path"]: item for item in manifest["files"]}
            for relative, local in sorted(paths.items()):
                if observed[relative]["size"] == indexed[relative]["size"]:
                    continue
                remote_path = f"{release_id}.partial/{relative}"
                commands.append(
                    "reput -f "
                    + self._sftp_quote(str(local))
                    + " "
                    + self._sftp_quote(remote_path)
                )
            if commands:
                self._sftp(commands)
            completed = self._upload_control("finalize", release_id)
            if (
                completed.get("state") != "complete"
                or self._validate_remote_status(completed, manifest) != 0
            ):
                raise SSHAdapterError(
                    "III_SSH_TRANSFER_INCOMPLETE",
                    "the gateway did not finalize the exact complete bundle",
                )
        else:
            raise SSHAdapterError(
                "III_SSH_RESPONSE_INVALID", "upload begin result state is malformed"
            )
        elapsed = self.monotonic() - started
        target_met = elapsed <= TRANSFER_TARGET_S
        return TransferResult(
            release_id=release_id,
            upload_id=release_id,
            transfer_id=manifest["upload_id"],
            endpoint=self.endpoint,
            expected_profile=profile,
            resumed=remote.get("resumed") is True,
            bytes_total=total,
            bytes_transferred=initial_remaining,
            elapsed_s=elapsed,
            target_s=TRANSFER_TARGET_S,
            target_met=target_met,
            content_addressed_optimization_justified=False,
            optimization_assessment=(
                "not-justified-target-met"
                if target_met
                else "not-justified-repeat-measurement-required"
            ),
            server_host_authentication="accepted-risk-none",
            logical_identity_checked=True,
            physical_host_authenticated=False,
        )

    def _backup_upload_control(
        self,
        action: str,
        backup_id: str,
        *,
        document: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if action not in {"begin", "inspect", "finalize"} or not IDENTITY.fullmatch(
            backup_id
        ):
            raise SSHAdapterError(
                "III_SSH_BACKUP_UPLOAD_INVALID",
                "portable backup upload control arguments are invalid",
            )
        result = self._ssh(
            original_command=f"iii-backup-upload {action} {backup_id}",
            input_bytes=(
                canonical_json(document) + b"\n" if document is not None else None
            ),
        )
        if result.get("schema") != BACKUP_UPLOAD_RESULT_SCHEMA:
            raise SSHAdapterError(
                "III_SSH_RESPONSE_INVALID",
                "portable backup upload result schema is unsupported",
            )
        return result

    def _receiver_update_upload_control(
        self,
        action: str,
        receiver_id: str,
        *,
        document: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if action not in {"begin", "inspect", "finalize"} or not IDENTITY.fullmatch(
            receiver_id
        ):
            raise SSHAdapterError(
                "III_SSH_RECEIVER_UPLOAD_INVALID",
                "receiver update upload control arguments are invalid",
            )
        result = self._ssh(
            original_command=f"iii-receiver-upload {action} {receiver_id}",
            input_bytes=(
                canonical_json(document) + b"\n" if document is not None else None
            ),
        )
        if result.get("schema") != RECEIVER_UPDATE_UPLOAD_RESULT_SCHEMA:
            raise SSHAdapterError(
                "III_SSH_RESPONSE_INVALID",
                "receiver update upload result schema is unsupported",
            )
        return result

    @staticmethod
    def _validate_receiver_update_status(
        remote: Mapping[str, Any], manifest: Mapping[str, Any]
    ) -> int:
        if (
            set(remote)
            != {
                "schema",
                "receiver_id",
                "upload_id",
                "state",
                "resumed",
                "files",
            }
            or remote.get("schema") != RECEIVER_UPDATE_UPLOAD_RESULT_SCHEMA
            or remote.get("receiver_id") != manifest["receiver_id"]
            or remote.get("upload_id") != manifest["upload_id"]
            or not isinstance(remote.get("resumed"), bool)
        ):
            raise SSHAdapterError(
                "III_SSH_PARTIAL_MISMATCH",
                "remote receiver update status is malformed",
            )
        observed = remote.get("files")
        expected = {item["path"]: item for item in manifest["files"]}
        if not isinstance(observed, dict) or set(observed) != set(expected):
            raise SSHAdapterError(
                "III_SSH_PARTIAL_MISMATCH",
                "remote receiver update file inventory differs",
            )
        remaining = 0
        for relative, local in expected.items():
            value = observed[relative]
            if not isinstance(value, dict) or set(value) != {"size", "sha256"}:
                raise SSHAdapterError(
                    "III_SSH_PARTIAL_MISMATCH",
                    "remote receiver update file status is malformed",
                )
            size = value["size"]
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or not 0 <= size <= local["size"]
                or (size == local["size"] and value["sha256"] != local["sha256"])
                or (size < local["size"] and value["sha256"] is not None)
            ):
                raise SSHAdapterError(
                    "III_SSH_PARTIAL_MISMATCH",
                    "remote receiver update partial differs from local identity",
                )
            remaining += local["size"] - size
        return remaining

    def upload_receiver_update(
        self,
        bundle: Path,
        *,
        receiver_id: str,
        profile: str,
        operation_id: str,
    ) -> ReceiverUpdateTransferResult:
        """Resume one exact signed receiver update through the fixed gateway."""

        self.verify_logical_target(profile=profile, operation_id=operation_id)
        if not IDENTITY.fullmatch(receiver_id):
            raise SSHAdapterError(
                "III_SSH_RECEIVER_UPLOAD_INVALID", "receiver update identity is invalid"
            )
        bundle = bundle.expanduser().absolute()
        if bundle.is_symlink() or not bundle.is_dir():
            raise SSHAdapterError(
                "III_SSH_RECEIVER_UPLOAD_INVALID",
                "receiver update bundle is unavailable or linked",
            )
        if {path.name for path in bundle.iterdir()} != RECEIVER_UPDATE_FILES:
            raise SSHAdapterError(
                "III_SSH_RECEIVER_UPLOAD_INVALID",
                "receiver update bundle file set is not exact",
            )
        paths = {f"bundle/{name}": bundle / name for name in RECEIVER_UPDATE_FILES}
        files = [
            {"path": relative, **self._local_file(path)}
            for relative, path in sorted(paths.items())
        ]
        manifest: dict[str, Any] = {
            "schema": RECEIVER_UPDATE_UPLOAD_SCHEMA,
            "upload_id": "0" * 64,
            "receiver_id": receiver_id,
            "client_id": self.client_id,
            "files": files,
        }
        manifest["upload_id"] = content_identity(
            {key: value for key, value in manifest.items() if key != "upload_id"}
        )
        started = self.monotonic()
        remote = self._receiver_update_upload_control(
            "begin", receiver_id, document=manifest
        )
        resumed = remote.get("resumed") is True
        initial_remaining = self._validate_receiver_update_status(remote, manifest)
        if remote.get("state") == "partial":
            observed = remote["files"]
            indexed = {item["path"]: item for item in files}
            commands = []
            for relative, local in sorted(paths.items()):
                if observed[relative]["size"] == indexed[relative]["size"]:
                    continue
                commands.append(
                    "reput -f "
                    + self._sftp_quote(str(local))
                    + " "
                    + self._sftp_quote(f"receiver-{receiver_id}.partial/{relative}")
                )
            if commands:
                self._sftp(commands)
            remote = self._receiver_update_upload_control("finalize", receiver_id)
        if (
            remote.get("state") != "complete"
            or self._validate_receiver_update_status(remote, manifest) != 0
        ):
            raise SSHAdapterError(
                "III_SSH_TRANSFER_INCOMPLETE",
                "receiver update upload did not finalize exactly",
            )
        elapsed = self.monotonic() - started
        total = sum(item["size"] for item in files)
        return ReceiverUpdateTransferResult(
            receiver_id=receiver_id,
            upload_id=manifest["upload_id"],
            transfer_id=manifest["upload_id"],
            endpoint=self.endpoint,
            expected_profile=profile,
            resumed=resumed,
            bytes_total=total,
            bytes_transferred=initial_remaining,
            elapsed_s=elapsed,
            target_s=TRANSFER_TARGET_S,
            target_met=elapsed <= TRANSFER_TARGET_S,
            server_host_authentication="accepted-risk-none",
            logical_identity_checked=True,
            physical_host_authenticated=False,
        )

    def upload_backup(
        self,
        archive: Path,
        *,
        backup_id: str,
        profile: str,
        operation_id: str,
    ) -> dict[str, Any]:
        """Resume one verified portable archive into the fixed incoming root."""

        self.verify_logical_target(profile=profile, operation_id=operation_id)
        if not IDENTITY.fullmatch(backup_id):
            raise SSHAdapterError(
                "III_SSH_BACKUP_UPLOAD_INVALID", "portable backup identity is invalid"
            )
        archive = archive.expanduser().absolute()
        identity = self._local_file(archive)
        manifest: dict[str, Any] = {
            "schema": BACKUP_UPLOAD_SCHEMA,
            "upload_id": "0" * 64,
            "backup_id": backup_id,
            "client_id": self.client_id,
            "archive": identity,
        }
        manifest["upload_id"] = content_identity(
            {key: value for key, value in manifest.items() if key != "upload_id"}
        )
        started = self.monotonic()
        remote = self._backup_upload_control("begin", backup_id, document=manifest)
        observed = remote.get("archive")
        if (
            remote.get("backup_id") != backup_id
            or remote.get("upload_id") != manifest["upload_id"]
            or not isinstance(observed, dict)
            or set(observed) != {"size", "sha256"}
            or not isinstance(observed["size"], int)
            or isinstance(observed["size"], bool)
            or not 0 <= observed["size"] <= identity["size"]
            or (
                observed["size"] == identity["size"]
                and observed["sha256"] != identity["sha256"]
            )
        ):
            raise SSHAdapterError(
                "III_SSH_PARTIAL_MISMATCH",
                "remote portable backup partial differs from local identity",
            )
        initial_size = observed["size"]
        if remote.get("state") == "partial" and initial_size < identity["size"]:
            self._sftp(
                [
                    "reput -f "
                    + self._sftp_quote(str(archive))
                    + " "
                    + self._sftp_quote(f"backup-{backup_id}.partial/portable-state.tar")
                ]
            )
            remote = self._backup_upload_control("finalize", backup_id)
        if remote.get("state") != "complete" or remote.get("archive") != {
            "size": identity["size"],
            "sha256": identity["sha256"],
        }:
            raise SSHAdapterError(
                "III_SSH_TRANSFER_INCOMPLETE",
                "portable backup upload did not finalize exactly",
            )
        elapsed = self.monotonic() - started
        return {
            "schema": "iii.ssh-portable-backup-transfer-result/v1",
            "backup_id": backup_id,
            "upload_id": manifest["upload_id"],
            "archive_sha256": identity["sha256"],
            "bytes_total": identity["size"],
            "bytes_transferred": identity["size"] - initial_size,
            "resumed": initial_size > 0,
            "elapsed_s": elapsed,
            "target_s": TRANSFER_TARGET_S,
            "target_met": elapsed <= TRANSFER_TARGET_S,
            "endpoint": self.endpoint,
            "logical_identity_checked": True,
            "physical_host_authenticated": False,
        }

    def execute(self, _command):
        raise SSHAdapterError(
            "III_SSH_ARBITRARY_COMMAND_RETIRED",
            "arbitrary SSH command execution is not part of deployment",
        )

    def transfer_to_host(self, *_args, **_kwargs):
        raise SSHAdapterError(
            "III_SSH_SCP_RETIRED", "SCP is replaced by content-bound resumable SFTP"
        )

    def sync(self, *_args, **_kwargs):
        raise SSHAdapterError(
            "III_SSH_RSYNC_RETIRED", "source synchronization is not a deployment path"
        )

    reverse_sync = sync

    def open_session(self):
        raise SSHAdapterError(
            "III_SSH_SHELL_NOT_DEPLOYMENT",
            "general SSH shell access is outside the deployment adapter",
        )
