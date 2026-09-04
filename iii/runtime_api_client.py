"""HTTP client for remote runtime-control commands served by iii-runtime-api."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


class RuntimeApiError(RuntimeError):
    """Raised when the remote runtime API cannot serve a CLI request."""


class RuntimeApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        cli_token: str,
        timeout_seconds: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.cli_token = cli_token
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "RuntimeApiClient":
        base_url = os.environ.get("III_RUNTIME_API_URL")
        if not base_url:
            host = (
                os.environ.get("III_RUNTIME_API_HOST")
                or os.environ.get("III_SSH_HOST")
                or "localhost"
            )
            port = os.environ.get("III_RUNTIME_API_PORT", "8765")
            base_url = f"http://{host}:{port}"
        token = os.environ.get("III_RUNTIME_API_CLI_TOKEN", "dev-cli-token")
        timeout = float(os.environ.get("III_RUNTIME_API_CLI_TIMEOUT_SEC", "60"))
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
