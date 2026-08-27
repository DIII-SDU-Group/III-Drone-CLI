from __future__ import annotations

import json

from iii.runtime_api_client import RuntimeApiClient


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
    assert requests[1][0].full_url.endswith(
        "/cli/configuration/capture-source/snapshots%2Foperator%20tuned.yaml?expected_profile=real"
    )
    assert requests[2][0].full_url.endswith("/cli/configuration/snapshots/delete")
    assert json.loads(requests[2][0].data)["force"] is False
    assert all(
        request.headers["X-iii-cli-token"] == "capture-token"
        for request, _kwargs in requests
    )
    assert all(kwargs["timeout"] == 9 for _request, kwargs in requests)
