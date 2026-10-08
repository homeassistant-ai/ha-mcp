"""pip_compat.pip_kwargs on both Home Assistant ``pip_kwargs`` signatures."""

from __future__ import annotations

from typing import Any

import pytest

from ._embedded_stubs import install

install()

import custom_components.ha_mcp_tools.embedded_server as es  # noqa: E402
from custom_components.ha_mcp_tools import pip_compat  # noqa: E402


def test_embedded_server_uses_the_compat_wrapper() -> None:
    assert es.pip_kwargs is pip_compat.pip_kwargs


def test_legacy_signature_gets_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def legacy(config_dir: str) -> dict[str, Any]:
        seen.append(config_dir)
        return {"target": "/config/deps"}

    monkeypatch.setattr(pip_compat.requirements, "pip_kwargs", legacy)

    assert pip_compat.pip_kwargs("/config") == {"target": "/config/deps"}
    assert seen == ["/config"]


def test_zero_arg_signature_is_called_without_config_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA 2026.11 (home-assistant/core#168155)."""
    monkeypatch.setattr(
        pip_compat.requirements,
        "pip_kwargs",
        lambda: {"constraints": "/c.txt", "timeout": 60},
    )

    assert pip_compat.pip_kwargs("/config") == {"constraints": "/c.txt", "timeout": 60}
