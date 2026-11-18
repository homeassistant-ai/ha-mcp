"""Unit tests for ToolsRegistry registration resilience (issues #1783/#1785).

A single tool module whose import or register function fails must not take the
whole server down: the in-process server can swap the ha-mcp package inside a
running Home Assistant (a pip-spec override), and a mixed old/new module set (or
a module that genuinely needs a newer custom component) previously crashed the
entire server via the registry's fail-fast re-raise. The registry now skips the
failing module, logs it loudly, and keeps every other module's tools available — but a
TOTAL failure (zero modules registered) still raises, because a tool-less
server silently "running" would be worse than a visible start failure.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from importlib.machinery import FileFinder
from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.registry import ToolsRegistry


def _make_registry(
    module_names: list[str], monkeypatch, client: HomeAssistantClient
) -> ToolsRegistry:
    server = SimpleNamespace(
        client=client,
        mcp=FastMCP("registry"),
        smart_tools=None,
        device_tools=None,
    )
    discover = pkgutil.iter_modules

    def modules(paths=None, prefix=""):
        if paths and paths[0].endswith("ha_mcp/tools"):
            return iter(
                pkgutil.ModuleInfo(FileFinder(paths[0]), name, False)
                for name in module_names
            )
        return discover(paths, prefix)

    monkeypatch.setattr(pkgutil, "iter_modules", modules)
    return ToolsRegistry(server, enabled_modules=",".join(module_names))


def _fake_module(name: str, registered: list[str], *, fail: bool = False) -> Any:
    """A stand-in tools module exposing one register_*_tools function."""

    def register(mcp: Any, client: Any, **kwargs: Any) -> None:
        if fail:
            # The live failure shape from issues #1783/#1785: new module code
            # reading a field an older cached Settings instance does not have.
            raise AttributeError("'Settings' object has no attribute 'enable_dev_mode'")
        registered.append(name)

    module = SimpleNamespace()
    setattr(module, f"register_{name}_tools", register)
    return module


@pytest.fixture
def fake_import(monkeypatch):
    """Route the registry's relative tool-module imports to fake modules.

    Returns the dict to populate: ``fake_import["tools_x"] = module``. Names
    not in the dict fall through to the real importer.
    """
    modules: dict[str, Any] = {}
    real_import_module = importlib.import_module

    def fake(name: str, package: str | None = None) -> Any:
        key = name.lstrip(".")
        if key in modules:
            return modules[key]
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", fake)
    return modules


class TestRegisterAllToolsResilience:
    def test_failing_module_is_skipped_and_others_register(
        self, fake_import, monkeypatch, caplog, core_client: HomeAssistantClient
    ):
        registered: list[str] = []
        fake_import["tools_ok_one"] = _fake_module("tools_ok_one", registered)
        fake_import["tools_boom"] = _fake_module("tools_boom", registered, fail=True)
        fake_import["tools_ok_two"] = _fake_module("tools_ok_two", registered)
        registry = _make_registry(
            ["tools_ok_one", "tools_boom", "tools_ok_two"], monkeypatch, core_client
        )

        with caplog.at_level(logging.ERROR):
            registry.register_all_tools()  # must not raise

        assert registered == ["tools_ok_one", "tools_ok_two"]

    def test_skip_summary_names_modules_count_and_restart_hint(
        self, fake_import, monkeypatch, caplog, core_client: HomeAssistantClient
    ):
        # The summary is the only user-facing signal that tools are missing:
        # it must carry the correct count, name EVERY skipped module, and
        # point at the recovery action (restart HA to load a consistent
        # package generation).
        registered: list[str] = []
        fake_import["tools_ok"] = _fake_module("tools_ok", registered)
        fake_import["tools_boom_one"] = _fake_module(
            "tools_boom_one", registered, fail=True
        )
        fake_import["tools_boom_two"] = _fake_module(
            "tools_boom_two", registered, fail=True
        )
        registry = _make_registry(
            ["tools_ok", "tools_boom_one", "tools_boom_two"], monkeypatch, core_client
        )

        with caplog.at_level(logging.ERROR):
            registry.register_all_tools()

        assert "Skipped 2 tool module(s)" in caplog.text
        assert "tools_boom_one, tools_boom_two" in caplog.text
        assert "restart Home Assistant" in caplog.text

    def test_explicit_module_registers_via_its_declared_func_name(
        self, fake_import, monkeypatch, core_client: HomeAssistantClient
    ):
        # EXPLICIT_MODULES entries ("backup") must register exactly once,
        # through their DECLARED function — not the register_*_tools scan. The
        # decoy sorts before the declared name, so if func_name threading were
        # dropped the convention scan would pick the decoy and this fails.
        registered: list[str] = []
        backup = SimpleNamespace()

        def register_backup_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
            registered.append("backup")

        def register_aaa_decoy_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
            registered.append("DECOY")

        backup.register_backup_tools = register_backup_tools
        backup.register_aaa_decoy_tools = register_aaa_decoy_tools
        fake_import["backup"] = backup
        fake_import["tools_ok"] = _fake_module("tools_ok", registered)
        registry = _make_registry(["tools_ok", "backup"], monkeypatch, core_client)

        registry.register_all_tools()

        assert registered == ["tools_ok", "backup"]

    def test_explicit_module_failure_is_skipped(
        self, fake_import, monkeypatch, caplog, core_client: HomeAssistantClient
    ):
        # An EXPLICIT_MODULES entry failing goes through the same containment
        # as convention modules: skipped, named in the summary, others intact.
        registered: list[str] = []
        backup = SimpleNamespace()

        def register_backup_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
            raise RuntimeError("backup registration broke")

        backup.register_backup_tools = register_backup_tools
        fake_import["backup"] = backup
        fake_import["tools_ok"] = _fake_module("tools_ok", registered)
        registry = _make_registry(["tools_ok", "backup"], monkeypatch, core_client)

        with caplog.at_level(logging.ERROR):
            registry.register_all_tools()  # must not raise

        assert registered == ["tools_ok"]
        assert "backup" in caplog.text

    def test_import_failure_is_also_skipped(
        self, fake_import, monkeypatch, caplog, core_client: HomeAssistantClient
    ):
        # A module that cannot even be imported (e.g. it references code that
        # only exists in a newer package generation) is skipped the same way
        # as a failing register function.
        registered: list[str] = []
        fake_import["tools_ok"] = _fake_module("tools_ok", registered)
        registry = _make_registry(
            ["tools_ok", "tools_missing"], monkeypatch, core_client
        )

        def raising_import(name: str, package: str | None = None) -> Any:
            key = name.lstrip(".")
            if key == "tools_missing":
                raise ImportError("No module named 'tools_missing'")
            return fake_import[key]

        # Re-route on top of the fixture: tools_missing raises at import time.
        monkeypatch.setattr(importlib, "import_module", raising_import)

        with caplog.at_level(logging.ERROR):
            registry.register_all_tools()

        assert registered == ["tools_ok"]
        assert "tools_missing" in caplog.text

    def test_all_modules_failing_raises(
        self, fake_import, monkeypatch, core_client: HomeAssistantClient
    ):
        # Zero registered tools = the install is broken outright; the start
        # must fail visibly (repair issue path), not "succeed" tool-less.
        registered: list[str] = []
        fake_import["tools_boom"] = _fake_module("tools_boom", registered, fail=True)
        registry = _make_registry(["tools_boom"], monkeypatch, core_client)

        with pytest.raises(RuntimeError, match="tools_boom"):
            registry.register_all_tools()


class TestModuleSplitExpansion:
    """A legacy ENABLED_TOOL_MODULES name keeps its pre-split tool surface."""

    @pytest.mark.parametrize(
        ("enabled", "expected"),
        [
            ("tools_utility", ["tools_utility", "tools_logs"]),
            ("tools_search", ["tools_search"]),
        ],
    )
    def test_legacy_filter_registers_only_its_own_modules(
        self,
        enabled,
        expected,
        monkeypatch,
        fake_import,
        core_client: HomeAssistantClient,
    ):
        registered: list[str] = []
        names = ["tools_utility", "tools_logs", "tools_search"]
        for name in names:
            fake_import[name] = _fake_module(name, registered)
        registry = _make_registry(names, monkeypatch, core_client)
        registry = ToolsRegistry(registry.server, enabled_modules=enabled)
        registry.register_all_tools()
        assert registered == expected


class TestDevModeSettingsFallback:
    def test_is_dev_mode_enabled_defaults_off_without_field(self, monkeypatch):
        # During an in-process package update an older Settings instance
        # (built before the field existed) can still be the cached singleton;
        # the read must degrade to "dev mode off", never AttributeError
        # (the exact crash in issues #1783/#1785).
        from ha_mcp import config
        from ha_mcp.tools.tools_dev import is_dev_mode_enabled

        monkeypatch.setattr(config, "get_global_settings", SimpleNamespace)

        assert is_dev_mode_enabled() is False
