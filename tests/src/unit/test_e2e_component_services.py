"""Unit tests for ``tests/src/e2e/utilities/component_services.py``.

The E2E cleanup steps reset the component settings through these helpers and
discard the response, so a failed reset only fails the test if the helper
raises. The services report a rejected caller with HTTP 200 and
``success: false``, which no status check catches.

No Docker and no HA instance: HTTP goes to an ``httpx.MockTransport``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.src.e2e.utilities import component_services


def _serve(monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, Any]) -> None:
    real_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(component_services.httpx, "AsyncClient", client)


async def test_a_successful_reset_returns_the_service_response(monkeypatch):
    response = {"success": True, "paths": []}
    _serve(monkeypatch, 200, {"service_response": response})

    assert await component_services.set_allowed_paths("http://ha", "t", []) == response


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(
            200,
            {"service_response": {"success": False, "error_code": "unauthorized"}},
            id="success-false",
        ),
        pytest.param(400, {"message": "Service not found"}, id="http-error"),
    ],
)
async def test_a_failed_reset_raises(monkeypatch, status, body):
    _serve(monkeypatch, status, body)

    with pytest.raises(AssertionError, match="set_extra_yaml_keys failed"):
        await component_services.set_extra_yaml_keys("http://ha", "t", [])
