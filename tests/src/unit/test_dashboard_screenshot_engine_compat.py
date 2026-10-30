"""Full-page fallback is limited to Supervisor-verified legacy Puppet."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.dashboard_screenshot.provision import EngineTarget


class _Response:
    def __init__(self, status: int, body: bytes, content_type: str) -> None:
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.body = body

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield self.body


class _Stream:
    def __init__(self, response: _Response) -> None:
        self.response = response

    async def __aenter__(self) -> _Response:
        return self.response

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _Client:
    requests: ClassVar[list[dict[str, Any]]]
    responses: ClassVar[list[_Response]]

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def stream(
        self,
        method: str,
        url: str,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _Stream:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "params": dict(params) if params is not None else None,
                "timeout": timeout,
            }
        )
        return _Stream(self.responses.pop(0))


async def _capture_error(
    monkeypatch: pytest.MonkeyPatch,
    target: EngineTarget,
    responses: list[_Response],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    capture = _configure_client(monkeypatch, target, responses)
    with pytest.raises(ToolError) as exc_info:
        await capture.capture_dashboard_images("lovelace/0", width=900, full_page=True)
    return _Client.requests, json.loads(str(exc_info.value))


def _configure_client(
    monkeypatch: pytest.MonkeyPatch,
    target: EngineTarget,
    responses: list[_Response],
) -> Any:
    from ha_mcp.dashboard_screenshot import capture

    async def resolve_engine() -> EngineTarget:
        return target

    monkeypatch.setattr(capture, "resolve_engine", resolve_engine)
    monkeypatch.setattr(capture.httpx, "AsyncClient", _Client)
    _Client.requests = []
    _Client.responses = responses
    return capture


def _error(status: int, body: bytes = b"invalid viewport") -> _Response:
    return _Response(status, body, "text/plain")


async def test_verified_legacy_puppet_retries_with_maximum_height(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _configure_client(
        monkeypatch,
        EngineTarget(
            "http://puppet:10000", engine_kind="puppet", engine_version="2.4.9"
        ),
        [_error(400, b""), _Response(200, b"\x89PNG\r\n\x1a\nimage", "image/png")],
    )

    result = await capture.capture_dashboard_images(
        "lovelace/0", width=900, full_page=True
    )

    assert [request["params"]["viewport"] for request in _Client.requests] == [
        "900xauto",
        "900x4096",
    ]
    assert result[0].height == 4096
    assert result[0].legacy_full_page_fallback is True


async def test_unknown_explicit_engine_is_not_retried_as_legacy_puppet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, error = await _capture_error(
        monkeypatch,
        EngineTarget(url="http://engine:10000"),
        [_error(400), _error(404, b"not found")],
    )

    assert len(requests) == 1
    assert requests[0]["url"] == "http://engine:10000/lovelace/0"
    assert requests[0]["params"]["viewport"] == "900xauto"
    assert error["engine_kind"] == "unknown"


async def test_current_puppet_does_not_use_legacy_full_page_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, error = await _capture_error(
        monkeypatch,
        EngineTarget(
            url="http://puppet:10000", engine_kind="puppet", engine_version="2.5.0"
        ),
        [_error(400)],
    )

    assert len(requests) == 1
    assert requests[0]["params"]["viewport"] == "900xauto"
    assert error["engine_kind"] == "puppet"
    assert error["engine_version"] == "2.5.0"
