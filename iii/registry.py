"""User-owned local record registry, deterministic archives, and blob integrity."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
from typing import Any, BinaryIO, Iterable, Mapping, Sequence
from uuid import uuid4


HASH = re.compile(r"^[a-f0-9]{64}$")
DOMAIN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
MAX_JSON_BYTES = 64 * 1024**2
MAX_ARCHIVE_BYTES = 100 * 1024**3
MAX_ARCHIVE_MEMBERS = 100_000
ARCHIVE_MANIFEST = "archive-manifest.json"
ARCHIVE_SCHEMA = "iii.record-archive-manifest/v1"
INDEX_SCHEMA = "iii.record-index/v1"
RECORD_SCHEMA = "iii.local-record/v1"

DOMAIN_ROOTS: tuple[tuple[str, PurePosixPath], ...] = (
    ("operations", PurePosixPath("operations")),
    ("release-cache", PurePosixPath("cache/releases")),
    ("gc-release-cache", PurePosixPath("cache/gc/releases")),
    ("captures", PurePosixPath("captures")),
    ("backups", PurePosixPath("backups")),
    ("commissioning", PurePosixPath("commissioning")),
    ("readiness", PurePosixPath("readiness")),
    ("release-evidence", PurePosixPath("release-evidence")),
    ("status-indexes", PurePosixPath("status-indexes")),
    ("log-pulls", PurePosixPath("log-pulls")),
    ("diagnostic-pulls", PurePosixPath("diagnostic-pulls")),
    ("archive-receipts", PurePosixPath("archive-receipts")),
    ("import-receipts", PurePosixPath("import-receipts")),
)
DOMAIN_BY_ROOT = {path.parts: domain for domain, path in DOMAIN_ROOTS}
ARCHIVABLE_DOMAINS = tuple(
    domain
    for domain, _path in DOMAIN_ROOTS
    if domain not in {"archive-receipts", "import-receipts"}
)
IRREPLACEABLE_DOMAINS = frozenset(
    {
        "captures",
        "backups",
        "commissioning",
        "readiness",
        "release-evidence",
        "status-indexes",
        "log-pulls",
        "diagnostic-pulls",
    }
)
DEFAULT_PROTECTION = {
    "backups": ("restore-evidence",),
    "commissioning": ("commissioning-evidence",),
    "release-evidence": ("promotion-evidence", "retained-release-evidence"),
}
SECRET_PATH_PARTS = frozenset(
    {
        ".ssh",
        "credentials",
        "id_ed25519",
        "id_rsa",
        "machine-id",
        "private-key",
        "private_key",
        "secrets",
        "wifi",
    }
)
SECRET_KEYS = frozenset(
    {
        "api_key",
        "credential",
        "credentials",
        "machine_id",
        "password",
        "passphrase",
        "private_key",
        "private_key_path",
        "psk",
        "runtime_api_token",
        "secret",
        "signing_key",
        "ssh_private_key",
        "token",
        "wifi_password",
        "wifi_psk",
    }
)
PRIVATE_MARKERS = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
)
SECRET_ARGUMENTS = frozenset(
    {
        "--api-key",
        "--credential",
        "--password",
        "--passphrase",
        "--private-key",
        "--psk",
        "--runtime-api-token",
        "--signing-key",
        "--token",
        "--wifi-password",
        "--wifi-psk",
    }
)
SECRET_ASSIGNMENT = re.compile(
    rb"(?im)(?:^|\n)\s*(?:api_key|credential|password|passphrase|private_key|psk|runtime_api_token|secret|token|wifi_password|wifi_psk)\s*[=:]\s*([^\r\n#]+)"
)
BEARER_ASSIGNMENT = re.compile(rb"(?im)(?:authorization\s*:\s*)?bearer\s+([^\s]+)")
SECRET_EXCLUSIONS = (
    "machine-identity",
    "runtime-api-credentials",
    "signing-private-keys",
    "ssh-private-keys",
    "unredacted-secret-inputs",
    "wifi-secrets",
)


class RegistryError(RuntimeError):
    code = "III_RECORD_REGISTRY_ERROR"


class RegistryConflict(RegistryError):
    code = "III_RECORD_REGISTRY_CONFLICT"


class RegistrySecretError(RegistryError):
    code = "III_RECORD_SECRET_REJECTED"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def content_id(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def registry_root(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    explicit = env.get("III_REGISTRY_ROOT")
    if explicit:
        return Path(explicit).expanduser().absolute()
    workspace = env.get("WORKSPACE_DIR")
    if workspace:
        return (Path(workspace).expanduser().absolute() / ".iii").absolute()
    state = Path(
        env.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
    ).expanduser()
    return (state / "iii").absolute()


def _safe_locator(value: str) -> PurePosixPath:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise RegistryError("record locator contains a forbidden character")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RegistryError("record locator is not a safe relative path")
    return path


def _inside_git_worktree(path: Path) -> tuple[Path, Path] | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            try:
                relative = path.relative_to(candidate)
            except ValueError:
                return None
            return candidate, relative
    return None


def ensure_registry_root(root: Path, *, create: bool) -> Path:
    root = root.expanduser().absolute()
    inside = _inside_git_worktree(root)
    if inside is not None and (not inside[1].parts or inside[1].parts[0] != ".iii"):
        raise RegistryError("registry paths inside Git worktrees must use .iii/")
    if not root.exists() and not root.is_symlink():
        if not create:
            return root
        _mkdir_absolute(root, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise RegistryError("local registry root is linked or not a directory")
    metadata = root.stat(follow_symlinks=False)
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise RegistryError("local registry root is not owned by the current user")
    if create:
        os.chmod(root, 0o700)
    return root


def _mkdir_absolute(path: Path, *, mode: int) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise RegistryError("registry directory path is unsafe")
        else:
            current.mkdir(mode=mode)


def _mkdir_relative(root: Path, relative: PurePosixPath, *, mode: int) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise RegistryError("registry directory contains an unsafe parent")
        else:
            current.mkdir(mode=mode)
    return current


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def registry_lock(root: Path):
    root = ensure_registry_root(root, create=True)
    lock = root / ".registry.lock"
    if lock.is_symlink():
        raise RegistryError("local registry lock is a symbolic link")
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise RegistryError("local registry lock is not user-owned")
        if metadata.st_mode & 0o077:
            os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        recover_staging(root)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def recover_staging(root: Path) -> list[str]:
    recovered: list[str] = []
    bases = [
        root / "blobs",
        root / "indexes",
        root / "archive-receipts",
        root / "import-receipts",
        *(root.joinpath(*relative.parts) for _domain, relative in DOMAIN_ROOTS),
    ]
    for base in bases:
        if not base.exists() and not base.is_symlink():
            continue
        if base.is_symlink() or not base.is_dir():
            raise RegistryError("registry staging root is unsafe")
        for path in sorted(
            base.rglob(".*.partial-*"), key=lambda item: item.as_posix()
        ):
            if path.is_symlink() or not path.is_file():
                raise RegistryError("registry staging entry is unsafe")
            path.unlink()
            recovered.append(path.relative_to(root).as_posix())
    operations = root / "operations"
    if operations.exists() and not operations.is_symlink():
        for path in sorted(
            operations.glob("*/.*.tmp"), key=lambda item: item.as_posix()
        ):
            if not re.fullmatch(
                r"\.[a-z][a-z0-9-]{0,63}\.json\.[0-9]+\.[a-f0-9]{32}\.tmp",
                path.name,
            ):
                continue
            if path.is_symlink() or not path.is_file():
                raise RegistryError("operation staging entry is unsafe")
            path.unlink()
            recovered.append(path.relative_to(root).as_posix())
    return recovered


def atomic_json(root: Path, locator: str, value: Mapping[str, Any]) -> Path:
    relative = _safe_locator(locator)
    path = root.joinpath(*relative.parts)
    _mkdir_relative(root, PurePosixPath(*relative.parts[:-1]), mode=0o700)
    if path.is_symlink():
        raise RegistryError("registry metadata target is a symbolic link")
    temporary = path.parent / f".{path.name}.partial-{os.getpid()}-{uuid4().hex}"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        view = memoryview(canonical_json(dict(value)) + b"\n")
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("registry metadata write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        return path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def read_canonical_json(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RegistryError(f"{label} is missing or linked")
    raw = path.read_bytes()
    if len(raw) > MAX_JSON_BYTES:
        raise RegistryError(f"{label} exceeds the JSON size limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise RegistryError(f"{label} is not canonical JSON")
    return value


def file_identity(path: Path) -> tuple[str, int]:
    if path.is_symlink():
        raise RegistryError("registry content is a symbolic link")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    size = 0
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RegistryError("registry content is not a regular file")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def _record_files(unit: Path, root: Path) -> list[Path]:
    if unit.is_symlink():
        raise RegistryError("registry record unit is a symbolic link")
    if unit.is_file():
        return [unit]
    if not unit.is_dir():
        raise RegistryError("registry record unit is a special file")
    files: list[Path] = []
    for path in sorted(unit.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise RegistryError("registry record contains a symbolic link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise RegistryError("registry record contains a special file")
        if not path.is_relative_to(root):
            raise RegistryError("registry record escapes its root")
        files.append(path)
    return files


def _json_payload(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if path.suffix != ".json":
        return None, None
    if path.stat(follow_symlinks=False).st_size > MAX_JSON_BYTES:
        return None, f"json-too-large:{path.name}"
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"invalid-json:{path.name}:{type(exc).__name__}"
    if not isinstance(value, dict):
        return None, f"invalid-json-object:{path.name}"
    return value, None


def _walk_values(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)


def _references(payloads: Sequence[Mapping[str, Any]]) -> list[str]:
    values = {
        item
        for payload in payloads
        for item in _walk_values(payload)
        if isinstance(item, str) and HASH.fullmatch(item)
    }
    return sorted(values)


def _target(payloads: Sequence[Mapping[str, Any]]) -> dict[str, str] | None:
    for payload in payloads:
        for value in _walk_values(payload):
            if not isinstance(value, Mapping):
                continue
            logical = value.get("logical_id")
            profile = value.get("profile")
            if (
                isinstance(logical, str)
                and logical
                and isinstance(profile, str)
                and profile
            ):
                return {"logical_id": logical, "profile": profile}
    return None


def _creation_source(
    domain: str, payloads: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    for payload in payloads:
        for key in ("creation_source", "command", "source"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return {"kind": "record-payload", "value": value[:512]}
    return {"kind": "registry-domain", "value": domain}


def _protection(domain: str, payloads: Sequence[Mapping[str, Any]]) -> list[str]:
    reasons = set(DEFAULT_PROTECTION.get(domain, ()))
    for payload in payloads:
        if payload.get("protected") is True:
            reasons.add("declared-protected")
        if payload.get("references"):
            reasons.add("cross-domain-reference")
        review = payload.get("review_status")
        if review not in {None, "resolved", "acknowledged"}:
            reasons.add("unresolved-review")
        if payload.get("acknowledged") is False:
            reasons.add("unacknowledged-failure")
        state = payload.get("state")
        if state in {"failed", "interrupted", "running", "planned"}:
            reasons.add(f"operation-{state}")
        schema = str(payload.get("schema", ""))
        if "qualified" in schema or "promotion" in schema:
            reasons.add("qualified-or-promotion-evidence")
        if domain in {"release-cache", "gc-release-cache"} and payload.get(
            "status"
        ) in {"qualified", "safe", "active", "retained"}:
            reasons.add("retained-release")
    return sorted(reasons)


def _record_descriptor(domain: str, unit: Path, root: Path) -> dict[str, Any]:
    files = _record_files(unit, root)
    directories = (
        sorted(
            path.relative_to(root).as_posix()
            for path in unit.rglob("*")
            if path.is_dir()
        )
        if unit.is_dir()
        else []
    )
    file_rows: list[dict[str, Any]] = []
    payloads: list[Mapping[str, Any]] = []
    issues: list[str] = []
    for path in files:
        digest, size = file_identity(path)
        locator = path.relative_to(root).as_posix()
        file_rows.append(
            {
                "locator": locator,
                "content_id": digest,
                "size": size,
            }
        )
        payload, issue = _json_payload(path)
        if payload is not None:
            payloads.append(payload)
        if issue is not None:
            issues.append(issue)
    unsigned = {
        "schema": RECORD_SCHEMA,
        "domain": domain,
        "locator": unit.relative_to(root).as_posix(),
        "unit_kind": "file" if unit.is_file() else "directory",
        "directories": directories,
        "files": file_rows,
        "creation_source": _creation_source(domain, payloads),
        "target": _target(payloads),
        "references": _references(payloads),
        "irreplaceable": domain in IRREPLACEABLE_DOMAINS,
    }
    return {
        **unsigned,
        "record_id": content_id(unsigned),
        "protection": _protection(domain, payloads),
        "integrity": {
            "state": "verified" if not issues else "corrupt",
            "issues": sorted(issues),
        },
    }


def _domain_units(root: Path, relative: PurePosixPath) -> tuple[list[Path], list[str]]:
    path = root.joinpath(*relative.parts)
    if not path.exists() and not path.is_symlink():
        return [], []
    if path.is_symlink() or not path.is_dir():
        raise RegistryError(f"registry domain {relative.as_posix()} is unsafe")
    units: list[Path] = []
    omitted: list[str] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        locator = child.relative_to(root).as_posix()
        if child.name.startswith("."):
            if (
                relative == PurePosixPath("captures")
                and child.name == ".partial"
                and not child.is_symlink()
                and child.is_dir()
            ):
                for partial in sorted(child.iterdir(), key=lambda item: item.name):
                    if (
                        partial.is_symlink()
                        or not partial.is_file()
                        or not re.fullmatch(
                            r"(?:[a-f0-9]{32}|import-[a-f0-9]{64})\.json",
                            partial.name,
                        )
                    ):
                        raise RegistryError(
                            "configuration capture staging content is unsafe"
                        )
                    units.append(partial)
                continue
            if child.is_symlink() or not child.is_file():
                raise RegistryError("hidden registry staging content is unsafe")
            if (
                relative == PurePosixPath("operations")
                and child.name == ".registry.lock"
            ):
                continue
            omitted.append(locator)
            continue
        if child.is_symlink() or not (child.is_file() or child.is_dir()):
            raise RegistryError("registry domain contains an unsafe entry")
        units.append(child)
    return units, omitted


def _blob_rows(root: Path) -> list[dict[str, Any]]:
    blob_root = root / "blobs/sha256"
    if not blob_root.exists() and not blob_root.is_symlink():
        return []
    if blob_root.is_symlink() or not blob_root.is_dir():
        raise RegistryError("registry blob root is unsafe")
    rows = []
    for path in sorted(blob_root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise RegistryError("registry blob store contains a link")
        if path.is_dir():
            continue
        if not path.is_file() or not HASH.fullmatch(path.name):
            raise RegistryError("registry blob store contains an invalid entry")
        digest, size = file_identity(path)
        rows.append(
            {
                "content_id": path.name,
                "size": size,
                "integrity": "verified" if digest == path.name else "corrupt",
            }
        )
    return rows


def build_inventory(
    root: Path,
    *,
    domains: Sequence[str] | None = None,
    exclude_record_locators: Sequence[str] = (),
) -> dict[str, Any]:
    root = ensure_registry_root(root, create=False)
    selected = set(domains or [domain for domain, _path in DOMAIN_ROOTS])
    unknown = sorted(selected - {domain for domain, _path in DOMAIN_ROOTS})
    if unknown:
        raise RegistryError("unknown registry domain(s): " + ", ".join(unknown))
    records: list[dict[str, Any]] = []
    excluded = {
        _safe_locator(locator).as_posix() for locator in exclude_record_locators
    }
    omitted: list[dict[str, str]] = []
    if root.exists():
        for domain, relative in DOMAIN_ROOTS:
            if domain not in selected:
                continue
            units, domain_omitted = _domain_units(root, relative)
            records.extend(
                _record_descriptor(domain, unit, root)
                for unit in units
                if unit.relative_to(root).as_posix() not in excluded
            )
            omitted.extend(
                {"locator": locator, "reason": "incomplete-staging"}
                for locator in domain_omitted
            )
    records.sort(key=lambda item: (item["domain"], item["locator"]))
    blobs = _blob_rows(root) if root.exists() else []
    unsigned = {
        "schema": INDEX_SCHEMA,
        "records": records,
        "blobs": blobs,
        "omitted": sorted(omitted, key=lambda item: item["locator"]),
    }
    return {**unsigned, "index_id": content_id(unsigned)}


def write_index(root: Path, inventory: Mapping[str, Any]) -> Path:
    expected = content_id(
        {key: item for key, item in inventory.items() if key != "index_id"}
    )
    if inventory.get("schema") != INDEX_SCHEMA or inventory.get("index_id") != expected:
        raise RegistryError("record index identity is invalid")
    return atomic_json(root, "indexes/records.json", inventory)


def read_index(root: Path) -> dict[str, Any] | None:
    path = root / "indexes/records.json"
    if not path.exists() and not path.is_symlink():
        return None
    value = read_canonical_json(path, label="local record index")
    expected = content_id(
        {key: item for key, item in value.items() if key != "index_id"}
    )
    if value.get("schema") != INDEX_SCHEMA or value.get("index_id") != expected:
        raise RegistryConflict("local record index identity mismatch")
    return value


def build_reindex_plan(
    root: Path, *, exclude_record_locators: Sequence[str] = ()
) -> dict[str, Any]:
    root = ensure_registry_root(root, create=False)
    excluded = sorted(set(exclude_record_locators))
    inventory = build_inventory(root, exclude_record_locators=excluded)
    unsigned: dict[str, Any] = {
        "schema": "iii.record-reindex-plan/v1",
        "registry_index": inventory,
        "excluded_record_locators": excluded,
    }
    return {**unsigned, "plan_id": content_id(unsigned)}


def apply_reindex_plan(root: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    expected = content_id({key: item for key, item in plan.items() if key != "plan_id"})
    if (
        plan.get("schema") != "iii.record-reindex-plan/v1"
        or plan.get("plan_id") != expected
    ):
        raise RegistryConflict("retained reindex plan identity mismatch")
    root = ensure_registry_root(root, create=True)
    with registry_lock(root):
        current = build_inventory(
            root,
            exclude_record_locators=plan.get("excluded_record_locators", []),
        )
        if current != plan.get("registry_index"):
            raise RegistryConflict("registry changed after reindex planning")
        path = write_index(root, current)
        return {
            "schema": "iii.record-reindex-result/v1",
            "registry_index_id": current["index_id"],
            "index_path": str(path),
            "record_count": len(current["records"]),
            "blob_count": len(current["blobs"]),
        }


def blob_path(root: Path, identity: str) -> Path:
    if not HASH.fullmatch(identity):
        raise RegistryError("invalid registry blob identity")
    return root / "blobs/sha256" / identity[:2] / identity


def _copy_stream_to_blob(
    root: Path,
    stream: BinaryIO,
    *,
    identity: str,
    size: int,
) -> Path:
    destination = blob_path(root, identity)
    _mkdir_relative(
        root,
        PurePosixPath("blobs", "sha256", identity[:2]),
        mode=0o700,
    )
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink():
            raise RegistryConflict("registry blob destination is linked")
        observed, observed_size = file_identity(destination)
        if observed != identity or observed_size != size:
            raise RegistryConflict("registry blob identity collision")
        return destination
    temporary = destination.parent / (
        f".{identity}.partial-{os.getpid()}-{uuid4().hex}"
    )
    descriptor = -1
    digest = hashlib.sha256()
    written_size = 0
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o440,
        )
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            written_size += len(block)
            view = memoryview(block)
            while view:
                count = os.write(descriptor, view)
                if count <= 0:
                    raise OSError("registry blob write made no progress")
                view = view[count:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if digest.hexdigest() != identity or written_size != size:
            raise RegistryConflict("registry blob content differs from its identity")
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
        return destination
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def materialize_blob(root: Path, source: Path, *, identity: str, size: int) -> Path:
    if source.is_symlink() or not source.is_file():
        raise RegistryConflict("registry blob source is missing or linked")
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return _copy_stream_to_blob(root, stream, identity=identity, size=size)
    finally:
        os.close(descriptor)


def _secret_value(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str) and value in {
        "",
        "redacted",
        "<redacted>",
        "REDACTED",
    }:
        return False
    return True


def _secret_json_issues(value: Any, *, path: str = "$") -> list[str]:
    issues: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            child = f"{path}.{key}"
            if normalized in SECRET_KEYS and _secret_value(item):
                issues.append(f"secret-field:{child}")
            issues.extend(_secret_json_issues(item, path=child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if (
                isinstance(item, str)
                and item.lower() in SECRET_ARGUMENTS
                and index + 1 < len(value)
                and _secret_value(value[index + 1])
            ):
                issues.append(f"secret-argument:{path}[{index + 1}]")
            issues.extend(_secret_json_issues(item, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        lowered = value.lower()
        if any(
            token in lowered
            for token in (
                "/.ssh/id_",
                "/etc/machine-id",
                "/etc/networkmanager/system-connections",
                "/var/lib/dbus/machine-id",
                "private-key",
                "private_key",
                "id_ed25519",
                "id_rsa",
                "runtime-api.env",
            )
        ):
            issues.append(f"secret-reference:{path}")
        if any(lowered.startswith(argument + "=") for argument in SECRET_ARGUMENTS):
            issues.append(f"secret-argument:{path}")
    return issues


def _stream_secret_issues(stream: BinaryIO, *, locators: Sequence[str]) -> list[str]:
    issues: list[str] = []
    raw = bytearray()
    tail = b""
    total = 0
    private_key = False
    secret_assignment = False
    while True:
        block = stream.read(1024 * 1024)
        if not block:
            break
        total += len(block)
        combined = tail + block
        if any(marker in combined for marker in PRIVATE_MARKERS):
            private_key = True
        assignments = [
            match.group(1).strip()
            for expression in (SECRET_ASSIGNMENT, BEARER_ASSIGNMENT)
            for match in expression.finditer(combined)
        ]
        if any(
            value.lower() not in {b"", b"redacted", b"<redacted>"}
            for value in assignments
        ):
            secret_assignment = True
        if total <= MAX_JSON_BYTES:
            raw.extend(block)
        tail = combined[-128:]
    for locator in locators:
        relative = _safe_locator(locator)
        if SECRET_PATH_PARTS.intersection(part.lower() for part in relative.parts):
            issues.append(f"secret-path:{locator}")
        if private_key:
            issues.append(f"private-key-content:{locator}")
        if secret_assignment:
            issues.append(f"secret-assignment:{locator}")
        if relative.suffix == ".json" and total <= MAX_JSON_BYTES:
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            issues.extend(_secret_json_issues(value, path=locator))
    return sorted(set(issues))


def secret_issues(path: Path, locator: str) -> list[str]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return _stream_secret_issues(stream, locators=[locator])
    finally:
        os.close(descriptor)


def secret_locator_issues(locators: Iterable[str]) -> list[str]:
    return sorted(
        {
            f"secret-path:{locator}"
            for locator in locators
            if SECRET_PATH_PARTS.intersection(
                part.lower() for part in _safe_locator(locator).parts
            )
        }
    )


def _validate_record(record: Mapping[str, Any]) -> None:
    required = {
        "schema",
        "record_id",
        "domain",
        "locator",
        "unit_kind",
        "directories",
        "files",
        "creation_source",
        "target",
        "references",
        "irreplaceable",
        "protection",
        "integrity",
    }
    if set(record) != required or record.get("schema") != RECORD_SCHEMA:
        raise RegistryError("archive record descriptor fields are invalid")
    if not isinstance(record.get("domain"), str) or not DOMAIN.fullmatch(
        record["domain"]
    ):
        raise RegistryError("archive record domain is invalid")
    unit = _safe_locator(str(record.get("locator", "")))
    expected_root = dict(DOMAIN_ROOTS).get(str(record["domain"]))
    interrupted_capture = (
        record["domain"] == "captures"
        and expected_root == PurePosixPath("captures")
        and len(unit.parts) == 3
        and unit.parts[:2] == ("captures", ".partial")
        and re.fullmatch(
            r"(?:[a-f0-9]{32}|import-[a-f0-9]{64})\.json", unit.name
        )
    )
    if not interrupted_capture and (
        expected_root is None
        or len(unit.parts) != len(expected_root.parts) + 1
        or unit.parts[: len(expected_root.parts)] != expected_root.parts
        or unit.name.startswith(".")
    ):
        raise RegistryError("archive record locator is outside its domain")
    if record.get("unit_kind") not in {"file", "directory"}:
        raise RegistryError("archive record unit kind is invalid")
    directories = record.get("directories")
    if (
        not isinstance(directories, list)
        or any(
            not isinstance(locator, str)
            or not _safe_locator(locator).is_relative_to(unit)
            or _safe_locator(locator) == unit
            for locator in directories
        )
        or directories != sorted(set(directories))
    ):
        raise RegistryError("archive record directory inventory is invalid")
    if record["unit_kind"] == "file" and directories:
        raise RegistryError("archive file record contains directories")
    unsigned = {
        key: item
        for key, item in record.items()
        if key not in {"record_id", "protection", "integrity"}
    }
    if record.get("record_id") != content_id(unsigned):
        raise RegistryConflict("archive record identity mismatch")
    creation_source = record.get("creation_source")
    if (
        not isinstance(creation_source, Mapping)
        or set(creation_source) != {"kind", "value"}
        or not all(
            isinstance(creation_source[key], str) and creation_source[key]
            for key in creation_source
        )
    ):
        raise RegistryError("archive record creation source is invalid")
    target = record.get("target")
    if target is not None and (
        not isinstance(target, Mapping)
        or set(target) != {"logical_id", "profile"}
        or not all(isinstance(target[key], str) and target[key] for key in target)
    ):
        raise RegistryError("archive record target is invalid")
    references = record.get("references")
    if (
        not isinstance(references, list)
        or any(
            not isinstance(item, str) or not HASH.fullmatch(item) for item in references
        )
        or references != sorted(set(references))
    ):
        raise RegistryError("archive record references are invalid")
    protection = record.get("protection")
    if (
        not isinstance(record.get("irreplaceable"), bool)
        or not isinstance(protection, list)
        or any(not isinstance(item, str) or not item for item in protection)
        or protection != sorted(set(protection))
    ):
        raise RegistryError("archive record retention metadata is invalid")
    integrity = record.get("integrity")
    if (
        not isinstance(integrity, Mapping)
        or set(integrity) != {"state", "issues"}
        or integrity.get("state") not in {"verified", "corrupt"}
        or not isinstance(integrity.get("issues"), list)
        or any(not isinstance(item, str) or not item for item in integrity["issues"])
        or integrity["issues"] != sorted(set(integrity["issues"]))
    ):
        raise RegistryError("archive record integrity metadata is invalid")
    if not isinstance(record.get("files"), list):
        raise RegistryError("archive record files are invalid")
    seen: set[str] = set()
    for item in record["files"]:
        if not isinstance(item, Mapping) or set(item) != {
            "locator",
            "content_id",
            "size",
        }:
            raise RegistryError("archive record file fields are invalid")
        locator = _safe_locator(str(item["locator"])).as_posix()
        if not PurePosixPath(locator).is_relative_to(unit):
            raise RegistryError("archive record file escapes its record unit")
        if locator in seen:
            raise RegistryError("archive record file locator is duplicated")
        seen.add(locator)
        if (
            not HASH.fullmatch(str(item["content_id"]))
            or not isinstance(item["size"], int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
        ):
            raise RegistryError("archive record file identity is invalid")
    if [item["locator"] for item in record["files"]] != sorted(seen):
        raise RegistryError("archive record files are not canonical")
    if seen.intersection(directories):
        raise RegistryError("archive record path is both a file and directory")
    if record["unit_kind"] == "file" and [
        item["locator"] for item in record["files"]
    ] != [unit.as_posix()]:
        raise RegistryError("archive file record unit is inconsistent")


def _manifest_identity(value: Mapping[str, Any]) -> str:
    return content_id({key: item for key, item in value.items() if key != "archive_id"})


def validate_archive_manifest(value: Mapping[str, Any]) -> None:
    required = {
        "schema",
        "archive_id",
        "format",
        "mode",
        "base_archive_id",
        "domains",
        "records",
        "blobs",
        "included_blob_ids",
        "coverage_blob_ids",
        "omitted",
        "missing",
        "secret_exclusions",
    }
    if set(value) != required or value.get("schema") != ARCHIVE_SCHEMA:
        raise RegistryError("record archive manifest fields are invalid")
    if value.get("format") != "ustar" or value.get("mode") not in {
        "full",
        "incremental",
    }:
        raise RegistryError("record archive format or mode is invalid")
    if value.get("archive_id") != _manifest_identity(value):
        raise RegistryConflict("record archive manifest identity mismatch")
    if value.get("base_archive_id") is not None and not HASH.fullmatch(
        str(value["base_archive_id"])
    ):
        raise RegistryError("record archive base identity is invalid")
    if (value["mode"] == "full") != (value.get("base_archive_id") is None):
        raise RegistryError("record archive mode and base identity disagree")
    supported_domains = {domain for domain, _path in DOMAIN_ROOTS}
    if (
        not isinstance(value.get("domains"), list)
        or any(not isinstance(item, str) for item in value["domains"])
        or value["domains"] != sorted(set(value["domains"]))
        or not set(value["domains"]).issubset(supported_domains)
    ):
        raise RegistryError("record archive domains are not canonical")
    records = value.get("records")
    if not isinstance(records, list):
        raise RegistryError("record archive record inventory is invalid")
    for record in records:
        if not isinstance(record, Mapping):
            raise RegistryError("record archive record inventory is invalid")
        _validate_record(record)
        if record["domain"] not in value["domains"]:
            raise RegistryError("archive record domain was not selected")
    if [(item["domain"], item["locator"]) for item in records] != sorted(
        (item["domain"], item["locator"]) for item in records
    ):
        raise RegistryError("record archive records are not canonical")
    blobs = value.get("blobs")
    if not isinstance(blobs, list):
        raise RegistryError("record archive blob inventory is invalid")
    seen = set()
    for item in blobs:
        if not isinstance(item, Mapping) or set(item) != {"content_id", "size"}:
            raise RegistryError("record archive blob fields are invalid")
        identity = str(item["content_id"])
        if (
            not HASH.fullmatch(identity)
            or identity in seen
            or not isinstance(item["size"], int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
        ):
            raise RegistryError("record archive blob identity is invalid")
        seen.add(identity)
    if [item["content_id"] for item in blobs] != sorted(seen):
        raise RegistryError("record archive blobs are not canonical")
    included = value.get("included_blob_ids")
    coverage = value.get("coverage_blob_ids")
    if (
        not isinstance(included, list)
        or any(not isinstance(item, str) for item in included)
        or included != sorted(set(included))
        or not set(included).issubset(seen)
    ):
        raise RegistryError("record archive included blob set is invalid")
    if (
        not isinstance(coverage, list)
        or any(not isinstance(item, str) for item in coverage)
        or coverage != sorted(set(coverage))
        or not set(included).issubset(coverage)
    ):
        raise RegistryError("record archive coverage blob set is invalid")
    referenced = {
        item["content_id"] for record in value["records"] for item in record["files"]
    }
    if referenced != seen or not referenced.issubset(set(coverage)):
        raise RegistryError("record archive lacks referenced blob coverage")
    if not set(included).issubset(referenced):
        raise RegistryError("record archive includes an unreferenced blob")
    if value["mode"] == "full" and (
        set(included) != referenced or set(coverage) != referenced
    ):
        raise RegistryError("full record archive coverage is incomplete")
    for field in ("omitted", "missing"):
        if not isinstance(value.get(field), list):
            raise RegistryError(f"record archive {field} inventory is invalid")
    if value.get("secret_exclusions") != list(SECRET_EXCLUSIONS):
        raise RegistryError("record archive secret exclusions are invalid")


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o440
    info.type = tarfile.REGTYPE
    return info


def _tar_projected_size(manifest_size: int, blob_sizes: Sequence[int]) -> int:
    def member(size: int) -> int:
        return 512 + ((size + 511) // 512) * 512

    unpadded = member(manifest_size) + sum(member(size) for size in blob_sizes) + 1024
    return (
        (unpadded + tarfile.RECORDSIZE - 1) // tarfile.RECORDSIZE
    ) * tarfile.RECORDSIZE


def _archive_parent(destination: Path) -> Path:
    current = destination.absolute().parent
    missing: list[str] = []
    while not current.exists() and not current.is_symlink():
        missing.append(current.name)
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise RegistryError("archive destination parent is unsafe")
    probe = current
    for part in reversed(missing):
        probe = probe / part
    return current


def inspect_archive(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RegistryError("record archive is missing or linked")
    metadata = path.stat(follow_symlinks=False)
    if metadata.st_size > MAX_ARCHIVE_BYTES:
        raise RegistryError("record archive exceeds the supported size limit")
    try:
        archive = tarfile.open(path, mode="r:")
    except (tarfile.TarError, OSError) as exc:
        raise RegistryError("record archive cannot be opened") from exc
    with archive:
        members: list[tarfile.TarInfo] = []
        try:
            while True:
                member = archive.next()
                if member is None:
                    break
                members.append(member)
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise RegistryError("record archive contains too many members")
        except tarfile.TarError as exc:
            raise RegistryError("record archive is truncated or corrupt") from exc
        if not members or members[0].name != ARCHIVE_MANIFEST:
            raise RegistryError("record archive manifest must be the first member")
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise RegistryError("record archive member is duplicated")
        for member in members:
            safe = _safe_locator(member.name).as_posix()
            if safe != member.name or not member.isfile():
                raise RegistryError("record archive contains an unsafe member")
            if (
                member.uid != 0
                or member.gid != 0
                or member.uname != ""
                or member.gname != ""
                or member.mtime != 0
                or member.mode != 0o440
            ):
                raise RegistryError(
                    "record archive member metadata is not deterministic"
                )
        manifest_member = members[0]
        if manifest_member.size > MAX_JSON_BYTES:
            raise RegistryError("record archive manifest exceeds its size limit")
        stream = archive.extractfile(manifest_member)
        if stream is None:
            raise RegistryError("record archive manifest cannot be read")
        raw = stream.read()
        try:
            manifest = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryError("record archive manifest is invalid JSON") from exc
        if not isinstance(manifest, dict) or raw != canonical_json(manifest) + b"\n":
            raise RegistryError("record archive manifest is not canonical JSON")
        validate_archive_manifest(manifest)
        expected_names = [
            ARCHIVE_MANIFEST,
            *[
                f"blobs/sha256/{identity[:2]}/{identity}"
                for identity in manifest["included_blob_ids"]
            ],
        ]
        if names != expected_names:
            raise RegistryError("record archive member inventory or order differs")
        sizes = {item["content_id"]: item["size"] for item in manifest["blobs"]}
        for member, identity in zip(members[1:], manifest["included_blob_ids"]):
            if member.size != sizes[identity]:
                raise RegistryConflict("record archive blob size mismatch")
            blob = archive.extractfile(member)
            if blob is None:
                raise RegistryError("record archive blob cannot be read")
            digest = hashlib.sha256()
            observed_size = 0
            while True:
                block = blob.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                observed_size += len(block)
            if digest.hexdigest() != identity or observed_size != sizes[identity]:
                raise RegistryConflict("record archive blob content mismatch")
    archive_identity, archive_size = file_identity(path)
    return {
        "manifest": manifest,
        "archive_sha256": archive_identity,
        "archive_size": archive_size,
    }


def archive_secret_issues(path: Path, manifest: Mapping[str, Any]) -> list[str]:
    locator_by_blob: dict[str, list[str]] = {}
    for record in manifest["records"]:
        for item in record["files"]:
            locator_by_blob.setdefault(item["content_id"], []).append(item["locator"])
    issues: list[str] = []
    issues.extend(
        secret_locator_issues(
            locator
            for record in manifest["records"]
            for locator in [record["locator"], *record["directories"]]
        )
    )
    with tarfile.open(path, mode="r:") as archive:
        members = {member.name: member for member in archive.getmembers()}
        for identity in manifest["included_blob_ids"]:
            member = members[f"blobs/sha256/{identity[:2]}/{identity}"]
            stream = archive.extractfile(member)
            if stream is None:
                raise RegistryError("record archive blob cannot be inspected")
            issues.extend(
                _stream_secret_issues(
                    stream, locators=locator_by_blob.get(identity, [])
                )
            )
    return sorted(set(issues))


def make_archive_manifest(
    inventory: Mapping[str, Any],
    *,
    domains: Sequence[str],
    base_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    records = [item for item in inventory["records"] if item["domain"] in set(domains)]
    file_rows = [file for record in records for file in record["files"]]
    blobs_by_id: dict[str, int] = {}
    for item in file_rows:
        previous = blobs_by_id.setdefault(item["content_id"], item["size"])
        if previous != item["size"]:
            raise RegistryConflict("duplicate content identity has conflicting sizes")
    coverage = set(base_manifest["coverage_blob_ids"] if base_manifest else [])
    included = sorted(set(blobs_by_id) - coverage)
    coverage.update(blobs_by_id)
    value: dict[str, Any] = {
        "schema": ARCHIVE_SCHEMA,
        "archive_id": "0" * 64,
        "format": "ustar",
        "mode": "incremental" if base_manifest else "full",
        "base_archive_id": base_manifest["archive_id"] if base_manifest else None,
        "domains": sorted(set(domains)),
        "records": records,
        "blobs": [
            {"content_id": identity, "size": blobs_by_id[identity]}
            for identity in sorted(blobs_by_id)
        ],
        "included_blob_ids": included,
        "coverage_blob_ids": sorted(coverage),
        "omitted": list(inventory["omitted"]),
        "missing": [
            {
                "record_id": record["record_id"],
                "issues": record["integrity"]["issues"],
            }
            for record in records
            if record["integrity"]["state"] != "verified"
        ],
        "secret_exclusions": list(SECRET_EXCLUSIONS),
    }
    value["archive_id"] = _manifest_identity(value)
    validate_archive_manifest(value)
    return value


def build_archive_plan(
    root: Path,
    *,
    destination: Path,
    domains: Sequence[str] | None = None,
    base_archive: Path | None = None,
    exclude_record_locators: Sequence[str] = (),
) -> dict[str, Any]:
    root = ensure_registry_root(root, create=False)
    selected = sorted(set(domains or ARCHIVABLE_DOMAINS))
    excluded = sorted(set(exclude_record_locators))
    inventory = build_inventory(
        root, domains=selected, exclude_record_locators=excluded
    )
    secret_findings: list[str] = []
    for record in inventory["records"]:
        secret_findings.extend(
            secret_locator_issues([record["locator"], *record["directories"]])
        )
        for item in record["files"]:
            path = root.joinpath(*_safe_locator(item["locator"]).parts)
            secret_findings.extend(secret_issues(path, item["locator"]))
    if secret_findings:
        raise RegistrySecretError(
            "archive selection contains secret-bearing content: "
            + "; ".join(sorted(set(secret_findings)))
        )
    base = None
    base_identity = None
    base_size = None
    if base_archive:
        base_archive = base_archive.expanduser().absolute()
        inspected_base = inspect_archive(base_archive)
        base = inspected_base["manifest"]
        base_identity = inspected_base["archive_sha256"]
        base_size = inspected_base["archive_size"]
        base_secret_findings = archive_secret_issues(base_archive, base)
        if base_secret_findings:
            raise RegistrySecretError(
                "incremental archive base contains secret-bearing content: "
                + "; ".join(base_secret_findings)
            )
    manifest = make_archive_manifest(inventory, domains=selected, base_manifest=base)
    sizes = {item["content_id"]: item["size"] for item in manifest["blobs"]}
    manifest_size = len(canonical_json(manifest)) + 1
    projected = _tar_projected_size(
        manifest_size, [sizes[item] for item in manifest["included_blob_ids"]]
    )
    destination = destination.expanduser().absolute()
    if destination.is_relative_to(root):
        raise RegistryError("record archives must be written outside the registry")
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise RegistryError("archive destination is linked or not a regular file")
    destination_state = "absent"
    additional_required = projected
    if destination.exists():
        existing = inspect_archive(destination)
        if existing["manifest"] != manifest:
            raise RegistryConflict(
                "archive destination already contains a different archive"
            )
        destination_state = "verified-identical"
        additional_required = 0
    capacity_root = _archive_parent(destination)
    usage = shutil.disk_usage(capacity_root)
    plan_unsigned: dict[str, Any] = {
        "schema": "iii.record-archive-plan/v1",
        "registry_index_id": inventory["index_id"],
        "destination": str(destination),
        "base_archive": (
            str(base_archive.expanduser().absolute()) if base_archive else None
        ),
        "base_archive_sha256": base_identity,
        "base_archive_size": base_size,
        "archive_manifest": manifest,
        "included_domains": selected,
        "excluded_record_locators": excluded,
        "mode": manifest["mode"],
        "record_count": len(manifest["records"]),
        "referenced_blob_count": len(manifest["blobs"]),
        "included_blob_count": len(manifest["included_blob_ids"]),
        "total_content_bytes": sum(item["size"] for item in manifest["blobs"]),
        "projected_archive_bytes": projected,
        "additional_required_bytes": additional_required,
        "destination_state": destination_state,
        "destination_free_bytes": usage.free,
        "capacity_sufficient": usage.free >= additional_required,
        "omitted": manifest["omitted"],
        "missing": manifest["missing"],
        "full": base is None and not manifest["omitted"] and not manifest["missing"],
        "incremental": base is not None,
    }
    plan_unsigned["plan_id"] = content_id(plan_unsigned)
    return plan_unsigned


def _write_archive(
    destination: Path,
    manifest: Mapping[str, Any],
    *,
    root: Path,
) -> tuple[str, int]:
    _mkdir_absolute(destination.parent, mode=0o700)
    temporary = destination.parent / (
        f".{destination.name}.partial-{manifest['archive_id']}"
    )
    if temporary.exists() or temporary.is_symlink():
        if temporary.is_symlink() or not temporary.is_file():
            raise RegistryError("archive staging path is unsafe")
        temporary.unlink()
    try:
        with tarfile.open(temporary, mode="x", format=tarfile.USTAR_FORMAT) as archive:
            raw = canonical_json(manifest) + b"\n"
            archive.addfile(_tar_info(ARCHIVE_MANIFEST, len(raw)), io.BytesIO(raw))
            sizes = {item["content_id"]: item["size"] for item in manifest["blobs"]}
            for identity in manifest["included_blob_ids"]:
                path = blob_path(root, identity)
                observed, size = file_identity(path)
                if observed != identity or size != sizes[identity]:
                    raise RegistryConflict("registry blob changed before archive write")
                with path.open("rb") as stream:
                    archive.addfile(
                        _tar_info(f"blobs/sha256/{identity[:2]}/{identity}", size),
                        stream,
                    )
        descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        observed, size = file_identity(temporary)
        if destination.exists():
            existing, existing_size = file_identity(destination)
            if existing != observed or existing_size != size:
                raise RegistryConflict(
                    "archive destination already contains other content"
                )
            temporary.unlink()
            return existing, existing_size
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
        return observed, size
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def apply_archive_plan(root: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    expected = content_id({key: item for key, item in plan.items() if key != "plan_id"})
    if (
        plan.get("schema") != "iii.record-archive-plan/v1"
        or plan.get("plan_id") != expected
    ):
        raise RegistryConflict("retained archive plan identity mismatch")
    if not plan.get("capacity_sufficient"):
        raise RegistryError("archive destination lacks planned free capacity")
    if plan.get("missing"):
        raise RegistryError("archive plan contains missing or corrupt record content")
    if plan.get("omitted"):
        raise RegistryError("archive plan contains incomplete staging content")
    root = ensure_registry_root(root, create=True)
    destination = Path(str(plan["destination"]))
    manifest = plan["archive_manifest"]
    validate_archive_manifest(manifest)
    with registry_lock(root):
        if plan.get("base_archive"):
            verified_base = inspect_archive(Path(str(plan["base_archive"])))
            if (
                verified_base["archive_sha256"] != plan.get("base_archive_sha256")
                or verified_base["archive_size"] != plan.get("base_archive_size")
                or verified_base["manifest"]["archive_id"]
                != manifest["base_archive_id"]
                or archive_secret_issues(
                    Path(str(plan["base_archive"])), verified_base["manifest"]
                )
            ):
                raise RegistryConflict(
                    "incremental base archive changed after planning"
                )
        available = shutil.disk_usage(_archive_parent(destination)).free
        if available < int(plan["additional_required_bytes"]):
            raise RegistryError("archive destination capacity changed after planning")
        current = build_inventory(
            root,
            domains=plan["included_domains"],
            exclude_record_locators=plan.get("excluded_record_locators", []),
        )
        if current["index_id"] != plan["registry_index_id"]:
            raise RegistryConflict("registry changed after archive planning")
        source_by_id: dict[str, Path] = {}
        sizes: dict[str, int] = {}
        for record in current["records"]:
            for item in record["files"]:
                source_by_id.setdefault(
                    item["content_id"],
                    root.joinpath(*_safe_locator(item["locator"]).parts),
                )
                sizes[item["content_id"]] = item["size"]
        for identity, source in sorted(source_by_id.items()):
            materialize_blob(root, source, identity=identity, size=sizes[identity])
        archive_sha256, archive_size = _write_archive(destination, manifest, root=root)
        verified = inspect_archive(destination)
        if (
            verified["manifest"] != manifest
            or verified["archive_sha256"] != archive_sha256
            or verified["archive_size"] != archive_size
        ):
            raise RegistryConflict("completed archive failed post-write verification")
        receipt_unsigned: dict[str, Any] = {
            "schema": "iii.record-archive-receipt/v1",
            "archive_id": manifest["archive_id"],
            "archive_sha256": archive_sha256,
            "archive_size": archive_size,
            "archive_path": str(destination),
            "verified_at": utc_now(),
            "mode": manifest["mode"],
            "base_archive_id": manifest["base_archive_id"],
            "covered_record_ids": sorted(
                record["record_id"] for record in manifest["records"]
            ),
            "covered_irreplaceable_record_ids": sorted(
                record["record_id"]
                for record in manifest["records"]
                if record["irreplaceable"]
            ),
            "covered_domains": manifest["domains"],
            "verification_state": "verified",
            "creation_source": "iii records archive",
        }
        receipt = {**receipt_unsigned, "receipt_id": content_id(receipt_unsigned)}
        atomic_json(
            root,
            f"archive-receipts/{receipt['receipt_id']}.json",
            receipt,
        )
        write_index(root, build_inventory(root))
        return receipt


def _archive_blob_stream(
    archive: tarfile.TarFile,
    members: Mapping[str, tarfile.TarInfo],
    identity: str,
) -> BinaryIO:
    member = members[f"blobs/sha256/{identity[:2]}/{identity}"]
    stream = archive.extractfile(member)
    if stream is None:
        raise RegistryError("archive blob cannot be read")
    return stream


def build_import_plan(root: Path, *, archive_path: Path) -> dict[str, Any]:
    root = ensure_registry_root(root, create=False)
    archive_path = archive_path.expanduser().absolute()
    inspected = inspect_archive(archive_path)
    manifest = inspected["manifest"]
    secret_findings = archive_secret_issues(archive_path, manifest)
    if secret_findings:
        raise RegistrySecretError(
            "archive contains secret-bearing content: " + "; ".join(secret_findings)
        )
    included = set(manifest["included_blob_ids"])
    referenced = {
        item["content_id"] for record in manifest["records"] for item in record["files"]
    }
    locator_by_blob: dict[str, list[str]] = {}
    for record in manifest["records"]:
        for item in record["files"]:
            locator_by_blob.setdefault(item["content_id"], []).append(item["locator"])
    sizes = {item["content_id"]: item["size"] for item in manifest["blobs"]}
    missing_blobs = []
    for identity in sorted(referenced):
        if identity in included:
            continue
        path = blob_path(root, identity)
        if not path.exists() or path.is_symlink():
            missing_blobs.append(identity)
            continue
        observed, observed_size = file_identity(path)
        if observed != identity or observed_size != sizes[identity]:
            missing_blobs.append(identity)
            continue
        findings = []
        for locator in locator_by_blob[identity]:
            findings.extend(secret_issues(path, locator))
        if findings:
            raise RegistrySecretError(
                "incremental archive base contains secret-bearing content: "
                + "; ".join(sorted(set(findings)))
            )
    conflicts = []
    idempotent = []
    for record in manifest["records"]:
        unit = _safe_locator(record["locator"])
        if record["domain"] not in {domain for domain, _path in DOMAIN_ROOTS}:
            raise RegistryError("archive record domain is not supported locally")
        current = root
        for part in unit.parts[:-1]:
            current = current / part
            if current.exists() or current.is_symlink():
                if current.is_symlink() or not current.is_dir():
                    conflicts.append(
                        {"locator": unit.as_posix(), "reason": "unsafe-existing-parent"}
                    )
                    break
        unit_path = root.joinpath(*unit.parts)
        if (
            record["unit_kind"] == "directory"
            and (unit_path.exists() or unit_path.is_symlink())
            and (unit_path.is_symlink() or not unit_path.is_dir())
        ):
            conflicts.append(
                {"locator": unit.as_posix(), "reason": "unsafe-existing-unit"}
            )
        for locator_value in record["directories"]:
            directory = root.joinpath(*_safe_locator(locator_value).parts)
            if (directory.exists() or directory.is_symlink()) and (
                directory.is_symlink() or not directory.is_dir()
            ):
                conflicts.append(
                    {
                        "locator": locator_value,
                        "reason": "unsafe-existing-directory",
                    }
                )
        for item in record["files"]:
            locator = _safe_locator(item["locator"])
            if not locator.is_relative_to(unit):
                raise RegistryError("archive record file escapes its record unit")
            destination = root.joinpath(*locator.parts)
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_file():
                    conflicts.append(
                        {
                            "locator": locator.as_posix(),
                            "reason": "unsafe-existing-path",
                        }
                    )
                    continue
                observed, size = file_identity(destination)
                if observed == item["content_id"] and size == item["size"]:
                    idempotent.append(locator.as_posix())
                else:
                    conflicts.append(
                        {"locator": locator.as_posix(), "reason": "content-conflict"}
                    )
    unsigned: dict[str, Any] = {
        "schema": "iii.record-import-plan/v1",
        "archive_path": str(archive_path),
        "archive_sha256": inspected["archive_sha256"],
        "archive_size": inspected["archive_size"],
        "archive_manifest": manifest,
        "missing_blob_ids": sorted(missing_blobs),
        "conflicts": sorted(conflicts, key=lambda item: item["locator"]),
        "idempotent_locators": sorted(idempotent),
        "record_count": len(manifest["records"]),
        "cross_computer_safe": True,
    }
    unsigned["plan_id"] = content_id(unsigned)
    return unsigned


def _copy_blob_to_record(root: Path, identity: str, size: int, locator: str) -> Path:
    source = blob_path(root, identity)
    observed, observed_size = file_identity(source)
    if observed != identity or observed_size != size:
        raise RegistryConflict("import source blob is missing or corrupt")
    relative = _safe_locator(locator)
    destination = root.joinpath(*relative.parts)
    _mkdir_relative(root, PurePosixPath(*relative.parts[:-1]), mode=0o700)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            raise RegistryConflict("import destination is unsafe")
        current, current_size = file_identity(destination)
        if current != identity or current_size != size:
            raise RegistryConflict("import would overwrite conflicting content")
        return destination
    temporary = destination.parent / (
        f".{destination.name}.partial-import-{os.getpid()}-{uuid4().hex}"
    )
    try:
        with source.open("rb") as input_stream:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            try:
                while True:
                    block = input_stream.read(1024 * 1024)
                    if not block:
                        break
                    view = memoryview(block)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("import record write made no progress")
                        view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        check, check_size = file_identity(temporary)
        if check != identity or check_size != size:
            raise RegistryConflict("imported record failed local verification")
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
        return destination
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def apply_import_plan(root: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    expected = content_id({key: item for key, item in plan.items() if key != "plan_id"})
    if (
        plan.get("schema") != "iii.record-import-plan/v1"
        or plan.get("plan_id") != expected
    ):
        raise RegistryConflict("retained import plan identity mismatch")
    if plan.get("conflicts"):
        raise RegistryConflict("record import has destination conflicts")
    if plan.get("missing_blob_ids"):
        raise RegistryError("incremental archive base content is unavailable")
    root = ensure_registry_root(root, create=True)
    archive_path = Path(str(plan["archive_path"]))
    with registry_lock(root):
        inspected = inspect_archive(archive_path)
        if (
            inspected["archive_sha256"] != plan["archive_sha256"]
            or inspected["archive_size"] != plan["archive_size"]
            or inspected["manifest"] != plan["archive_manifest"]
        ):
            raise RegistryConflict("record archive changed after import planning")
        manifest = inspected["manifest"]
        if archive_secret_issues(archive_path, manifest):
            raise RegistrySecretError("record archive secret policy changed at apply")
        sizes = {item["content_id"]: item["size"] for item in manifest["blobs"]}
        with tarfile.open(archive_path, mode="r:") as archive:
            members = {member.name: member for member in archive.getmembers()}
            for identity in manifest["included_blob_ids"]:
                stream = _archive_blob_stream(archive, members, identity)
                _copy_stream_to_blob(
                    root, stream, identity=identity, size=sizes[identity]
                )
        referenced = {
            item["content_id"]
            for record in manifest["records"]
            for item in record["files"]
        }
        for identity in sorted(referenced):
            path = blob_path(root, identity)
            observed, size = file_identity(path)
            if observed != identity or size != sizes[identity]:
                raise RegistryConflict("import blob coverage is incomplete")
            locators = [
                item["locator"]
                for record in manifest["records"]
                for item in record["files"]
                if item["content_id"] == identity
            ]
            findings = []
            for locator in locators:
                findings.extend(secret_issues(path, locator))
            if findings:
                raise RegistrySecretError(
                    "record import contains secret-bearing content: "
                    + "; ".join(sorted(set(findings)))
                )
        imported = []
        for record in manifest["records"]:
            if record["unit_kind"] == "directory":
                _mkdir_relative(root, _safe_locator(record["locator"]), mode=0o700)
                imported.append(record["locator"])
                for locator in record["directories"]:
                    _mkdir_relative(root, _safe_locator(locator), mode=0o700)
                    imported.append(locator)
            for item in record["files"]:
                _copy_blob_to_record(
                    root,
                    item["content_id"],
                    item["size"],
                    item["locator"],
                )
                imported.append(item["locator"])
        receipt_unsigned: dict[str, Any] = {
            "schema": "iii.record-import-receipt/v1",
            "archive_id": manifest["archive_id"],
            "archive_sha256": inspected["archive_sha256"],
            "archive_size": inspected["archive_size"],
            "source_path": str(archive_path),
            "imported_at": utc_now(),
            "record_ids": sorted(record["record_id"] for record in manifest["records"]),
            "materialized_locators": sorted(imported),
            "verification_state": "verified",
            "creation_source": "iii records import",
        }
        receipt = {**receipt_unsigned, "receipt_id": content_id(receipt_unsigned)}
        atomic_json(root, f"import-receipts/{receipt['receipt_id']}.json", receipt)
        write_index(root, build_inventory(root))
        return receipt


def archive_receipts(root: Path) -> list[dict[str, Any]]:
    """Return content-authentic receipts without requiring mounted archive media."""

    rows: list[dict[str, Any]] = []
    receipts_root = root / "archive-receipts"
    if not receipts_root.exists() and not receipts_root.is_symlink():
        return rows
    if receipts_root.is_symlink() or not receipts_root.is_dir():
        raise RegistryError("archive receipt registry is unsafe")
    for path in sorted(receipts_root.glob("*.json")):
        try:
            receipt = read_canonical_json(path, label="archive receipt")
        except (RegistryError, OSError):
            continue
        unsigned = {key: item for key, item in receipt.items() if key != "receipt_id"}
        expected_fields = {
            "schema",
            "receipt_id",
            "archive_id",
            "archive_sha256",
            "archive_size",
            "archive_path",
            "verified_at",
            "mode",
            "base_archive_id",
            "covered_record_ids",
            "covered_irreplaceable_record_ids",
            "covered_domains",
            "verification_state",
            "creation_source",
        }
        if (
            set(receipt) != expected_fields
            or receipt.get("schema") != "iii.record-archive-receipt/v1"
            or receipt.get("receipt_id") != content_id(unsigned)
            or receipt.get("verification_state") != "verified"
            or not HASH.fullmatch(str(receipt.get("archive_id", "")))
            or not HASH.fullmatch(str(receipt.get("archive_sha256", "")))
            or not isinstance(receipt.get("archive_size"), int)
            or isinstance(receipt.get("archive_size"), bool)
            or receipt["archive_size"] < 1
            or not isinstance(receipt.get("archive_path"), str)
            or not Path(receipt["archive_path"]).is_absolute()
            or receipt.get("mode") not in {"full", "incremental"}
            or not isinstance(receipt.get("covered_record_ids"), list)
            or not isinstance(receipt.get("covered_irreplaceable_record_ids"), list)
            or any(
                not isinstance(item, str) or not HASH.fullmatch(item)
                for field in (
                    receipt["covered_record_ids"],
                    receipt["covered_irreplaceable_record_ids"],
                )
                for item in field
            )
            or receipt["covered_record_ids"]
            != sorted(set(receipt["covered_record_ids"]))
            or receipt["covered_irreplaceable_record_ids"]
            != sorted(set(receipt["covered_irreplaceable_record_ids"]))
        ):
            continue
        rows.append(receipt)
    return rows


def verified_archive_receipts(root: Path) -> list[dict[str, Any]]:
    """Return receipts whose referenced archives still verify byte-for-byte."""

    rows: list[dict[str, Any]] = []
    for receipt in archive_receipts(root):
        try:
            inspected = inspect_archive(Path(receipt["archive_path"]))
        except (RegistryError, OSError):
            continue
        manifest = inspected["manifest"]
        if (
            inspected["archive_sha256"] != receipt["archive_sha256"]
            or inspected["archive_size"] != receipt["archive_size"]
            or manifest["archive_id"] != receipt["archive_id"]
            or manifest["mode"] != receipt["mode"]
            or manifest["base_archive_id"] != receipt["base_archive_id"]
            or manifest["domains"] != receipt["covered_domains"]
            or sorted(record["record_id"] for record in manifest["records"])
            != receipt["covered_record_ids"]
            or sorted(
                record["record_id"]
                for record in manifest["records"]
                if record["irreplaceable"]
            )
            != receipt["covered_irreplaceable_record_ids"]
        ):
            continue
        rows.append(receipt)
    return rows


def build_prune_plan(
    root: Path,
    *,
    record_ids: Sequence[str],
    exclude_record_locators: Sequence[str] = (),
) -> dict[str, Any]:
    """Plan exact explicit deletion while retaining every safety-critical record."""

    root = ensure_registry_root(root, create=False)
    requested = sorted(set(record_ids))
    if not requested or any(not HASH.fullmatch(item) for item in requested):
        raise RegistryError("prune requires valid explicit record identities")
    excluded = sorted(set(exclude_record_locators))
    inventory = build_inventory(root, exclude_record_locators=excluded)
    by_id = {record["record_id"]: record for record in inventory["records"]}
    unknown = sorted(set(requested) - set(by_id))
    archived = {
        record_id
        for receipt in verified_archive_receipts(root)
        for record_id in receipt.get("covered_record_ids", [])
        if isinstance(record_id, str)
    }
    references = {
        reference
        for record in inventory["records"]
        if record["domain"] not in {"archive-receipts", "import-receipts"}
        for reference in record["references"]
    }
    candidates: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    for identifier in requested:
        record = by_id.get(identifier)
        if record is None:
            continue
        reasons = set(record["protection"])
        content_ids = {item["content_id"] for item in record["files"]}
        if identifier in references or content_ids.intersection(references):
            reasons.add("referenced-by-retained-record")
        if record["irreplaceable"] and identifier not in archived:
            reasons.add("irreplaceable-not-externally-archived")
        row = {
            "record_id": identifier,
            "domain": record["domain"],
            "locator": record["locator"],
            "files": record["files"],
            "irreplaceable": record["irreplaceable"],
            "archive_verified": identifier in archived,
        }
        if reasons:
            protected.append({**row, "reasons": sorted(reasons)})
        else:
            candidates.append(row)
    unsigned: dict[str, Any] = {
        "schema": "iii.record-prune-plan/v1",
        "registry_index_id": inventory["index_id"],
        "requested_record_ids": requested,
        "unknown_record_ids": unknown,
        "candidates": candidates,
        "protected": protected,
        "automatic": False,
        "blob_store_pruned": False,
        "excluded_record_locators": excluded,
    }
    return {**unsigned, "plan_id": content_id(unsigned)}


def _remove_record_unit(root: Path, record: Mapping[str, Any]) -> None:
    unit = root.joinpath(*_safe_locator(str(record["locator"])).parts)
    if unit.is_symlink() or not unit.exists():
        raise RegistryConflict("prune record unit changed after planning")
    observed = _record_descriptor(str(record["domain"]), unit, root)
    if (
        observed["record_id"] != record["record_id"]
        or observed["files"] != record["files"]
    ):
        raise RegistryConflict("prune record content changed after planning")
    files = [
        root.joinpath(*_safe_locator(str(item["locator"])).parts)
        for item in record["files"]
    ]
    for path in files:
        path.unlink()
    if unit.is_dir():
        directories = sorted(
            [path for path in unit.rglob("*") if path.is_dir()],
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            directory.rmdir()
        unit.rmdir()
    else:
        if files != [unit]:
            raise RegistryConflict("prune file unit inventory is inconsistent")
    _fsync_directory(unit.parent)


def apply_prune_plan(root: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    expected = content_id({key: item for key, item in plan.items() if key != "plan_id"})
    if (
        plan.get("schema") != "iii.record-prune-plan/v1"
        or plan.get("plan_id") != expected
    ):
        raise RegistryConflict("retained prune plan identity mismatch")
    root = ensure_registry_root(root, create=True)
    with registry_lock(root):
        fresh = build_prune_plan(
            root,
            record_ids=plan.get("requested_record_ids", []),
            exclude_record_locators=plan.get("excluded_record_locators", []),
        )
        if fresh != plan:
            raise RegistryConflict(
                "registry or archive coverage changed after prune planning"
            )
        for record in plan["candidates"]:
            _remove_record_unit(root, record)
        updated = build_inventory(root)
        write_index(root, updated)
        return {
            "schema": "iii.record-prune-result/v1",
            "plan_id": plan["plan_id"],
            "removed_record_ids": sorted(
                record["record_id"] for record in plan["candidates"]
            ),
            "protected": plan["protected"],
            "unknown_record_ids": plan["unknown_record_ids"],
            "automatic": False,
            "blob_store_pruned": False,
            "registry_index_id": updated["index_id"],
        }


def archive_coverage(root: Path, *, warning_days: int = 30) -> dict[str, Any]:
    if (
        not isinstance(warning_days, int)
        or isinstance(warning_days, bool)
        or warning_days < 1
    ):
        raise RegistryError("archive warning age must be a positive whole number")
    root = ensure_registry_root(root, create=False)
    inventory = (
        build_inventory(root)
        if root.exists()
        else {
            "records": [],
            "index_id": content_id(
                {"schema": INDEX_SCHEMA, "records": [], "blobs": [], "omitted": []}
            ),
        }
    )
    current = {
        item["record_id"] for item in inventory["records"] if item["irreplaceable"]
    }
    rows = []
    for receipt in archive_receipts(root):
        try:
            verified_at = datetime.fromisoformat(
                str(receipt["verified_at"]).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            continue
        if verified_at.tzinfo is None:
            continue
        age_days = max(
            0.0,
            (datetime.now(timezone.utc) - verified_at).total_seconds() / 86400,
        )
        covered = set(receipt.get("covered_irreplaceable_record_ids", []))
        rows.append((verified_at, receipt, age_days, covered))
    if not rows:
        return {
            "schema": "iii.record-archive-coverage/v1",
            "recent": False,
            "complete": not current,
            "age_days": None,
            "verified_at": None,
            "archive_id": None,
            "receipt_id": None,
            "archive_path": None,
            "archive_available": False,
            "covered_irreplaceable_records": 0,
            "current_irreplaceable_records": len(current),
            "missing_record_ids": sorted(current),
        }
    complete_rows = [item for item in rows if current.issubset(item[3])]
    _when, receipt, age_days, covered = max(
        complete_rows or rows, key=lambda item: item[0]
    )
    missing = current - covered
    return {
        "schema": "iii.record-archive-coverage/v1",
        "recent": age_days <= warning_days and not missing,
        "complete": not missing,
        "age_days": age_days,
        "verified_at": receipt["verified_at"],
        "archive_id": receipt["archive_id"],
        "receipt_id": receipt["receipt_id"],
        "archive_path": receipt["archive_path"],
        "archive_available": (
            Path(receipt["archive_path"]).is_file()
            and not Path(receipt["archive_path"]).is_symlink()
        ),
        "covered_irreplaceable_records": len(current - missing),
        "current_irreplaceable_records": len(current),
        "missing_record_ids": sorted(missing),
    }
