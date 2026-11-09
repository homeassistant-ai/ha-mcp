"""Unit tests for ``tests/src/e2e/utilities/component_services.py``.

The E2E cleanup steps reset the component settings through these helpers and
discard the response, so a failed reset only fails the test if the helper
raises. The services report a rejected caller with HTTP 200 and
``success: false``, which no status check catches. The request checks pin the
service name, the admin token and the body keys the component reads.

No Docker and no HA instance: HTTP goes to an ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.src.e2e.utilities import component_services
from tests.test_constants import TEST_TOKEN


def _serve(
    monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, Any]
) -> list[httpx.Request]:
    real_client = httpx.AsyncClient
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=body)

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(component_services.httpx, "AsyncClient", client)
    return requests


def _assert_admin_service_request(
    request: httpx.Request, service: str, data: dict[str, Any]
) -> None:
    assert request.method == "POST"
    assert request.url.path == f"/api/services/ha_mcp_tools/{service}"
    assert "return_response" in request.url.params
    assert request.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert json.loads(request.content) == data


@pytest.mark.parametrize(
    ("call", "service", "data"),
    [
        pytest.param(
            lambda: component_services.set_allowed_paths("http://ha", "t", ["/a"]),
            "set_allowed_paths",
            {"_ha_mcp_token": "t", "paths": ["/a"]},
            id="set_allowed_paths",
        ),
        pytest.param(
            lambda: component_services.set_extra_yaml_keys("http://ha", "t", ["k"]),
            "set_extra_yaml_keys",
            {"_ha_mcp_token": "t", "keys": ["k"]},
            id="set_extra_yaml_keys",
        ),
    ],
)
async def test_a_successful_reset_calls_the_service_and_returns_its_response(
    monkeypatch, call, service, data
):
    response = {"success": True}
    requests = _serve(monkeypatch, 200, {"service_response": response})

    assert await call() == response
    [request] = requests
    _assert_admin_service_request(request, service, data)


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


async def test_bootstrap_returns_the_caller_token(monkeypatch):
    requests = _serve(
        monkeypatch, 200, {"service_response": {"success": True, "token": "abc"}}
    )

    assert await component_services.bootstrap_caller_token("http://ha") == "abc"
    [request] = requests
    _assert_admin_service_request(request, "get_caller_token", {})


async def test_bootstrap_without_a_token_raises_naming_the_service(monkeypatch):
    _serve(monkeypatch, 200, {"service_response": {"success": True}})

    with pytest.raises(AssertionError, match="get_caller_token"):
        await component_services.bootstrap_caller_token("http://ha")
