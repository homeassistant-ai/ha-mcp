"""Unit tests for ha_mcp.update_check — the self-update notifier."""

from __future__ import annotations

import asyncio
import os
import platform
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp import update_check
from ha_mcp.update_check import (
    UpdateInfo,
    get_update_info,
    update_command_hint,
)

from ._core_client import refused_url
from ._tools_addons_core import Supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app
from ._update_check_sources import PyPI, app_store
from ._update_check_sources import pypi as pypi


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """Default every test to: enabled, stable 7.8.0, fresh in-memory memo.

    The real ``is_dev_version`` is left in place (it just inspects the string),
    so a test can flip the channel simply by setting ``get_version``.
    ``SUPERVISOR_TOKEN`` is cleared so the default path is non-add-on (PyPI);
    add-on tests set it explicitly.
    """
    # Runtime simulations belong to update_check, not aiohttp/pytest or
    # other threads sharing the interpreter. Keep real argv and environ.
    monkeypatch.setattr(
        update_check,
        "sys",
        SimpleNamespace(version_info=sys.version_info, argv=sys.argv),
    )
    monkeypatch.setattr(
        update_check,
        "os",
        SimpleNamespace(
            environ=os.environ, path=SimpleNamespace(exists=os.path.exists)
        ),
    )
    monkeypatch.delenv(update_check.DISABLE_ENV, raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(update_check, "get_version", lambda: "7.8.0")
    get_update_info.cache_clear()
    yield
    get_update_info.cache_clear()


class TestVersionCompare:
    """PEP 440 comparison — correctness across both the stable and dev channels."""

    @pytest.mark.parametrize(
        "latest,current,expected",
        [
            ("7.8.1", "7.8.0", True),
            ("7.9.0", "7.8.5", True),
            ("7.10.0", "7.9.0", True),  # numeric, not lexical (".10" > ".9")
            ("7.8.0", "7.8.0", False),
            ("7.8.0", "7.8.1", False),
            # dev channel ordering
            ("7.8.0.dev720", "7.8.0.dev714", True),
            ("7.8.0.dev714", "7.8.0.dev714", False),
            ("7.8.0", "7.8.0.dev714", True),  # final release > its dev builds
            ("7.8.0.dev714", "7.8.0", False),
            ("7.9.0.dev1", "7.8.0", True),  # next minor's dev > prior final
        ],
    )
    async def test_is_newer(
        self, monkeypatch, pypi: PyPI, latest: str, current: str, expected: bool
    ) -> None:
        monkeypatch.setattr(update_check, "get_version", lambda: current)
        package = "ha-mcp-dev" if ".dev" in current else "ha-mcp"
        pypi.publish(package, latest)
        info = await asyncio.to_thread(get_update_info)
        assert info is not None
        assert info.update_available is expected

    @pytest.mark.parametrize("bad", ["unknown", "garbage", "7.x", ""])
    async def test_unparseable_is_never_newer(self, pypi: PyPI, bad: str) -> None:
        pypi.publish("ha-mcp", bad)
        info = await asyncio.to_thread(get_update_info)
        assert info is None or info.update_available is False

    @pytest.mark.parametrize("current", ["unknown", "garbage", "7.x", ""])
    async def test_unparseable_current_version_is_never_newer(
        self, monkeypatch: pytest.MonkeyPatch, pypi: PyPI, current: str
    ) -> None:
        monkeypatch.setattr(update_check, "get_version", lambda: current)
        pypi.publish("ha-mcp", "7.9.0")
        info = await asyncio.to_thread(get_update_info)
        assert info is None or info.update_available is False
        assert pypi.asked == ([] if current == "unknown" else ["ha-mcp"])


class TestGetUpdateInfoGating:
    """No-op (no network) for the excluded cases."""

    async def test_disabled_env_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, pypi: PyPI
    ) -> None:
        monkeypatch.setenv(update_check.DISABLE_ENV, "1")
        pypi.publish("ha-mcp", "7.9.0")
        assert await asyncio.to_thread(get_update_info) is None
        assert pypi.asked == []

    async def test_unknown_version_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, pypi: PyPI
    ) -> None:
        monkeypatch.setattr(update_check, "get_version", lambda: "unknown")
        pypi.publish("ha-mcp", "7.9.0")
        assert await asyncio.to_thread(get_update_info) is None
        assert pypi.asked == []

    @pytest.mark.parametrize(
        "val,enabled",
        [
            ("1", False),
            ("true", False),
            ("TRUE", False),
            ("yes", False),
            ("on", False),
            ("0", True),
            ("false", True),
            ("no", True),
            ("off", True),
            ("   ", True),
        ],
    )
    async def test_disable_env_parses_falsy_values(
        self, monkeypatch: pytest.MonkeyPatch, pypi: PyPI, val: str, enabled: bool
    ) -> None:
        """HA_MCP_DISABLE_UPDATE_CHECK=0/false/no/off (or blank) keeps the check
        ENABLED — only truthy values disable it, so a user setting =0/=false to
        'keep it on' isn't surprised."""
        monkeypatch.setenv(update_check.DISABLE_ENV, val)
        pypi.publish("ha-mcp", "7.9.0")
        result = await asyncio.to_thread(get_update_info)
        if enabled:
            assert result is not None  # the check ran
        else:
            assert result is None  # disabled
            assert pypi.asked == []

    async def test_addon_sources_from_supervisor_not_pypi(
        self, monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor, pypi: PyPI
    ) -> None:
        """In the add-on the check reads the Supervisor add-on store, never PyPI
        (whose counter is unrelated to the add-on's own version)."""
        app_store(
            monkeypatch,
            supervisor,
            installed="7.8.0.dev443",
            latest="7.8.0.dev450",
            update_available=True,
        )
        info = await asyncio.to_thread(get_update_info)
        assert info == UpdateInfo("7.8.0.dev443", "7.8.0.dev450", True)
        assert pypi.asked == []


class TestGetUpdateInfoChannels:
    """Stable vs dev: each channel compares against its own PyPI package."""

    async def test_stable_install_queries_ha_mcp_and_reports_update(
        self, pypi: PyPI
    ) -> None:
        pypi.publish("ha-mcp", "7.9.0")
        info = await asyncio.to_thread(get_update_info)
        assert pypi.asked == ["ha-mcp"]
        assert info == UpdateInfo("7.8.0", "7.9.0", True)

    async def test_dev_install_queries_ha_mcp_dev_and_reports_update(
        self, monkeypatch: pytest.MonkeyPatch, pypi: PyPI
    ) -> None:
        """Dev parity: a ``.dev`` install compares against the ``ha-mcp-dev``
        package and surfaces newer dev builds (matching the add-on dev channel)."""
        pypi.publish("ha-mcp-dev", "7.8.0.dev720")
        monkeypatch.setattr(update_check, "get_version", lambda: "7.8.0.dev714")
        get_update_info.cache_clear()
        info = await asyncio.to_thread(get_update_info)
        assert pypi.asked == ["ha-mcp-dev"]
        assert info == UpdateInfo("7.8.0.dev714", "7.8.0.dev720", True)

    async def test_up_to_date_reports_not_available(self, pypi: PyPI) -> None:
        pypi.publish("ha-mcp", "7.8.0")
        info = await asyncio.to_thread(get_update_info)
        assert info is not None and info.update_available is False

    def test_network_failure_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
    ) -> None:
        monkeypatch.setattr(
            update_check, "PYPI_JSON_URL", refused_url() + "/pypi/{package}/json"
        )
        assert get_update_info() is None

    def test_never_raises_on_unexpected_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contract backstop: an unexpected helper failure degrades to None so
        the unguarded startup-banner call can never break server startup."""

        def boom() -> str:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(update_check, "get_version", boom)
        get_update_info.cache_clear()
        assert get_update_info() is None


class TestAddonSupervisorSource:
    """In the add-on (stable OR dev), the update reference is the Supervisor
    add-on store (same counter as the installed add-on), not PyPI."""

    async def test_dev_addon_update_from_supervisor(
        self, monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor, pypi: PyPI
    ) -> None:
        """Dev add-on gets a real 'you're on X, Y is out' from the store."""
        monkeypatch.setattr(update_check, "get_version", lambda: "7.8.0.dev443")
        app_store(
            monkeypatch,
            supervisor,
            installed="7.8.0.dev443",
            latest="7.8.0.dev450",
            update_available=True,
        )
        info = await asyncio.to_thread(get_update_info)
        assert info == UpdateInfo("7.8.0.dev443", "7.8.0.dev450", True)
        assert pypi.asked == []

    async def test_addon_up_to_date(
        self, monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor, pypi: PyPI
    ) -> None:
        app_store(
            monkeypatch,
            supervisor,
            installed="7.8.1",
            latest="7.8.1",
            update_available=False,
        )
        info = await asyncio.to_thread(get_update_info)
        assert info is not None and info.update_available is False
        assert pypi.asked == []

    def test_addon_supervisor_unreachable_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
    ) -> None:
        monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "hassio-abc")
        monkeypatch.setenv("SUPERVISOR_BASE_URL", refused_url())
        assert get_update_info() is None

    async def test_addon_disable_env_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor, pypi: PyPI
    ) -> None:
        """The opt-out is honored before any add-on source is consulted."""
        app_store(
            monkeypatch,
            supervisor,
            installed="7.8.0",
            latest="7.9.0",
            update_available=True,
        )
        monkeypatch.setenv(update_check.DISABLE_ENV, "1")

        assert await asyncio.to_thread(get_update_info) is None
        assert supervisor.requests == []
        assert pypi.asked == []

    async def test_dev_non_addon_still_uses_pypi(
        self, monkeypatch: pytest.MonkeyPatch, pypi: PyPI
    ) -> None:
        """Docker :dev / pip ha-mcp-dev (a .dev version, NO add-on token) IS a real
        PyPI version, so it stays on the PyPI path against ha-mcp-dev."""
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setattr(update_check, "get_version", lambda: "7.8.0.dev443")
        pypi.publish("ha-mcp-dev", "7.8.0.dev500")
        info = await asyncio.to_thread(get_update_info)
        assert info is not None and info.update_available is True
        assert pypi.asked == ["ha-mcp-dev"]


class TestInMemoryMemo:
    """The check runs once per process (memoized), not on every call."""

    async def test_memoized_until_cleared(self, pypi: PyPI) -> None:
        pypi.publish("ha-mcp", "7.9.0")
        await asyncio.to_thread(get_update_info)
        await asyncio.to_thread(get_update_info)
        assert len(pypi.asked) == 1  # second call reused the in-memory result
        get_update_info.cache_clear()
        await asyncio.to_thread(get_update_info)
        assert len(pypi.asked) == 2  # re-checked after cache_clear


class TestFetchLatestFromPypi:
    async def test_malformed_payload_returns_none(self, pypi: PyPI) -> None:
        pypi.releases["ha-mcp"] = {}
        assert await asyncio.to_thread(get_update_info) is None

    async def test_http_error_returns_none(self, pypi: PyPI) -> None:
        assert await asyncio.to_thread(get_update_info) is None
        assert pypi.asked == ["ha-mcp"]


class TestGetUpdateField:
    """The async tool-facing helper: shapes the dict and never raises."""

    async def test_returns_dict_when_update_available(self, pypi: PyPI) -> None:
        pypi.publish("ha-mcp", "7.9.0")
        field = await update_check.get_update_field()
        assert field == {
            "current": "7.8.0",
            "latest": "7.9.0",
            "update_available": True,
        }

    async def test_returns_none_when_not_applicable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(update_check.DISABLE_ENV, "1")
        assert await update_check.get_update_field() is None

    async def test_swallows_unexpected_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import functools

        # lru_cache so get_update_field's cache_info() check works; it never
        # caches (the call raises), so currsize stays 0 -> the to_thread path
        # runs it and the error is swallowed.
        @functools.lru_cache(maxsize=1)
        def boom() -> UpdateInfo | None:
            raise RuntimeError("boom")

        monkeypatch.setattr(update_check, "get_update_info", boom)
        assert await update_check.get_update_field() is None

    async def test_warm_cache_skips_thread_offload(
        self, monkeypatch: pytest.MonkeyPatch, pypi: PyPI
    ) -> None:
        """Once the memo is warm (currsize > 0 — the common post-startup case),
        get_update_field calls get_update_info directly and must NOT offload to a
        thread (only the cold first call does)."""
        pypi.publish("ha-mcp", "7.9.0")
        # Prime the memo so the warm branch is the one exercised below.
        assert await asyncio.to_thread(update_check.get_update_info) is not None
        assert update_check.get_update_info.cache_info().currsize == 1

        offloaded = False

        async def tracking_to_thread(func: Any) -> Any:
            nonlocal offloaded
            offloaded = True
            return func()

        monkeypatch.setattr(update_check.asyncio, "to_thread", tracking_to_thread)
        field = await update_check.get_update_field()
        assert offloaded is False, "warm cache must not offload to a thread"
        assert field == {
            "current": "7.8.0",
            "latest": "7.9.0",
            "update_available": True,
        }


class TestUpdateCommandHint:
    """Deployment- and channel-aware upgrade hint."""

    def test_stable_pip_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_check.os.path, "exists", lambda path: False)
        assert update_command_hint("7.9.0") == "Upgrade with: pip install -U ha-mcp."

    def test_dev_pip_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_check.os.path, "exists", lambda path: False)
        assert "pip install -U ha-mcp-dev" in update_command_hint("7.8.0.dev720")

    def test_stable_docker_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            update_check.os.path, "exists", lambda path: path == "/.dockerenv"
        )
        assert "ha-mcp:stable" in update_command_hint("7.9.0")

    def test_dev_docker_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            update_check.os.path, "exists", lambda path: path == "/.dockerenv"
        )
        assert "ha-mcp:dev" in update_command_hint("7.8.0.dev720")

    def test_addon_hint_points_to_supervisor_ui(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In the add-on, the hint points at the Supervisor UI, not pip/docker."""
        monkeypatch.setattr(update_check, "is_running_in_addon", lambda: True)
        hint = update_command_hint("7.9.0")
        assert "Settings -> Apps -> Home Assistant MCP Server" in hint
        assert "pip install" not in hint and "docker pull" not in hint


class TestEmbeddedUpdateHint:
    def test_embedded_hint_points_to_hacs(self, monkeypatch):
        # The server arrives with the component release (#2427): no update
        # entity exists and a reload installs nothing. Embedded wins even
        # though the HA core container also carries a SUPERVISOR_TOKEN.
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        hint = update_command_hint("7.11.0")
        assert "HACS" in hint
        assert "update entity" not in hint
        assert "pip install" not in hint


class TestReleaseNeedsNewerPython:
    """pip and uv skip a release whose requires_python excludes the running
    Python, silently; a plain upgrade hint would reinstall the old version."""

    @pytest.fixture(autouse=True)
    def _pip_install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_check.os.path, "exists", lambda path: False)
        monkeypatch.setattr(update_check.sys, "version_info", (3, 13, 7, "final", 0))

    async def test_status_field_names_the_python_and_the_command(
        self, pypi: PyPI
    ) -> None:
        pypi.publish("ha-mcp", "9.2.0", ">=3.14,<3.15")
        field = await update_check.get_update_field()
        assert field is not None
        assert field["python_required"] == "3.14"
        assert field["upgrade_command"] == "uvx --python 3.14 ha-mcp@latest"

    def test_startup_hint_gives_the_uvx_command_not_pip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        info = UpdateInfo("9.1.2", "9.2.0", True, requires_python=">=3.14")
        hint = update_command_hint("9.1.2", info)
        assert "Python 3.14" in hint
        assert "uvx --python 3.14 ha-mcp@latest" in hint
        assert "pip install" not in hint

    def test_other_entry_point_is_kept_in_the_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """uvx runs the script named like the package: ha-mcp is stdio."""
        monkeypatch.setattr(sys, "argv", ["/venv/bin/ha-mcp-web"])
        info = UpdateInfo("9.1.2", "9.2.0", True, requires_python=">=3.14")
        hint = update_command_hint("9.1.2", info)
        assert "uvx --python 3.14 --from ha-mcp@latest ha-mcp-web" in hint

    def test_dev_channel_command_keeps_the_dev_package(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        info = UpdateInfo("9.2.0.dev3", "9.2.0.dev4", True, requires_python=">=3.14")
        hint = update_command_hint("9.2.0.dev3", info)
        assert "uvx --python 3.14 ha-mcp-dev@latest" in hint

    async def test_supported_python_keeps_the_plain_notice(self, pypi: PyPI) -> None:
        pypi.publish("ha-mcp", "9.2.0", ">=3.13")
        field = await update_check.get_update_field()
        assert field == {
            "current": "7.8.0",
            "latest": "9.2.0",
            "update_available": True,
        }

    def test_supported_python_keeps_the_pip_hint(self) -> None:
        info = UpdateInfo("9.1.2", "9.2.0", True, requires_python=">=3.13")
        assert (
            update_command_hint("9.1.2", info) == "Upgrade with: pip install -U ha-mcp."
        )

    async def test_patch_level_floor_names_that_patch(
        self, monkeypatch: pytest.MonkeyPatch, pypi: PyPI
    ) -> None:
        """3.14.1 is excluded by >=3.14.2; naming "3.14" would let uvx reuse it."""
        monkeypatch.setattr(update_check.sys, "version_info", (3, 14, 1, "final", 0))
        pypi.publish("ha-mcp", "9.2.0", ">=3.14.2")
        field = await update_check.get_update_field()
        assert field is not None
        assert field["python_required"] == "3.14.2"
        assert field["upgrade_command"] == "uvx --python 3.14.2 ha-mcp@latest"

    def test_embedded_install_keeps_the_hacs_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its server arrives with the component release, not with uvx."""
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        info = UpdateInfo("9.1.2", "9.2.0", True, requires_python=">=3.14")
        assert update_check.required_python(info) is None
        assert "HACS" in update_command_hint("9.1.2", info)

    def test_invalid_spec_keeps_the_pip_hint(self) -> None:
        info = UpdateInfo("9.1.2", "9.2.0", True, requires_python="not a spec")
        assert update_check.required_python(info) is None

    def test_docker_image_update_still_pulls_the_image(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new image ships its own Python; the pull is still the fix."""
        monkeypatch.setattr(
            update_check.os.path, "exists", lambda path: path == "/.dockerenv"
        )
        info = UpdateInfo("9.1.2", "9.2.0", True, requires_python=">=3.14")
        assert update_check.required_python(info) is None
        assert "docker pull" in update_command_hint("9.1.2", info)

    @pytest.mark.parametrize(
        ("spec", "floor"),
        [
            (">=3.14,<3.15", "3.14"),
            (">=3.14.2", "3.14.2"),
            ("~=3.14.2", "3.14.2"),
            ("==3.14.*", "3.14"),
            (">=3", "3.0"),
            ("<4", None),
            ("not a spec", None),
        ],
    )
    def test_python_floor(self, monkeypatch, spec: str, floor: str | None) -> None:
        monkeypatch.setattr(update_check.sys, "version_info", (2, 7, 0, "final", 0))
        info = UpdateInfo("9.1.2", "9.2.0", True, requires_python=spec)
        assert update_check.required_python(info) == floor


def test_local_build_version_suffix_is_not_read_as_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CPython built from an untagged checkout reports "3.14.0+", which
    packaging reads as outside every specifier."""
    monkeypatch.setattr(update_check.os.path, "exists", lambda path: False)
    monkeypatch.setattr(platform, "python_version", lambda: "3.14.0+")
    major, minor = sys.version_info[:2]
    spec = f">={major}.{minor},<{major}.{minor + 1}"
    info = UpdateInfo("9.1.2", "9.2.0", True, requires_python=spec)
    assert update_check.required_python(info) is None
