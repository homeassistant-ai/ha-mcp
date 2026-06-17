"""Unit tests for ha_mcp.update_check — the self-update notifier."""

from __future__ import annotations

import httpx
import pytest

from ha_mcp import update_check
from ha_mcp.update_check import (
    UpdateInfo,
    _is_newer,
    get_update_info,
    update_command_hint,
)


def _no_network(package: str) -> str | None:
    raise AssertionError("_fetch_latest_from_pypi should not be called on this path")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """Default every test to: enabled, stable 7.8.0, fresh in-memory memo.

    The real ``is_dev_version`` is left in place (it just inspects the string),
    so a test can flip the channel simply by setting ``get_version``.
    ``SUPERVISOR_TOKEN`` is cleared so the default path is non-add-on (PyPI);
    add-on tests set it explicitly.
    """
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
    def test_is_newer(self, latest: str, current: str, expected: bool) -> None:
        assert _is_newer(latest, current) is expected

    @pytest.mark.parametrize("bad", ["unknown", "garbage", "7.x", ""])
    def test_unparseable_is_never_newer(self, bad: str) -> None:
        assert _is_newer(bad, "7.8.0") is False
        assert _is_newer("7.8.0", bad) is False


class TestGetUpdateInfoGating:
    """No-op (no network) for the excluded cases."""

    def test_disabled_env_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(update_check.DISABLE_ENV, "1")
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", _no_network)
        assert get_update_info() is None

    def test_unknown_version_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(update_check, "get_version", lambda: "unknown")
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", _no_network)
        assert get_update_info() is None

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
    def test_disable_env_parses_falsy_values(
        self, monkeypatch: pytest.MonkeyPatch, val: str, enabled: bool
    ) -> None:
        """HA_MCP_DISABLE_UPDATE_CHECK=0/false/no/off (or blank) keeps the check
        ENABLED — only truthy values disable it, so a user setting =0/=false to
        'keep it on' isn't surprised."""
        monkeypatch.setenv(update_check.DISABLE_ENV, val)
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", lambda _p: "7.9.0")
        result = get_update_info()
        if enabled:
            assert result is not None  # the check ran
        else:
            assert result is None  # disabled

    def test_addon_sources_from_supervisor_not_pypi(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In the add-on the check reads the Supervisor add-on store, never PyPI
        (whose counter is unrelated to the add-on's own version)."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", "hassio-abc")
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", _no_network)
        monkeypatch.setattr(
            update_check,
            "_fetch_supervisor_addon_info",
            lambda: {
                "version": "7.8.0.dev443",
                "version_latest": "7.8.0.dev450",
                "update_available": True,
            },
        )
        info = get_update_info()
        assert info == UpdateInfo("7.8.0.dev443", "7.8.0.dev450", True)


class TestGetUpdateInfoChannels:
    """Stable vs dev: each channel compares against its own PyPI package."""

    def test_stable_install_queries_ha_mcp_and_reports_update(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, str] = {}

        def fetch(package: str) -> str | None:
            captured["package"] = package
            return "7.9.0"

        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", fetch)
        info = get_update_info()
        assert captured["package"] == "ha-mcp"
        assert info == UpdateInfo("7.8.0", "7.9.0", True)

    def test_dev_install_queries_ha_mcp_dev_and_reports_update(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dev parity: a ``.dev`` install compares against the ``ha-mcp-dev``
        package and surfaces newer dev builds (matching the add-on dev channel)."""
        captured: dict[str, str] = {}

        def fetch(package: str) -> str | None:
            captured["package"] = package
            return "7.8.0.dev720"

        monkeypatch.setattr(update_check, "get_version", lambda: "7.8.0.dev714")
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", fetch)
        get_update_info.cache_clear()
        info = get_update_info()
        assert captured["package"] == "ha-mcp-dev"
        assert info == UpdateInfo("7.8.0.dev714", "7.8.0.dev720", True)

    def test_up_to_date_reports_not_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", lambda _p: "7.8.0")
        info = get_update_info()
        assert info is not None and info.update_available is False

    def test_network_failure_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", lambda _p: None)
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

    def test_dev_addon_update_from_supervisor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dev add-on gets a real 'you're on X, Y is out' from the store."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", "hassio-abc")
        monkeypatch.setattr(update_check, "get_version", lambda: "7.8.0.dev443")
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", _no_network)
        monkeypatch.setattr(
            update_check,
            "_fetch_supervisor_addon_info",
            lambda: {
                "version": "7.8.0.dev443",
                "version_latest": "7.8.0.dev450",
                "update_available": True,
            },
        )
        info = get_update_info()
        assert info == UpdateInfo("7.8.0.dev443", "7.8.0.dev450", True)

    def test_addon_up_to_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "hassio-abc")
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", _no_network)
        monkeypatch.setattr(
            update_check,
            "_fetch_supervisor_addon_info",
            lambda: {
                "version": "7.8.1",
                "version_latest": "7.8.1",
                "update_available": False,
            },
        )
        info = get_update_info()
        assert info is not None and info.update_available is False

    def test_addon_supervisor_unreachable_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "hassio-abc")
        monkeypatch.setattr(update_check, "_fetch_supervisor_addon_info", lambda: None)
        assert get_update_info() is None

    def test_addon_disable_env_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The opt-out is honored before any add-on source is consulted."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", "hassio-abc")
        monkeypatch.setenv(update_check.DISABLE_ENV, "1")

        def boom() -> dict | None:
            raise AssertionError("disabled check must not consult Supervisor")

        monkeypatch.setattr(update_check, "_fetch_supervisor_addon_info", boom)
        assert get_update_info() is None

    def test_dev_non_addon_still_uses_pypi(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Docker :dev / pip ha-mcp-dev (a .dev version, NO add-on token) IS a real
        PyPI version, so it stays on the PyPI path against ha-mcp-dev."""
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setattr(update_check, "get_version", lambda: "7.8.0.dev443")
        monkeypatch.setattr(
            update_check, "_fetch_latest_from_pypi", lambda _p: "7.8.0.dev500"
        )
        info = get_update_info()
        assert info is not None and info.update_available is True


class TestFetchSupervisorAddonInfo:
    """The Supervisor /addons/self/info fetch: envelope handling + fail-silent."""

    def test_unwraps_data_envelope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")

        class FakeResp:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                return {
                    "result": "ok",
                    "data": {
                        "version": "1",
                        "version_latest": "2",
                        "update_available": True,
                    },
                }

        monkeypatch.setattr(update_check.httpx, "get", lambda *a, **k: FakeResp())
        assert update_check._fetch_supervisor_addon_info() == {
            "version": "1",
            "version_latest": "2",
            "update_available": True,
        }

    def test_no_token_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        assert update_check._fetch_supervisor_addon_info() is None

    def test_http_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")

        def boom(*a: object, **k: object) -> object:
            raise httpx.ConnectError("no supervisor")

        monkeypatch.setattr(update_check.httpx, "get", boom)
        assert update_check._fetch_supervisor_addon_info() is None


class TestInMemoryMemo:
    """The check runs once per process (memoized), not on every call."""

    def test_memoized_until_cleared(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def fetch(package: str) -> str | None:
            calls.append(package)
            return "7.9.0"

        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", fetch)
        get_update_info()
        get_update_info()
        assert len(calls) == 1  # second call reused the in-memory result
        get_update_info.cache_clear()
        get_update_info()
        assert len(calls) == 2  # re-checked after cache_clear


class TestFetchLatestFromPypi:
    """The PyPI fetch itself — correct package URL, fail-silent on any error."""

    def test_parses_info_version_for_package(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, str] = {}

        class FakeResp:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                return {"info": {"version": "7.8.0.dev720"}}

        def fake_get(url: str, **kwargs: object) -> FakeResp:
            seen["url"] = url
            return FakeResp()

        monkeypatch.setattr(update_check.httpx, "get", fake_get)
        assert update_check._fetch_latest_from_pypi("ha-mcp-dev") == "7.8.0.dev720"
        assert seen["url"] == "https://pypi.org/pypi/ha-mcp-dev/json"

    def test_http_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a: object, **k: object) -> object:
            raise httpx.ConnectError("no network")

        monkeypatch.setattr(update_check.httpx, "get", boom)
        assert update_check._fetch_latest_from_pypi("ha-mcp") is None

    def test_malformed_payload_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeResp:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                return {"info": {}}  # missing "version" -> KeyError -> None

        monkeypatch.setattr(update_check.httpx, "get", lambda *a, **k: FakeResp())
        assert update_check._fetch_latest_from_pypi("ha-mcp") is None


class TestGetUpdateField:
    """The async tool-facing helper: shapes the dict and never raises."""

    async def test_returns_dict_when_update_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(update_check, "_fetch_latest_from_pypi", lambda _p: "7.9.0")
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


class TestUpdateCommandHint:
    """Deployment- and channel-aware upgrade hint."""

    def test_stable_pip_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_check, "_running_in_docker", lambda: False)
        assert update_command_hint("7.9.0") == "Upgrade with: pip install -U ha-mcp."

    def test_dev_pip_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_check, "_running_in_docker", lambda: False)
        assert "pip install -U ha-mcp-dev" in update_command_hint("7.8.0.dev720")

    def test_stable_docker_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_check, "_running_in_docker", lambda: True)
        assert "ha-mcp:stable" in update_command_hint("7.9.0")

    def test_dev_docker_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(update_check, "_running_in_docker", lambda: True)
        assert "ha-mcp:dev" in update_command_hint("7.8.0.dev720")

    def test_addon_hint_points_to_supervisor_ui(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In the add-on, the hint points at the Supervisor UI, not pip/docker."""
        monkeypatch.setattr(update_check, "is_running_in_addon", lambda: True)
        hint = update_command_hint("7.9.0")
        assert "Add-ons" in hint
        assert "pip install" not in hint and "docker pull" not in hint
