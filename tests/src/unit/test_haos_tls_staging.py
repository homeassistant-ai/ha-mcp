"""Unit coverage for the final same-VM HAOS Core TLS scenario."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.src import haos_runtime


def test_build_home_assistant_tls_config_preserves_runtime_settings() -> None:
    """A pending TLS trial keeps proxy/port settings and drops storage metadata."""
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
    def __init__(
        self, result: Any, *, frames: list[dict[str, Any]] | None = None
    ) -> None:
        self.sent: list[dict[str, Any]] = []
        self._frames = iter(
            frames
            if frames is not None
            else [
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
    websocket = _FakeWebSocket(None)
    monkeypatch.setattr(
        "websockets.sync.client.connect",
        lambda *args, **kwargs: websocket,
    )

    # No result binding: the wrapper is a procedure (it raises on any
    # non-None promote result — see the negative test below).
    haos_runtime.promote_home_assistant_http_config(
        "https://127.0.0.1:18123",
        "token",
        verify_ssl=False,
    )

    assert websocket.sent == [
        {"type": "auth", "access_token": "token"},
        {"id": 1, "type": "http/config/promote"},
    ]


def _patch_ws_connect(monkeypatch: Any, websocket: _FakeWebSocket) -> None:
    monkeypatch.setattr(
        "websockets.sync.client.connect",
        lambda *args, **kwargs: websocket,
    )


def test_ws_command_requires_the_auth_required_handshake(monkeypatch: Any) -> None:
    """A socket that skips auth_required fails loudly instead of proceeding."""
    _patch_ws_connect(monkeypatch, _FakeWebSocket(None, frames=[{"type": "auth_ok"}]))

    with pytest.raises(RuntimeError, match="expected auth_required"):
        haos_runtime.get_home_assistant_http_config("http://127.0.0.1:18123", "token")


def test_ws_command_rejected_auth_fails_loudly(monkeypatch: Any) -> None:
    """An auth_invalid reply never falls through to the command send."""
    _patch_ws_connect(
        monkeypatch,
        _FakeWebSocket(
            None, frames=[{"type": "auth_required"}, {"type": "auth_invalid"}]
        ),
    )

    with pytest.raises(RuntimeError, match="auth rejected"):
        haos_runtime.get_home_assistant_http_config("http://127.0.0.1:18123", "token")


def test_ws_command_unsuccessful_result_fails_loudly(monkeypatch: Any) -> None:
    """A success=False result frame raises with Core's error payload."""
    _patch_ws_connect(
        monkeypatch,
        _FakeWebSocket(
            None,
            frames=[
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {"code": "unknown_command"},
                },
            ],
        ),
    )

    with pytest.raises(RuntimeError, match="unknown_command"):
        haos_runtime.get_home_assistant_http_config("http://127.0.0.1:18123", "token")


def test_get_home_assistant_http_config_rejects_non_object_result(
    monkeypatch: Any,
) -> None:
    """A non-dict http/config result is a contract break, not data."""
    _patch_ws_connect(monkeypatch, _FakeWebSocket("not-a-config"))

    with pytest.raises(RuntimeError, match="non-object result"):
        haos_runtime.get_home_assistant_http_config("http://127.0.0.1:18123", "token")


def test_configure_home_assistant_http_rejects_invalid_result(
    monkeypatch: Any,
) -> None:
    """A configure result without a boolean restart flag fails loudly."""
    _patch_ws_connect(monkeypatch, _FakeWebSocket({"restart": "yes"}))

    with pytest.raises(RuntimeError, match="invalid result"):
        haos_runtime.configure_home_assistant_http(
            "http://127.0.0.1:18123", "token", {"server_port": 8123}
        )


def test_promote_home_assistant_http_config_rejects_unexpected_result(
    monkeypatch: Any,
) -> None:
    """A non-None promote result means the API shape changed underneath us."""
    _patch_ws_connect(monkeypatch, _FakeWebSocket({"promoted": True}))

    with pytest.raises(RuntimeError, match="unexpected result"):
        haos_runtime.promote_home_assistant_http_config(
            "http://127.0.0.1:18123", "token"
        )


def test_move_haos_tls_item_last() -> None:
    """The hook reorders collection so the haos_tls item is last.

    The scheduling consequence (Core restarts only after a worker's ordinary
    work) is documented on ``_move_haos_tls_items_last`` itself.
    """
    from tests.src.e2e import conftest as e2e_conftest

    class Item:
        def __init__(self, name: str, *, tls: bool = False) -> None:
            self.name = name
            self.keywords = {"haos_tls": True} if tls else {}

    items = [Item("first"), Item("tls", tls=True), Item("last")]

    e2e_conftest._move_haos_tls_items_last(items)

    assert [item.name for item in items] == ["first", "last", "tls"]


def test_apply_haos_tls_skip_gates_on_the_embedded_lane() -> None:
    """The TLS scenario is skipped exactly where it is disabled.

    An inverted condition would silently stop the #2241 regression proof from
    running anywhere, under the skip ceilings' radar.
    """
    from tests.src.e2e import conftest as e2e_conftest

    class Item:
        def __init__(self, *, tls: bool) -> None:
            self.keywords = {"haos_tls": True} if tls else {}
            self.markers: list[Any] = []

        def add_marker(self, marker: Any) -> None:
            self.markers.append(marker)

    marker = object()

    disabled = Item(tls=True)
    e2e_conftest._apply_haos_tls_skip(disabled, False, marker)
    assert disabled.markers == [marker]

    enabled = Item(tls=True)
    e2e_conftest._apply_haos_tls_skip(enabled, True, marker)
    assert enabled.markers == []

    ordinary = Item(tls=False)
    e2e_conftest._apply_haos_tls_skip(ordinary, False, marker)
    assert ordinary.markers == []


def test_tls_module_contains_exactly_one_test() -> None:
    """The end-of-queue scheduling guarantee needs a one-test module.

    ``loadscope`` schedules whole modules; a second test here would make the
    TLS scope larger than the other one-test scopes and let the reorder sort
    schedule the destructive Core restart mid-suite.
    """
    import ast
    from pathlib import Path

    module = (
        Path(__file__).parents[1] / "e2e" / "haos_only" / "test_zz_manage_app_tls.py"
    )
    tree = ast.parse(module.read_text())
    test_defs = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    ]
    assert test_defs == ["test_manage_app_reproduces_legacy_tls_failure_then_uses_fix"]
