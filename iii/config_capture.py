"""Immutable local configuration captures and portable capture archives."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Iterable, Mapping
from uuid import uuid4
import zipfile

from .result import CommandResult, Finding, Outcome
from .runtime_api_client import RuntimeApiClient

SHA256 = re.compile(r"^[a-f0-9]{64}$")
SHORT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+-]{0,63}$")
SECRET_NAME = re.compile(
    r"(?:^|[/_.-])(password|passwd|secret|token|credential|private[_-]?key)(?:$|[/_.-])",
    re.IGNORECASE,
)
ARCHIVE_SCHEMA = "iii.configuration-capture-archive/v1"
METADATA_SCHEMA = "iii.configuration-capture-metadata/v1"
PARTIAL_SCHEMA = "iii.configuration-capture-partial/v1"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


def _capture_contracts():
    try:
        from iii_drone_contracts import configuration_capture
    except ImportError as exc:
        raise ValueError(
            "III-Drone-Contracts is not installed; build/source the III workspace"
        ) from exc
    return configuration_capture


def canonical_json(value: Any) -> bytes:
    return _capture_contracts().canonical_json(value)


def content_identity(value: Any) -> str:
    return _capture_contracts().content_identity(value)


def seal_capture(value: Mapping[str, Any]) -> dict[str, Any]:
    return _capture_contracts().seal_capture(value)


def verify_capture(value: Mapping[str, Any]) -> dict[str, Any]:
    return _capture_contracts().verify_capture(value)


def capture_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    return _capture_contracts().capture_receipt(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _environment(args: argparse.Namespace) -> Mapping[str, str]:
    return getattr(args, "_iii_environment", os.environ)


def _workspace(args: argparse.Namespace) -> Path:
    configured = _environment(args).get("WORKSPACE_DIR")
    candidates = [Path(configured)] if configured else []
    candidates.extend((Path.cwd(), *Path.cwd().parents))
    for candidate in candidates:
        resolved = candidate.expanduser().absolute()
        if (resolved / "deps/submodule-lock.txt").is_file():
            return resolved
    raise ValueError("cannot locate the current III workspace clone")


def _capture_root(args: argparse.Namespace, *, create: bool = False) -> Path:
    explicit = getattr(args, "capture_root", None)
    env = _environment(args)
    path = (
        Path(
            explicit
            or env.get("III_CAPTURE_ROOT")
            or _workspace(args) / ".iii/captures"
        )
        .expanduser()
        .absolute()
    )
    if create:
        return _safe_directory(path)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"capture root is not a real directory: {path}")
    return path


def _safe_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o750)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"capture root is not a real directory: {path}")
    metadata = path.stat(follow_symlinks=False)
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise ValueError(f"capture root is not owned by this user: {path}")
    if stat.S_IMODE(metadata.st_mode) & 0o027:
        os.chmod(path, 0o750)
    return path


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _immutable_document(path: Path, value: Mapping[str, Any]) -> None:
    parent = _safe_directory(path.parent)
    raw = canonical_json(value) + b"\n"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
            raise ValueError(f"immutable capture path collides: {path}")
        return
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_document(path: Path, value: Mapping[str, Any]) -> None:
    parent = _safe_directory(path.parent)
    temporary = parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(canonical_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _publish_bytes_without_overwrite(path: Path, raw: bytes) -> None:
    """Durably publish bytes while retaining a visible partial on interruption."""

    path = path.expanduser().absolute()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError(f"archive parent is not a real directory: {parent}")
    if path.exists() or path.is_symlink():
        raise ValueError("archive destination already exists")
    partial = parent / f".{path.name}.{uuid4().hex}.partial"
    descriptor = os.open(
        partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640
    )
    published = False
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(parent)
        os.link(partial, path, follow_symlinks=False)
        published = True
        _fsync_directory(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if published:
            partial.unlink()
            _fsync_directory(parent)


def _read_document(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing or linked")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise ValueError(f"{label} is not canonical JSON")
    return value


def _capture_path(root: Path, capture_id: str) -> Path:
    if not SHA256.fullmatch(capture_id):
        raise ValueError("capture ID is malformed")
    path = root / capture_id
    if path.parent != root:
        raise ValueError("capture path escapes its fixed root")
    return path


def _reject_secrets(source: Mapping[str, Any]) -> None:
    values = source.get("values")
    document = source.get("parameter_document")
    if not isinstance(values, dict) or not isinstance(document, dict):
        raise ValueError("capture source values are invalid")
    names = set(values)

    def collect(node: Mapping[str, Any]) -> None:
        if "ros__parameters" in node:
            if set(node) != {"ros__parameters"} or not isinstance(
                node["ros__parameters"], dict
            ):
                raise ValueError("capture source parameter document is invalid")
            names.update(str(name) for name in node["ros__parameters"])
            return
        if not node:
            raise ValueError("capture source parameter document is invalid")
        for nested in node.values():
            if not isinstance(nested, dict):
                raise ValueError("capture source parameter document is invalid")
            collect(nested)

    collect(document)
    prohibited = sorted(name for name in names if SECRET_NAME.search(str(name)))
    if prohibited:
        raise ValueError(
            "configuration capture contains prohibited secret-bearing parameter names: "
            + ", ".join(prohibited)
        )


def _verified(root: Path, capture_id: str) -> tuple[dict[str, Any], Path]:
    path = _capture_path(root, capture_id) / "capture.json"
    value = _read_document(path, label="configuration capture")
    verified = verify_capture(value)
    if verified["capture_id"] != capture_id:
        raise ValueError("capture directory and content identities differ")
    _reject_secrets(verified["source"])
    return verified, path


def _metadata(root: Path, capture_id: str) -> list[dict[str, Any]]:
    directory = _capture_path(root, capture_id) / "metadata"
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("capture metadata directory is unsafe")
    values = []
    for path in sorted(directory.glob("*.json")):
        value = _read_document(path, label="capture metadata")
        expected = content_identity(
            {key: item for key, item in value.items() if key != "metadata_id"}
        )
        if (
            set(value)
            != {
                "schema",
                "metadata_id",
                "capture_id",
                "short_name",
                "description",
                "captured_at",
                "source_locator",
            }
            or value.get("schema") != METADATA_SCHEMA
            or value.get("metadata_id") != expected
            or path.stem != expected
            or value.get("capture_id") != capture_id
        ):
            raise ValueError("capture metadata identity is invalid")
        values.append(value)
    return values


def _runtime_client(args: argparse.Namespace) -> RuntimeApiClient:
    env = _environment(args)
    base_url = env.get("III_RUNTIME_API_URL")
    if not base_url:
        host = "iii.local" if args.target == "real" else "localhost"
        port = env.get("III_RUNTIME_API_PORT", "8765")
        base_url = f"http://{host}:{port}"
    return RuntimeApiClient(
        base_url=base_url,
        cli_token=env.get("III_RUNTIME_API_CLI_TOKEN", "dev-cli-token"),
        timeout_seconds=float(env.get("III_RUNTIME_API_CLI_TIMEOUT_SEC", "15")),
    )


def _result(
    command: str,
    code: str,
    summary: str,
    payload: Mapping[str, Any],
    *,
    target: str | None = None,
) -> CommandResult:
    return CommandResult(
        command=command,
        outcome=Outcome.SUCCESS,
        summary=summary,
        code=code,
        target=target,
        profile=target,
        payload_schema=str(
            payload.get("schema", "iii.configuration-capture-result/v1")
        ),
        payload=payload,
        evidence=tuple(str(item) for item in payload.get("evidence", [])),
        terminal_reason="The requested configuration-capture operation completed without changing tracked or target defaults.",
    )


def _rejected(command: str, code: str, exc: Exception) -> CommandResult:
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="The configuration-capture operation was refused.",
        code=code,
        findings=(Finding(code, str(exc)),),
        terminal_reason="No unverified capture result is treated as complete.",
    )


def _names_and_descriptions(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    if not (len(args.snapshot) == len(args.name) == len(args.description)):
        raise ValueError(
            "repeat --snapshot, --name, and --description the same number of times in matching order"
        )
    selections = []
    for snapshot, name, description in zip(args.snapshot, args.name, args.description):
        if not snapshot or len(snapshot) > 255:
            raise ValueError("snapshot reference is invalid")
        if not SHORT_NAME.fullmatch(name):
            raise ValueError("short name must be 1-64 safe display characters")
        if not description.strip() or len(description) > 2000:
            raise ValueError("description must contain 1-2000 characters")
        selections.append((snapshot, name, description.strip()))
    if not selections:
        raise ValueError("at least one snapshot selection is required")
    return selections


def pull_preflight(args: argparse.Namespace) -> dict[str, Any]:
    selections = _names_and_descriptions(args)
    root = _capture_root(args)
    return {
        "schema": "iii.configuration-capture-pull-plan/v1",
        "target": args.target,
        "selections": [
            {"snapshot_id": item[0], "short_name": item[1], "description": item[2]}
            for item in selections
        ],
        "permissions": ["runtime-configuration-read", "local-capture-write"],
        "mutations": [str(root)],
    }


def pull(args: argparse.Namespace) -> CommandResult:
    command = "iii config capture pull"
    partial_path: Path | None = None
    try:
        selections = _names_and_descriptions(args)
        retained = args._iii_retained_plan["preflight"]
        expected = [
            {"snapshot_id": item[0], "short_name": item[1], "description": item[2]}
            for item in selections
        ]
        if (
            retained.get("target") != args.target
            or retained.get("selections") != expected
        ):
            raise ValueError("capture selections changed after planning")
        root = _capture_root(args, create=True)
        partial_id = uuid4().hex
        partial_path = root / ".partial" / f"{partial_id}.json"
        partial = {
            "schema": PARTIAL_SCHEMA,
            "partial_id": partial_id,
            "target": args.target,
            "started_at": _now(),
            "selections": expected,
            "completed_capture_ids": [],
            "status": "in-progress",
        }
        _atomic_document(partial_path, partial)
        client = _runtime_client(args)
        completed = []
        for snapshot_id, short_name, description in selections:
            source = client.configuration_capture_source(
                snapshot_id=snapshot_id, expected_profile=args.target
            )
            _reject_secrets(source)
            capture = seal_capture(source)
            capture_id = capture["capture_id"]
            destination = _capture_path(root, capture_id)
            _immutable_document(destination / "capture.json", capture)
            receipt = capture_receipt(capture)
            _immutable_document(destination / "receipt.json", receipt)
            metadata = {
                "schema": METADATA_SCHEMA,
                "metadata_id": "",
                "capture_id": capture_id,
                "short_name": short_name,
                "description": description,
                "captured_at": _now(),
                "source_locator": {
                    "target": args.target,
                    "snapshot_id": snapshot_id,
                },
            }
            metadata["metadata_id"] = content_identity(
                {key: item for key, item in metadata.items() if key != "metadata_id"}
            )
            _immutable_document(
                destination / "metadata" / f"{metadata['metadata_id']}.json",
                metadata,
            )
            completed.append(capture_id)
            partial["completed_capture_ids"] = list(completed)
            _atomic_document(partial_path, partial)
        partial_path.unlink()
        _fsync_directory(partial_path.parent)
        partial_path = None
        payload = {
            "schema": "iii.configuration-capture-pull-result/v1",
            "capture_ids": completed,
            "capture_root": str(root),
            "complete": True,
            "evidence": [
                str(_capture_path(root, item) / "capture.json") for item in completed
            ],
        }
    except Exception as exc:
        if partial_path is not None and partial_path.exists():
            try:
                value = _read_document(partial_path, label="partial capture")
                value["status"] = "interrupted"
                value["error_type"] = type(exc).__name__
                value["updated_at"] = _now()
                _atomic_document(partial_path, value)
            except Exception:
                pass
        return _rejected(command, "III_CONFIG_CAPTURE_PULL_REJECTED", exc)
    return _result(
        command,
        "III_CONFIG_CAPTURE_PULLED",
        f"Sealed {len(completed)} immutable configuration capture(s).",
        payload,
        target=args.target,
    )


def list_captures(args: argparse.Namespace) -> CommandResult:
    command = "iii config capture list"
    try:
        root = _capture_root(args)
        captures = []
        for path in sorted(root.iterdir()) if root.is_dir() else []:
            if (
                not path.is_dir()
                or path.is_symlink()
                or not SHA256.fullmatch(path.name)
            ):
                continue
            capture, _ = _verified(root, path.name)
            captures.append(
                {
                    "capture_id": path.name,
                    "source": capture["source"],
                    "metadata": _metadata(root, path.name),
                }
            )
        partials = []
        partial_root = root / ".partial"
        if partial_root.is_dir() and not partial_root.is_symlink():
            partials = [
                _read_document(path, label="partial capture")
                for path in sorted(partial_root.glob("*.json"))
            ]
        payload = {
            "schema": "iii.configuration-capture-list/v1",
            "captures": captures,
            "partials": partials,
            "display": "\n".join(
                f"{item['capture_id']}  "
                + ", ".join(meta["short_name"] for meta in item["metadata"])
                for item in captures
            )
            or "No completed captures.",
        }
    except Exception as exc:
        return _rejected(command, "III_CONFIG_CAPTURE_LIST_REJECTED", exc)
    return _result(
        command,
        "III_CONFIG_CAPTURE_LISTED",
        "Listed local configuration captures.",
        payload,
    )


def show(args: argparse.Namespace) -> CommandResult:
    command = "iii config capture show"
    try:
        root = _capture_root(args)
        capture, path = _verified(root, args.capture_id)
        payload = {
            "schema": "iii.configuration-capture-show/v1",
            "capture": capture,
            "metadata": _metadata(root, args.capture_id),
            "evidence": [str(path)],
            "display": json.dumps(capture, indent=2, sort_keys=True),
        }
    except Exception as exc:
        return _rejected(command, "III_CONFIG_CAPTURE_SHOW_REJECTED", exc)
    return _result(
        command,
        "III_CONFIG_CAPTURE_SHOWN",
        "Verified and displayed the configuration capture.",
        payload,
    )


def diff(args: argparse.Namespace) -> CommandResult:
    command = "iii config capture diff"
    try:
        root = _capture_root(args)
        capture, _ = _verified(root, args.capture_id)
        left = capture["source"]["values"]
        label = args.against
        if label == "baseline":
            right = capture["source"]["baseline_values"]
        else:
            other, _ = _verified(root, label)
            right = other["source"]["values"]
        names = sorted(set(left) | set(right))
        changes = [
            {"name": name, "capture": left.get(name), "against": right.get(name)}
            for name in names
            if left.get(name) != right.get(name) or (name in left) != (name in right)
        ]
        payload = {
            "schema": "iii.configuration-capture-diff/v1",
            "capture_id": args.capture_id,
            "against": label,
            "changes": changes,
            "display": "\n".join(
                f"{item['name']}: {item['against']!r} -> {item['capture']!r}"
                for item in changes
            )
            or "No value differences.",
        }
    except Exception as exc:
        return _rejected(command, "III_CONFIG_CAPTURE_DIFF_REJECTED", exc)
    return _result(
        command,
        "III_CONFIG_CAPTURE_DIFFED",
        "Compared verified configuration values.",
        payload,
    )


def verify(args: argparse.Namespace) -> CommandResult:
    command = "iii config capture verify"
    try:
        root = _capture_root(args)
        ids = args.capture_id or sorted(
            path.name
            for path in root.iterdir()
            if path.is_dir() and SHA256.fullmatch(path.name)
        )
        verified_ids = []
        for capture_id in ids:
            _verified(root, capture_id)
            _metadata(root, capture_id)
            receipt = _read_document(
                _capture_path(root, capture_id) / "receipt.json",
                label="configuration capture receipt",
            )
            capture, _ = _verified(root, capture_id)
            if receipt != capture_receipt(capture):
                raise ValueError("configuration capture receipt is invalid")
            verified_ids.append(capture_id)
        payload = {
            "schema": "iii.configuration-capture-verification/v1",
            "verified_capture_ids": verified_ids,
            "valid": True,
        }
    except Exception as exc:
        return _rejected(command, "III_CONFIG_CAPTURE_VERIFY_REJECTED", exc)
    return _result(
        command,
        "III_CONFIG_CAPTURE_VERIFIED",
        f"Verified {len(verified_ids)} capture(s).",
        payload,
    )


def _archive_members(root: Path, ids: Iterable[str]) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    for capture_id in sorted(set(ids)):
        capture, path = _verified(root, capture_id)
        del capture
        prefix = f"captures/{capture_id}"
        members[f"{prefix}/capture.json"] = path.read_bytes()
        receipt_path = path.parent / "receipt.json"
        members[f"{prefix}/receipt.json"] = receipt_path.read_bytes()
        for metadata in _metadata(root, capture_id):
            metadata_path = path.parent / "metadata" / f"{metadata['metadata_id']}.json"
            members[f"{prefix}/metadata/{metadata_path.name}"] = (
                metadata_path.read_bytes()
            )
    return members


def export_preflight(args: argparse.Namespace) -> dict[str, Any]:
    root = _capture_root(args)
    ids = args.capture_id or sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and SHA256.fullmatch(path.name)
    )
    if not ids:
        raise ValueError("no configuration captures were selected for export")
    members = _archive_members(root, ids)
    return {
        "schema": "iii.configuration-capture-export-plan/v1",
        "capture_ids": sorted(set(ids)),
        "member_hashes": {
            name: hashlib.sha256(raw).hexdigest() for name, raw in members.items()
        },
        "destination": str(args.archive.expanduser().absolute()),
        "permissions": ["local-capture-read", "local-archive-write"],
        "mutations": [str(args.archive.expanduser().absolute())],
    }


def _zip_bytes(manifest: Mapping[str, Any], members: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, raw in [
            ("manifest.json", canonical_json(manifest) + b"\n"),
            *sorted(members.items()),
        ]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100640 << 16
            archive.writestr(info, raw)
    return output.getvalue()


def export(args: argparse.Namespace) -> CommandResult:
    command = "iii config capture export"
    try:
        retained = args._iii_retained_plan["preflight"]
        root = _capture_root(args)
        members = _archive_members(root, retained["capture_ids"])
        hashes = {
            name: hashlib.sha256(raw).hexdigest() for name, raw in members.items()
        }
        if hashes != retained["member_hashes"]:
            raise ValueError("capture evidence changed after export planning")
        manifest = {
            "schema": ARCHIVE_SCHEMA,
            "archive_id": "",
            "captures": retained["capture_ids"],
            "members": hashes,
        }
        manifest["archive_id"] = content_identity(
            {key: item for key, item in manifest.items() if key != "archive_id"}
        )
        raw = _zip_bytes(manifest, members)
        destination = args.archive.expanduser().absolute()
        _publish_bytes_without_overwrite(destination, raw)
        payload = {
            "schema": "iii.configuration-capture-export-result/v1",
            "archive_id": manifest["archive_id"],
            "archive": str(destination),
            "archive_sha256": hashlib.sha256(raw).hexdigest(),
            "capture_ids": retained["capture_ids"],
            "evidence": [str(destination)],
        }
    except Exception as exc:
        return _rejected(command, "III_CONFIG_CAPTURE_EXPORT_REJECTED", exc)
    return _result(
        command,
        "III_CONFIG_CAPTURE_EXPORTED",
        "Exported a deterministic checksummed capture archive.",
        payload,
    )


def _read_archive(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    path = path.expanduser().absolute()
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > MAX_ARCHIVE_BYTES
    ):
        raise ValueError("capture archive is missing, linked, or oversized")
    with zipfile.ZipFile(path, "r") as archive:
        members: dict[str, bytes] = {}
        total = 0
        seen = set()
        for info in archive.infolist():
            member = PurePosixPath(info.filename)
            if (
                info.filename in seen
                or member.is_absolute()
                or ".." in member.parts
                or info.is_dir()
                or info.flag_bits & 0x1
            ):
                raise ValueError("capture archive contains an unsafe member")
            seen.add(info.filename)
            total += info.file_size
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("expanded capture archive exceeds the fixed limit")
            members[info.filename] = archive.read(info)
    raw_manifest = members.pop("manifest.json", None)
    if raw_manifest is None:
        raise ValueError("capture archive manifest is missing")
    try:
        manifest = json.loads(raw_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"capture archive manifest is invalid: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or raw_manifest != canonical_json(manifest) + b"\n"
    ):
        raise ValueError("capture archive manifest is not canonical")
    expected_id = content_identity(
        {key: item for key, item in manifest.items() if key != "archive_id"}
    )
    if (
        set(manifest) != {"schema", "archive_id", "captures", "members"}
        or manifest.get("schema") != ARCHIVE_SCHEMA
        or manifest.get("archive_id") != expected_id
        or not isinstance(manifest.get("captures"), list)
        or not isinstance(manifest.get("members"), dict)
        or not manifest["captures"]
        or any(
            not isinstance(capture_id, str) or not SHA256.fullmatch(capture_id)
            for capture_id in manifest["captures"]
        )
        or len(set(manifest["captures"])) != len(manifest["captures"])
        or any(
            not isinstance(digest, str) or not SHA256.fullmatch(digest)
            for digest in manifest["members"].values()
        )
        or set(members) != set(manifest["members"])
        or any(
            hashlib.sha256(raw).hexdigest() != manifest["members"][name]
            for name, raw in members.items()
        )
    ):
        raise ValueError("capture archive identity or member hashes are invalid")
    captures = set(manifest["captures"])
    for name in members:
        parts = PurePosixPath(name).parts
        if (
            len(parts) not in {3, 4}
            or parts[0] != "captures"
            or parts[1] not in captures
            or (len(parts) == 3 and parts[2] not in {"capture.json", "receipt.json"})
            or (
                len(parts) == 4
                and (
                    parts[2] != "metadata"
                    or not parts[3].endswith(".json")
                    or not SHA256.fullmatch(parts[3][:-5])
                )
            )
        ):
            raise ValueError("capture archive contains an undeclared member path")
    for capture_id in captures:
        required = {
            f"captures/{capture_id}/capture.json",
            f"captures/{capture_id}/receipt.json",
        }
        if not required.issubset(members):
            raise ValueError("capture archive is incomplete")
    return manifest, members


def import_preflight(args: argparse.Namespace) -> dict[str, Any]:
    manifest, _members = _read_archive(args.archive)
    return {
        "schema": "iii.configuration-capture-import-plan/v1",
        "archive_id": manifest["archive_id"],
        "archive_sha256": hashlib.sha256(
            args.archive.expanduser().absolute().read_bytes()
        ).hexdigest(),
        "capture_ids": manifest["captures"],
        "permissions": ["local-archive-read", "local-capture-write"],
        "mutations": [str(_capture_root(args))],
    }


def import_archive(args: argparse.Namespace) -> CommandResult:
    command = "iii config capture import"
    partial_path: Path | None = None
    try:
        retained = args._iii_retained_plan["preflight"]
        path = args.archive.expanduser().absolute()
        if hashlib.sha256(path.read_bytes()).hexdigest() != retained["archive_sha256"]:
            raise ValueError("capture archive changed after import planning")
        manifest, members = _read_archive(path)
        if manifest["archive_id"] != retained["archive_id"]:
            raise ValueError("capture archive identity changed after planning")
        root = _capture_root(args, create=True)
        validated = []
        for capture_id in manifest["captures"]:
            if not isinstance(capture_id, str) or not SHA256.fullmatch(capture_id):
                raise ValueError("capture archive contains a malformed capture ID")
            prefix = f"captures/{capture_id}/"
            capture_raw = members.get(prefix + "capture.json")
            receipt_raw = members.get(prefix + "receipt.json")
            if capture_raw is None or receipt_raw is None:
                raise ValueError("capture archive is incomplete")
            capture = json.loads(capture_raw)
            verified = verify_capture(capture)
            if (
                verified["capture_id"] != capture_id
                or capture_raw != canonical_json(capture) + b"\n"
            ):
                raise ValueError("archived capture content identity is invalid")
            _reject_secrets(verified["source"])
            receipt = json.loads(receipt_raw)
            if (
                receipt != capture_receipt(capture)
                or receipt_raw != canonical_json(receipt) + b"\n"
            ):
                raise ValueError("archived capture receipt is invalid")
            metadata_documents = []
            metadata_prefix = prefix + "metadata/"
            for name, raw in sorted(members.items()):
                if not name.startswith(metadata_prefix):
                    continue
                metadata = json.loads(raw)
                metadata_id = metadata.get("metadata_id")
                expected_metadata_id = content_identity(
                    {
                        key: item
                        for key, item in metadata.items()
                        if key != "metadata_id"
                    }
                )
                if (
                    set(metadata)
                    != {
                        "schema",
                        "metadata_id",
                        "capture_id",
                        "short_name",
                        "description",
                        "captured_at",
                        "source_locator",
                    }
                    or metadata.get("schema") != METADATA_SCHEMA
                    or metadata.get("capture_id") != capture_id
                    or metadata_id != expected_metadata_id
                    or name != metadata_prefix + metadata_id + ".json"
                    or raw != canonical_json(metadata) + b"\n"
                ):
                    raise ValueError("archived capture metadata is invalid")
                metadata_documents.append(metadata)
            validated.append((capture_id, capture, receipt, metadata_documents))

        partial_path = root / ".partial" / f"import-{manifest['archive_id']}.json"
        partial = {
            "schema": PARTIAL_SCHEMA,
            "partial_id": f"import-{manifest['archive_id']}",
            "operation": "import",
            "archive_id": manifest["archive_id"],
            "started_at": _now(),
            "capture_ids": list(manifest["captures"]),
            "completed_capture_ids": [],
            "status": "in-progress",
        }
        _atomic_document(partial_path, partial)
        imported = []
        deduplicated = []
        for capture_id, capture, receipt, metadata_documents in validated:
            destination = _capture_path(root, capture_id)
            if (destination / "capture.json").is_file():
                deduplicated.append(capture_id)
            _immutable_document(destination / "capture.json", capture)
            _immutable_document(destination / "receipt.json", receipt)
            for metadata in metadata_documents:
                metadata_id = metadata["metadata_id"]
                _immutable_document(
                    destination / "metadata" / f"{metadata_id}.json", metadata
                )
            _metadata(root, capture_id)
            imported.append(capture_id)
            partial["completed_capture_ids"] = list(imported)
            _atomic_document(partial_path, partial)
        partial_path.unlink()
        _fsync_directory(partial_path.parent)
        partial_path = None
        payload = {
            "schema": "iii.configuration-capture-import-result/v1",
            "archive_id": manifest["archive_id"],
            "capture_ids": imported,
            "deduplicated_capture_ids": deduplicated,
            "deduplicated": len(deduplicated) == len(imported),
        }
    except Exception as exc:
        if partial_path is not None and partial_path.exists():
            try:
                value = _read_document(partial_path, label="partial capture import")
                value["status"] = "interrupted"
                value["error_type"] = type(exc).__name__
                value["updated_at"] = _now()
                _atomic_document(partial_path, value)
            except Exception:
                pass
        return _rejected(command, "III_CONFIG_CAPTURE_IMPORT_REJECTED", exc)
    return _result(
        command,
        "III_CONFIG_CAPTURE_IMPORTED",
        f"Imported and verified {len(imported)} capture(s).",
        payload,
    )


def delete_preflight(args: argparse.Namespace) -> dict[str, Any]:
    root = _capture_root(args)
    request: dict[str, Any] = {
        "schema": "iii.configuration-snapshot-delete-request/v1",
        "snapshot_id": args.snapshot,
        "force": bool(args.force),
        "confirmation": args.confirm_snapshot,
        "capture_receipt": None,
    }
    if args.force:
        if args.capture_id:
            raise ValueError("forced deletion does not accept a capture receipt")
        if args.confirm_snapshot != f"delete:{args.snapshot}":
            raise ValueError(
                "forced deletion requires --confirm-snapshot delete:<exact-snapshot-id>"
            )
    else:
        if not args.capture_id:
            raise ValueError("normal deletion requires --capture-id")
        capture, _ = _verified(root, args.capture_id)
        if capture["source"]["snapshot_id"] != args.snapshot:
            raise ValueError("capture receipt names another target snapshot")
        request["capture_receipt"] = capture_receipt(capture)
    return {
        "schema": "iii.configuration-snapshot-delete-plan/v1",
        "target": args.target,
        "request": request,
        "permissions": ["runtime-configuration-write"],
        "mutations": [f"{args.target}:{args.snapshot}"],
    }


def delete(args: argparse.Namespace) -> CommandResult:
    command = "iii config capture delete"
    try:
        retained = args._iii_retained_plan["preflight"]
        if retained.get("target") != args.target:
            raise ValueError("delete target changed after planning")
        result = _runtime_client(args).delete_configuration_snapshot(
            retained["request"]
        )
        if result.get("deleted") is not True:
            raise ValueError("runtime did not authenticate snapshot deletion")
        payload = {
            "schema": "iii.configuration-snapshot-delete-cli-result/v1",
            "result": result,
        }
    except Exception as exc:
        return _rejected(command, "III_CONFIG_CAPTURE_DELETE_REJECTED", exc)
    return _result(
        command,
        "III_CONFIG_CAPTURE_SNAPSHOT_DELETED",
        "Deleted the authenticated inactive target snapshot.",
        payload,
        target=args.target,
    )


def _common_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--capture-root", type=Path)


def initialize(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="config_capture_command", required=True)
    pull_parser = commands.add_parser(
        "pull", help="seal one or more target snapshots locally"
    )
    pull_parser.add_argument("--target", choices=("real", "sim"), required=True)
    pull_parser.add_argument(
        "--snapshot",
        action="append",
        required=True,
        help="saved target snapshot ID/path (repeat with --name and --description)",
    )
    pull_parser.add_argument(
        "--name",
        action="append",
        required=True,
        help="short display name paired with each --snapshot",
    )
    pull_parser.add_argument(
        "--description",
        action="append",
        required=True,
        help="purpose paired with each --snapshot",
    )
    _common_root(pull_parser)
    pull_parser.set_defaults(
        func=pull, _iii_mutating=True, _iii_plan_provider=pull_preflight
    )

    list_parser = commands.add_parser(
        "list", help="list complete and interrupted local captures"
    )
    _common_root(list_parser)
    list_parser.set_defaults(func=list_captures, _iii_mutating=False)

    show_parser = commands.add_parser("show", help="verify and show one local capture")
    show_parser.add_argument("capture_id")
    _common_root(show_parser)
    show_parser.set_defaults(func=show, _iii_mutating=False)

    diff_parser = commands.add_parser(
        "diff", help="compare a capture with its baseline or another capture"
    )
    diff_parser.add_argument("capture_id")
    diff_parser.add_argument("--against", default="baseline")
    _common_root(diff_parser)
    diff_parser.set_defaults(func=diff, _iii_mutating=False)

    verify_parser = commands.add_parser(
        "verify", help="verify selected or all local captures"
    )
    verify_parser.add_argument("capture_id", nargs="*")
    _common_root(verify_parser)
    verify_parser.set_defaults(func=verify, _iii_mutating=False)

    export_parser = commands.add_parser(
        "export", help="write a deterministic portable capture archive"
    )
    export_parser.add_argument("--capture-id", action="append", default=[])
    export_parser.add_argument("--archive", type=Path, required=True)
    _common_root(export_parser)
    export_parser.set_defaults(
        func=export, _iii_mutating=True, _iii_plan_provider=export_preflight
    )

    import_parser = commands.add_parser(
        "import", help="verify and import a portable capture archive"
    )
    import_parser.add_argument("archive", type=Path)
    _common_root(import_parser)
    import_parser.set_defaults(
        func=import_archive, _iii_mutating=True, _iii_plan_provider=import_preflight
    )

    delete_parser = commands.add_parser(
        "delete", help="delete an inactive target snapshot after capture"
    )
    delete_parser.add_argument("--target", choices=("real", "sim"), required=True)
    delete_parser.add_argument("--snapshot", required=True)
    delete_parser.add_argument("--capture-id")
    delete_parser.add_argument("--force", action="store_true")
    delete_parser.add_argument("--confirm-snapshot")
    _common_root(delete_parser)
    delete_parser.set_defaults(
        func=delete, _iii_mutating=True, _iii_plan_provider=delete_preflight
    )
