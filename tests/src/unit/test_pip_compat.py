"""pip_compat.pip_kwargs on both Home Assistant ``pip_kwargs`` signatures."""

from __future__ import annotations

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import pip_compat  # noqa: E402


def test_legacy_signature_gets_config_dir(monkeypatch):
    seen: list[str] = []

    def legacy(config_dir):
        seen.append(config_dir)
        return {"target": "/config/deps"}

    monkeypatch.setattr(pip_compat.requirements, "pip_kwargs", legacy)

    assert pip_compat.pip_kwargs("/config") == {"target": "/config/deps"}
    assert seen == ["/config"]


def test_zero_arg_signature_is_called_without_config_dir(monkeypatch):
    """HA 2026.11 (home-assistant/core#168155)."""
    monkeypatch.setattr(
        pip_compat.requirements,
        "pip_kwargs",
        lambda: {"constraints": "/c.txt", "timeout": 60},
    )

    assert pip_compat.pip_kwargs("/config") == {"constraints": "/c.txt", "timeout": 60}
