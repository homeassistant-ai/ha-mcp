"""Unit coverage for the final same-VM HAOS Core TLS scenario."""

from __future__ import annotations

import json
from typing import Any

import tests.src.haos_runtime as haos_runtime


def test_build_home_assistant_tls_config_preserves_runtime_settings() -> None:
    """A pending TLS trial keeps proxy/port settings and drops storage metadata."""
    assert hasattr(haos_runtime, "build_home_assistant_tls_config")
    stable = {
        "server_port": 8123,
        "use_x_forwarded_for": True,
        "trusted_proxies": ["172.30.32.0/23"],
        "ip_ban_enabled": True,
        "created_at": "2026-08-20T00:00:00+00:00",
        "error": None,
        "error_message": None,
    }

    result = haos_runtime.build_home_assistant_tls_config(
        stable,
        certificate_path="/config/ssl/haos-e2e-cert.pem",
        key_path="/config/ssl/haos-e2e-key.pem",
    )

    assert result == {
        "server_port": 8123,
        "use_x_forwarded_for": True,
        "trusted_proxies": ["172.30.32.0/23"],
        "ip_ban_enabled": True,
        "ssl_certificate": "/config/ssl/haos-e2e-cert.pem",
        "ssl_key": "/config/ssl/haos-e2e-key.pem",
    }


class _FakeWebSocket:
    def __init__(self, result: Any) -> None:
        self.sent: list[dict[str, Any]] = []
        self._frames = iter(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {
                    "id": 1,
                    "type": "result",
                    "success": True,
                    "result": result,
                },
            ]
        )

    def __enter__(self) -> _FakeWebSocket:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def recv(self, timeout: float | None = None) -> str:
        del timeout
        return json.dumps(next(self._frames))


def test_configure_home_assistant_http_requests_in_place_core_restart(
    monkeypatch: Any,
) -> None:
    """The runtime switch uses Core's supported pending-config restart API."""
    assert hasattr(haos_runtime, "configure_home_assistant_http")
    websocket = _FakeWebSocket({"restart": True})
    monkeypatch.setattr(
        "websockets.sync.client.connect",
        lambda *args, **kwargs: websocket,
    )
    config = {"server_port": 8123, "ssl_certificate": "/config/ssl/cert.pem"}

    restarted = haos_runtime.configure_home_assistant_http(
        "http://127.0.0.1:18123", "token", config
    )

    assert restarted is True
    assert websocket.sent == [
        {"type": "auth", "access_token": "token"},
        {
            "id": 1,
            "type": "http/config/configure",
            "config": config,
        },
    ]


def test_get_home_assistant_http_config_reads_stable_slot(monkeypatch: Any) -> None:
    """The runtime switch derives its TLS trial from Core's active store."""
    assert hasattr(haos_runtime, "get_home_assistant_http_config")
    expected = {
        "stable": {"server_port": 8123, "use_x_forwarded_for": True},
        "pending": None,
        "active_config_type": "stable",
    }
    websocket = _FakeWebSocket(expected)
    monkeypatch.setattr(
        "websockets.sync.client.connect",
        lambda *args, **kwargs: websocket,
    )

    result = haos_runtime.get_home_assistant_http_config(
        "http://127.0.0.1:18123", "token"
    )

    assert result == expected
    assert websocket.sent == [
        {"type": "auth", "access_token": "token"},
        {"id": 1, "type": "http/config"},
    ]


def test_promote_home_assistant_http_config_commits_pending_slot(
    monkeypatch: Any,
) -> None:
    """The live TLS/HTTP trial is promoted before its auto-revert deadline."""
    assert hasattr(haos_runtime, "promote_home_assistant_http_config")
    websocket = _FakeWebSocket(None)
    monkeypatch.setattr(
        "websockets.sync.client.connect",
        lambda *args, **kwargs: websocket,
    )

    result = haos_runtime.promote_home_assistant_http_config(
        "https://127.0.0.1:18123",
        "token",
        verify_ssl=False,
    )

    assert result is None
    assert websocket.sent == [
        {"type": "auth", "access_token": "token"},
        {"id": 1, "type": "http/config/promote"},
    ]


def test_move_haos_tls_item_last() -> None:
    """The destructive TLS/Core-restart scenario runs after ordinary work."""
    from tests.src.e2e import conftest as e2e_conftest

    assert hasattr(e2e_conftest, "_move_haos_tls_items_last")

    class Item:
        def __init__(self, name: str, *, tls: bool = False) -> None:
            self.name = name
            self.keywords = {"haos_tls": True} if tls else {}

    items = [Item("first"), Item("tls", tls=True), Item("last")]

    e2e_conftest._move_haos_tls_items_last(items)

    assert [item.name for item in items] == ["first", "last", "tls"]
