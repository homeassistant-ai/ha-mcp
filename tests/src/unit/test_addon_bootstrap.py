"""Unit tests for the ha_mcp_tools add-on bootstrap logic.

Covers the pure slug/repository resolution helpers and the
``async_install_and_start_addon`` orchestration (repository-add, install, and
start sequencing) with a faked Supervisor client. The live Supervisor path
needs a real Home Assistant OS / Supervised host; it is not exercised by CI
(the HAOS tier seeds the config entry via storage rather than driving this
config flow), so the aiohasupervisor method and model names were verified
against the library source instead.

Home Assistant, voluptuous, and aiohasupervisor are mocked in ``sys.modules``
so importing the component package (and the deferred imports inside
``async_install_and_start_addon``) succeeds without a Home Assistant install.
Mirrors the mocking approach in ``test_custom_component_filesystem.py``.
"""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.modules.setdefault("voluptuous", MagicMock())
sys.modules.setdefault("homeassistant", MagicMock())
sys.modules.setdefault("homeassistant.components", MagicMock())
sys.modules.setdefault("homeassistant.components.hassio", MagicMock())
sys.modules.setdefault("homeassistant.components.persistent_notification", MagicMock())
sys.modules.setdefault("homeassistant.config", MagicMock())
sys.modules.setdefault("homeassistant.config_entries", MagicMock())
sys.modules.setdefault("homeassistant.core", MagicMock())
sys.modules.setdefault("homeassistant.helpers", MagicMock())
sys.modules.setdefault("homeassistant.helpers.config_validation", MagicMock())
sys.modules.setdefault("homeassistant.helpers.storage", MagicMock())
sys.modules.setdefault("homeassistant.loader", MagicMock())

_HELPERS_HASSIO = MagicMock()
_HELPERS_HASSIO.is_hassio = MagicMock(return_value=True)
sys.modules["homeassistant.helpers.hassio"] = _HELPERS_HASSIO


class _FakeSupervisorError(Exception):
    """Stand-in for aiohasupervisor.SupervisorError (must be a real Exception)."""


_FAKE_AIOHASUPERVISOR = MagicMock()
_FAKE_AIOHASUPERVISOR.SupervisorError = _FakeSupervisorError
sys.modules.setdefault("aiohasupervisor", _FAKE_AIOHASUPERVISOR)
sys.modules.setdefault("aiohasupervisor.models", MagicMock())

from custom_components.ha_mcp_tools.addon import (  # noqa: E402
    ADDON_REPOSITORY_URL,
    AddonBootstrapError,
    _find_repository,
    _is_running,
    _normalize_repo_url,
    _select_stable_addon,
    async_install_and_start_addon,
)


def _store_addon(slug: str, repository: str = "abcd1234", installed: bool = False):
    return SimpleNamespace(slug=slug, repository=repository, installed=installed)


class TestNormalizeRepoUrl:
    def test_strips_trailing_slash_and_lowercases(self):
        assert (
            _normalize_repo_url("https://github.com/Org/Ha-Mcp/")
            == "https://github.com/org/ha-mcp"
        )

    def test_strips_dot_git(self):
        assert (
            _normalize_repo_url("https://github.com/org/ha-mcp.git")
            == "https://github.com/org/ha-mcp"
        )


class TestFindRepository:
    def test_matches_by_source_ignoring_trailing_slash(self):
        repos = [SimpleNamespace(slug="abcd1234", source=ADDON_REPOSITORY_URL)]
        assert _find_repository(repos, ADDON_REPOSITORY_URL + "/") is repos[0]

    def test_returns_none_when_absent(self):
        repos = [SimpleNamespace(slug="x", source="https://github.com/other/repo")]
        assert _find_repository(repos, ADDON_REPOSITORY_URL) is None

    def test_ignores_repo_without_source(self):
        repos = [SimpleNamespace(slug="x")]
        assert _find_repository(repos, ADDON_REPOSITORY_URL) is None


class TestSelectStableAddon:
    def test_prefers_repo_match_and_stable_suffix(self):
        addons = [
            _store_addon("abcd1234_ha_mcp_dev"),
            _store_addon("abcd1234_ha_mcp"),
            _store_addon("ffff0000_other", repository="ffff0000"),
        ]
        assert _select_stable_addon(addons, "abcd1234").slug == "abcd1234_ha_mcp"

    def test_excludes_dev_addon(self):
        addons = [_store_addon("abcd1234_ha_mcp_dev")]
        assert _select_stable_addon(addons, "abcd1234") is None

    def test_falls_back_to_suffix_when_repo_slug_unknown(self):
        addons = [_store_addon("abcd1234_ha_mcp")]
        assert _select_stable_addon(addons, None).slug == "abcd1234_ha_mcp"

    def test_returns_none_when_repo_known_but_addon_from_other_repo(self):
        # repo_slug is known but the only stable add-on belongs to a different
        # repository, so it must not be selected (no cross-repo fallback).
        addons = [_store_addon("abcd1234_ha_mcp", repository="abcd1234")]
        assert _select_stable_addon(addons, "zzzzzzzz") is None

    def test_returns_none_when_no_stable_addon(self):
        addons = [_store_addon("abcd1234_ha_mcp_dev"), _store_addon("ffff0000_other")]
        assert _select_stable_addon(addons, "abcd1234") is None


class TestIsRunning:
    def test_started_string(self):
        assert _is_running(SimpleNamespace(state="started")) is True

    def test_startup_state_not_running(self):
        # "startup" is a real AddonState that is not yet running.
        assert _is_running(SimpleNamespace(state="startup")) is False

    def test_enum_like_state(self):
        assert (
            _is_running(SimpleNamespace(state=SimpleNamespace(value="started"))) is True
        )

    def test_stopped(self):
        assert _is_running(SimpleNamespace(state="stopped")) is False

    def test_missing_state(self):
        assert _is_running(SimpleNamespace()) is False


def _make_client(*, repo_present, addon, addon_state):
    """Build a fake aiohasupervisor SupervisorClient."""
    repo = SimpleNamespace(slug="abcd1234", source=ADDON_REPOSITORY_URL)
    store = MagicMock()
    if repo_present:
        store.repositories_list = AsyncMock(return_value=[repo])
    else:
        # Empty before the add, populated after add_repository + reload.
        store.repositories_list = AsyncMock(side_effect=[[], [repo]])
    store.add_repository = AsyncMock()
    store.reload = AsyncMock()
    store.addons_list = AsyncMock(return_value=[addon])
    store.install_addon = AsyncMock()

    addons = MagicMock()
    addons.addon_info = AsyncMock(return_value=SimpleNamespace(state=addon_state))
    addons.start_addon = AsyncMock()
    return SimpleNamespace(store=store, addons=addons)


def _patch_supervisor_client(client):
    sys.modules["homeassistant.components.hassio"].get_supervisor_client = MagicMock(
        return_value=client
    )


class TestAsyncInstallAndStartAddon:
    def test_adds_repo_installs_and_starts_when_absent(self):
        addon = _store_addon("abcd1234_ha_mcp", installed=False)
        client = _make_client(repo_present=False, addon=addon, addon_state="stopped")
        _patch_supervisor_client(client)

        asyncio.run(async_install_and_start_addon(object()))

        assert client.store.add_repository.await_count == 1
        assert client.store.reload.await_count == 1
        assert client.store.install_addon.await_count == 1
        assert client.addons.start_addon.await_count == 1

    def test_idempotent_when_already_installed_and_running(self):
        addon = _store_addon("abcd1234_ha_mcp", installed=True)
        client = _make_client(repo_present=True, addon=addon, addon_state="started")
        _patch_supervisor_client(client)

        asyncio.run(async_install_and_start_addon(object()))

        assert client.store.add_repository.await_count == 0
        assert client.store.install_addon.await_count == 0
        # State was checked before skipping the start (not a blind skip).
        assert client.addons.addon_info.await_count == 1
        assert client.addons.start_addon.await_count == 0

    def test_starts_when_installed_but_stopped(self):
        addon = _store_addon("abcd1234_ha_mcp", installed=True)
        client = _make_client(repo_present=True, addon=addon, addon_state="stopped")
        _patch_supervisor_client(client)

        asyncio.run(async_install_and_start_addon(object()))

        assert client.store.install_addon.await_count == 0
        assert client.addons.start_addon.await_count == 1

    def test_raises_when_stable_addon_missing(self):
        addon = _store_addon("abcd1234_ha_mcp_dev", installed=False)
        client = _make_client(repo_present=True, addon=addon, addon_state="stopped")
        _patch_supervisor_client(client)

        with pytest.raises(AddonBootstrapError):
            asyncio.run(async_install_and_start_addon(object()))

    def test_wraps_supervisor_error(self):
        addon = _store_addon("abcd1234_ha_mcp", installed=False)
        client = _make_client(repo_present=True, addon=addon, addon_state="stopped")
        client.store.install_addon = AsyncMock(side_effect=_FakeSupervisorError("boom"))
        _patch_supervisor_client(client)

        with pytest.raises(AddonBootstrapError):
            asyncio.run(async_install_and_start_addon(object()))

    def test_wraps_unexpected_non_supervisor_error(self):
        # A non-SupervisorError (transport/parse error, malformed entry, etc.)
        # must still surface as AddonBootstrapError so the config flow degrades
        # gracefully instead of crashing with no entry created.
        addon = _store_addon("abcd1234_ha_mcp", installed=False)
        client = _make_client(repo_present=True, addon=addon, addon_state="stopped")
        client.store.install_addon = AsyncMock(side_effect=RuntimeError("boom"))
        _patch_supervisor_client(client)

        with pytest.raises(AddonBootstrapError):
            asyncio.run(async_install_and_start_addon(object()))

    def test_succeeds_via_suffix_when_repo_not_refound(self):
        # If the repository cannot be re-found after add + reload, resolution
        # falls back to the slug suffix and still installs + starts the add-on.
        addon = _store_addon("abcd1234_ha_mcp", installed=False)
        client = _make_client(repo_present=False, addon=addon, addon_state="stopped")
        client.store.repositories_list = AsyncMock(return_value=[])
        _patch_supervisor_client(client)

        asyncio.run(async_install_and_start_addon(object()))

        assert client.store.add_repository.await_count == 1
        assert client.store.install_addon.await_count == 1
        assert client.addons.start_addon.await_count == 1

    def test_raises_when_not_supervised(self):
        # Defence in depth: even if invoked without a Supervisor (Container /
        # Core), the bootstrap must refuse clearly rather than misbehave.
        # Patch the live sys.modules entry the lazy import resolves against, so
        # this is robust to another test module restubbing helpers.hassio.
        mod = sys.modules["homeassistant.helpers.hassio"]
        saved = mod.is_hassio
        mod.is_hassio = MagicMock(return_value=False)
        try:
            with pytest.raises(AddonBootstrapError):
                asyncio.run(async_install_and_start_addon(object()))
        finally:
            mod.is_hassio = saved
