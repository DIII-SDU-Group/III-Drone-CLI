"""HTTP client for remote runtime-control commands served by iii-runtime-api."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


class RuntimeApiError(RuntimeError):
    """Raised when the remote runtime API cannot serve a CLI request."""


def _cli_token_from_env() -> str:
    explicit = os.environ.get("III_RUNTIME_API_CLI_TOKEN")
    if explicit is not None:
        if not explicit:
            raise RuntimeApiError("III_RUNTIME_API_CLI_TOKEN is empty")
        return explicit

    configured = os.environ.get("III_RUNTIME_API_TOKEN_FILE")
    if configured:
        path = Path(configured).expanduser().absolute()
        required = True
    else:
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        ).expanduser()
        path = (config_home / "iii/credentials/runtime-api.token").absolute()
        required = False

    if not path.exists() and not path.is_symlink():
        if required:
            raise RuntimeApiError("configured Runtime API token file is missing")
        return "dev-cli-token"
    if path.is_symlink() or not path.is_file():
        raise RuntimeApiError("Runtime API token file is linked or not a regular file")
    metadata = path.stat(follow_symlinks=False)
    if metadata.st_uid != os.getuid() or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeApiError("Runtime API token file ownership or type is unsafe")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeApiError("Runtime API token file must be owner-only")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeApiError("Runtime API token file could not be read") from exc
    if not token:
        raise RuntimeApiError("Runtime API token file is empty")
    return token


class RuntimeApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        cli_token: str,
        timeout_seconds: float = 210.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.cli_token = cli_token
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls, *, endpoint: str | None = None) -> "RuntimeApiClient":
        if endpoint is not None:
            host = "localhost" if endpoint == "local" else endpoint
            if host not in {"localhost", "iii.local"}:
                raise RuntimeApiError("runtime target endpoint is unsupported")
            port = os.environ.get("III_RUNTIME_API_PORT", "8765")
            base_url = f"http://{host}:{port}"
        else:
            base_url = os.environ.get("III_RUNTIME_API_URL")
        if not base_url:
            host = (
                os.environ.get("III_RUNTIME_API_HOST")
                or os.environ.get("III_SSH_HOST")
                or "localhost"
            )
            port = os.environ.get("III_RUNTIME_API_PORT", "8765")
            base_url = f"http://{host}:{port}"
        token = _cli_token_from_env()
        # A cold system start may consume the supervisor's 120-second external
        # service gate followed by its 60-second lifecycle discovery gate. The
        # client must not report failure while that bounded operation is still
        # progressing on the aircraft.
        timeout = float(os.environ.get("III_RUNTIME_API_CLI_TIMEOUT_SEC", "210"))
        return cls(base_url=base_url, cli_token=token, timeout_seconds=timeout)

    def command(
        self, command_id: str, parameters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/cli/commands",
            {
                "request_id": f"cli-{uuid4()}",
                "command_id": command_id,
                "client_label": "remote-cli",
                "parameters": parameters or {},
            },
        )

    def identity(self) -> dict[str, Any]:
        """Return the remote runtime identity through its public endpoint."""

        return self._request("GET", "/identity")

    def vehicle_status(self) -> dict[str, Any]:
        """Return CLI-authenticated read-only vehicle safety state."""

        return self._request("GET", "/cli/vehicle/status")

    def log_tail(self, source_id: str, *, lines: int = 200) -> dict[str, Any]:
        query = urlencode({"lines": lines})
        return self._request("GET", f"/cli/logs/{quote(source_id)}/tail?{query}")

    def configuration_journal(
        self,
        *,
        expected_profile: str,
        session_id: str | None,
        after_sequence: int,
        limit: int = 250,
    ) -> dict[str, Any]:
        query = urlencode(
            {
                "expected_profile": expected_profile,
                "after_sequence": after_sequence,
                "limit": limit,
                **({"session_id": session_id} if session_id is not None else {}),
            }
        )
        return self._request("GET", f"/cli/configuration/journal?{query}")

    def configuration_state(self) -> dict[str, Any]:
        """Return the CLI-authenticated configuration mirror state."""

        return self._request("GET", "/cli/configuration/state")

    def configuration_capture_source(
        self, *, snapshot_id: str, expected_profile: str
    ) -> dict[str, Any]:
        query = urlencode({"expected_profile": expected_profile})
        encoded = quote(snapshot_id, safe="")
        return self._request(
            "GET", f"/cli/configuration/capture-source/{encoded}?{query}"
        )

    def delete_configuration_snapshot(
        self, request_value: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "POST", "/cli/configuration/snapshots/delete", request_value
        )

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "X-III-CLI-Token": self.cli_token,
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeApiError(self._http_error_message(exc)) from exc
        except URLError as exc:
            raise RuntimeApiError(
                f"Runtime API unavailable at {self.base_url}: {exc.reason}"
            ) from exc
        except OSError as exc:
            raise RuntimeApiError(
                f"Runtime API unavailable at {self.base_url}: {exc}"
            ) from exc

        if not response_body:
            return {}
        try:
            return json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise RuntimeApiError(
                f"Runtime API returned invalid JSON from {self.base_url}{path}"
            ) from exc

    def _http_error_message(self, exc: HTTPError) -> str:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {}
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, str):
            return f"Runtime API request failed with HTTP {exc.code}: {detail}"
        return f"Runtime API request failed with HTTP {exc.code}"
