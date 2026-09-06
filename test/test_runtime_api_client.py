from __future__ import annotations

import json
from pathlib import Path

import pytest

from iii.runtime_api_client import RuntimeApiClient, RuntimeApiError


def test_configuration_capture_transport_is_cli_authenticated_and_profile_bound(
    monkeypatch,
):
    import iii.runtime_api_client as module

    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok":true}'

    def open_request(request, **kwargs):
        requests.append((request, kwargs))
        return Response()

    monkeypatch.setattr(module, "urlopen", open_request)
    client = RuntimeApiClient(
        base_url="http://iii.local:8765",
        cli_token="capture-token",
        timeout_seconds=9,
    )

    client.configuration_journal(
        expected_profile="real",
        session_id="a" * 64,
        after_sequence=7,
        limit=25,
    )
    client.configuration_state()
    client.configuration_capture_source(
        snapshot_id="snapshots/operator tuned.yaml", expected_profile="real"
    )
    client.delete_configuration_snapshot(
        {
            "schema": "iii.configuration-snapshot-delete-request/v1",
            "snapshot_id": "snapshots/operator tuned.yaml",
            "force": False,
            "confirmation": None,
            "capture_receipt": {},
        }
    )

    assert requests[0][0].full_url.endswith(
        "/cli/configuration/journal?expected_profile=real&after_sequence=7&limit=25&session_id="
        + "a" * 64
    )
    assert requests[1][0].full_url.endswith("/cli/configuration/state")
    assert requests[2][0].full_url.endswith(
        "/cli/configuration/capture-source/snapshots%2Foperator%20tuned.yaml?expected_profile=real"
    )
    assert requests[3][0].full_url.endswith("/cli/configuration/snapshots/delete")
    assert json.loads(requests[3][0].data)["force"] is False
    assert all(
        request.headers["X-iii-cli-token"] == "capture-token"
        for request, _kwargs in requests
    )
    assert all(kwargs["timeout"] == 9 for _request, kwargs in requests)


def test_runtime_api_client_default_timeout_allows_lifecycle_operations(monkeypatch):
    monkeypatch.delenv("III_RUNTIME_API_CLI_TIMEOUT_SEC", raising=False)
    monkeypatch.setenv("III_RUNTIME_API_URL", "http://runtime.example")

    client = RuntimeApiClient.from_env()

    assert client.timeout_seconds == 210.0


def test_explicit_target_endpoint_overrides_ambient_runtime_url(monkeypatch):
    monkeypatch.setenv("III_RUNTIME_API_URL", "http://localhost:8765")
    monkeypatch.setenv("III_RUNTIME_API_CLI_TOKEN", "token")

    aircraft = RuntimeApiClient.from_env(endpoint="iii.local")
    simulation = RuntimeApiClient.from_env(endpoint="local")

    assert aircraft.base_url == "http://iii.local:8765"
    assert simulation.base_url == "http://localhost:8765"


def test_identity_and_vehicle_status_use_expected_read_only_endpoints(monkeypatch):
    import iii.runtime_api_client as module

    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"profile":"hil","armed":false}'

    def open_request(request, **kwargs):
        requests.append((request, kwargs))
        return Response()

    monkeypatch.setattr(module, "urlopen", open_request)
    client = RuntimeApiClient(
        base_url="http://iii.local:8765", cli_token="machine-token"
    )

    assert client.identity()["profile"] == "hil"
    assert client.vehicle_status()["armed"] is False
    assert [request.full_url for request, _kwargs in requests] == [
        "http://iii.local:8765/identity",
        "http://iii.local:8765/cli/vehicle/status",
    ]
    assert all(
        request.headers["X-iii-cli-token"] == "machine-token"
        for request, _kwargs in requests
    )


def test_runtime_api_client_uses_owner_only_default_token_file(
    monkeypatch, tmp_path: Path
):
    token = tmp_path / "iii/credentials/runtime-api.token"
    token.parent.mkdir(parents=True)
    token.write_text("enrolled-token\n")
    token.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("III_RUNTIME_API_CLI_TOKEN", raising=False)
    monkeypatch.delenv("III_RUNTIME_API_TOKEN_FILE", raising=False)

    client = RuntimeApiClient.from_env()

    assert client.cli_token == "enrolled-token"


def test_runtime_api_client_rejects_unsafe_token_file(
    monkeypatch, tmp_path: Path
):
    token = tmp_path / "runtime-api.token"
    token.write_text("exposed-token\n")
    token.chmod(0o644)
    monkeypatch.delenv("III_RUNTIME_API_CLI_TOKEN", raising=False)
    monkeypatch.setenv("III_RUNTIME_API_TOKEN_FILE", str(token))

    with pytest.raises(RuntimeApiError, match="owner-only"):
        RuntimeApiClient.from_env()
