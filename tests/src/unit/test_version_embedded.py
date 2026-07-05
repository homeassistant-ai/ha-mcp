"""Unit tests for embedded-mode detection in ``ha_mcp._version`` (issue #1527).

``_version.py`` is deliberately standalone (it imports only ``os`` / ``logging``
/ ``importlib.metadata``), so it is loaded here in isolation via importlib —
without pulling in the full ``ha_mcp`` package — and exercised purely through the
environment. The load-in-isolation keeps this test in the hermetic local tier.

The key interaction: on HAOS the HA core container carries ``SUPERVISOR_TOKEN``,
so ``is_running_in_addon()`` would wrongly report True in-process. Setting
``HA_MCP_EMBEDDED`` must flip it back to False so add-on-only code paths (direct
Supervisor log fetch, add-on settings routing) do not apply to the embedded
server, which is a plain admin client of HA core.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_VERSION_PATH = (
    pathlib.Path(__file__).resolve().parents[3] / "src" / "ha_mcp" / "_version.py"
)


def _load_version_module():
    spec = importlib.util.spec_from_file_location(
        "_ha_mcp_version_under_test", _VERSION_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def version_mod():
    return _load_version_module()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    yield


class TestIsEmbedded:
    def test_false_when_unset(self, version_mod):
        assert version_mod.is_embedded() is False

    def test_true_when_set(self, version_mod, monkeypatch):
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        assert version_mod.is_embedded() is True


class TestIsRunningInAddon:
    def test_false_with_no_env(self, version_mod):
        assert version_mod.is_running_in_addon() is False

    def test_true_with_supervisor_token_only(self, version_mod, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        assert version_mod.is_running_in_addon() is True

    def test_false_when_embedded_even_with_supervisor_token(
        self, version_mod, monkeypatch
    ):
        # The load-bearing case: HAOS core container has SUPERVISOR_TOKEN, but
        # the in-process server sets HA_MCP_EMBEDDED and must NOT be an add-on.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        assert version_mod.is_embedded() is True
        assert version_mod.is_running_in_addon() is False

    def test_false_when_embedded_without_supervisor_token(
        self, version_mod, monkeypatch
    ):
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        assert version_mod.is_running_in_addon() is False
