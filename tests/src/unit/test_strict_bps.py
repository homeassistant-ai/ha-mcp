"""Unit tests for the strict mandatory best-practices gate (#1779).

Covers, without a FastMCP boot:

* ``strict_bps_effective`` truth table + the two fail-open degrade paths
  (settings ValidationError, missing skills-vendor submodule).
* ``StrictBpsMiddleware.on_call_tool`` — driven directly with a fake
  ``call_next`` (mirrors ``test_read_only.py``): keyless / wrong-key
  blocks on a gated tool, correct-key passes, non-gated tools pass
  untouched, strict-off passthrough. Asserts the structured error never
  contains the key literal.
* Wiring: every ``STRICT_BPS_GATED_TOOLS`` tool declares a
  ``BestPracticeKey`` parameter and maps to the FIRST entry of its
  module's canonical ``_*_SKILL_FILES`` constant.
* Server wiring: ``_apply_strict_bps_middleware`` installs the middleware
  regardless of flags and warns when the child flag is on but the parent
  is off (strict mode inert).
* Skill-guide injection: the Tier-3 best-practices content carries the
  acknowledgment line when strict is effective, and does not when it is
  off.
"""

from __future__ import annotations

import ast
import importlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.errors import ErrorCode
from ha_mcp.strict_bps import (
    STRICT_BPS_ACK_KEY,
    STRICT_BPS_GATED_TOOLS,
    STRICT_BPS_KEY_PARAM,
    StrictBpsMiddleware,
    strict_bps_ack_line,
    strict_bps_effective,
)

# tool name -> (module under ha_mcp.tools, canonical _*_SKILL_FILES const)
GATED_TOOL_MODULES: dict[str, tuple[str, str]] = {
    "ha_config_set_automation": ("tools_config_automations", "_AUTOMATION_SKILL_FILES"),
    "ha_config_set_script": ("tools_config_scripts", "_SCRIPT_SKILL_FILES"),
    "ha_config_set_scene": ("tools_config_scenes", "_SCENE_SKILL_FILES"),
    "ha_config_set_helper": ("tools_config_helpers", "_HELPER_SKILL_FILES"),
    "ha_config_set_dashboard": ("tools_config_dashboards", "_DASHBOARD_SKILL_FILES"),
    "ha_config_set_yaml": ("tools_yaml_config", "_YAML_SKILL_FILES"),
}


# ---------------------------------------------------------------------------
# strict_bps_effective
# ---------------------------------------------------------------------------


def _patch_settings(monkeypatch, *, parent: bool, child: bool) -> None:
    monkeypatch.setattr(
        "ha_mcp.config.get_global_settings",
        lambda: SimpleNamespace(
            enable_mandatory_bps=parent, enable_strict_mandatory_bps=child
        ),
    )


def _patch_skills_dir(monkeypatch, value: Path | None) -> None:
    monkeypatch.setattr("ha_mcp.utils.skill_loader.get_skills_dir", lambda: value)


class TestStrictBpsEffective:
    def test_both_on_is_effective(self, monkeypatch, tmp_path):
        _patch_settings(monkeypatch, parent=True, child=True)
        _patch_skills_dir(monkeypatch, tmp_path)
        assert strict_bps_effective() is True

    def test_parent_off_is_not_effective(self, monkeypatch, tmp_path):
        """Child on but parent off ⇒ inert (no config-level cascade)."""
        _patch_settings(monkeypatch, parent=False, child=True)
        _patch_skills_dir(monkeypatch, tmp_path)
        assert strict_bps_effective() is False

    def test_child_off_is_not_effective(self, monkeypatch, tmp_path):
        _patch_settings(monkeypatch, parent=True, child=False)
        _patch_skills_dir(monkeypatch, tmp_path)
        assert strict_bps_effective() is False

    def test_validation_error_fails_open(self, monkeypatch, caplog):
        """A corrupt settings env degrades to False, not an exception."""

        def _boom():
            from ha_mcp.config import Settings

            # Non-boolean coerces to a pydantic ValidationError at load.
            Settings(_env_file=None, ENABLE_MANDATORY_BPS="garbage")  # type: ignore[call-arg]

        monkeypatch.setattr("ha_mcp.config.get_global_settings", _boom)
        with caplog.at_level(logging.WARNING, logger="ha_mcp.strict_bps"):
            assert strict_bps_effective() is False
        assert any("settings lookup failed" in r.getMessage() for r in caplog.records)

    def test_missing_skills_dir_fails_open(self, monkeypatch, caplog):
        """Both flags on but skills-vendor absent ⇒ False (key unobtainable)."""
        _patch_settings(monkeypatch, parent=True, child=True)
        _patch_skills_dir(monkeypatch, None)
        with caplog.at_level(logging.WARNING, logger="ha_mcp.strict_bps"):
            assert strict_bps_effective() is False
        assert any("skills-vendor" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# StrictBpsMiddleware
# ---------------------------------------------------------------------------


def make_context(name: str, arguments: dict | None = None):
    msg = MagicMock()
    msg.name = name
    msg.arguments = arguments if arguments is not None else {}
    ctx = MagicMock()
    ctx.message = msg
    return ctx


@pytest.fixture
def strict_on(monkeypatch):
    monkeypatch.setattr("ha_mcp.strict_bps.strict_bps_effective", lambda: True)


@pytest.fixture
def strict_off(monkeypatch):
    monkeypatch.setattr("ha_mcp.strict_bps.strict_bps_effective", lambda: False)


class TestStrictBpsMiddleware:
    async def test_gated_without_key_blocked(self, strict_on):
        mw = StrictBpsMiddleware()
        call_next = AsyncMock(return_value="ok")
        ctx = make_context("ha_config_set_automation", {"config": {}})
        with pytest.raises(ToolError) as excinfo:
            await mw.on_call_tool(ctx, call_next)
        call_next.assert_not_awaited()

        raw = excinfo.value.args[0]
        body = json.loads(raw)
        assert body["error"]["code"] == ErrorCode.BPS_ACKNOWLEDGMENT_REQUIRED.value
        assert body["strict_mandatory_bps"] is True
        assert body["tool_name"] == "ha_config_set_automation"
        # The key literal must NEVER appear in the block error.
        assert STRICT_BPS_ACK_KEY not in raw
        # The suggestion names the exact recovery call for this tool.
        suggestion = body["error"]["suggestion"]
        assert "ha_get_skill_guide" in suggestion
        assert "references/automation-patterns.md" in suggestion
        assert STRICT_BPS_KEY_PARAM in suggestion

    async def test_gated_with_wrong_key_blocked(self, strict_on):
        mw = StrictBpsMiddleware()
        call_next = AsyncMock(return_value="ok")
        ctx = make_context("ha_config_set_scene", {"BestPracticeKey": "not-the-key"})
        with pytest.raises(ToolError) as excinfo:
            await mw.on_call_tool(ctx, call_next)
        call_next.assert_not_awaited()
        body = json.loads(excinfo.value.args[0])
        assert body["error"]["code"] == ErrorCode.BPS_ACKNOWLEDGMENT_REQUIRED.value
        # scene maps to SKILL.md (its canonical first file).
        assert "SKILL.md" in body["error"]["suggestion"]

    async def test_gated_with_correct_key_passes(self, strict_on):
        mw = StrictBpsMiddleware()
        call_next = AsyncMock(return_value="ok")
        ctx = make_context(
            "ha_config_set_automation", {"BestPracticeKey": STRICT_BPS_ACK_KEY}
        )
        result = await mw.on_call_tool(ctx, call_next)
        assert result == "ok"
        call_next.assert_awaited_once()

    async def test_non_gated_tool_passes_without_key(self, strict_on):
        """A non-gated tool is never blocked, even with strict effective."""
        mw = StrictBpsMiddleware()
        call_next = AsyncMock(return_value="ok")
        ctx = make_context("ha_get_state", {"entity_id": "light.kitchen"})
        result = await mw.on_call_tool(ctx, call_next)
        assert result == "ok"
        call_next.assert_awaited_once()

    async def test_strict_off_passes_gated_keyless(self, strict_off):
        """Strict not effective ⇒ gated tool passes with no key."""
        mw = StrictBpsMiddleware()
        call_next = AsyncMock(return_value="ok")
        ctx = make_context("ha_config_set_automation", {"config": {}})
        result = await mw.on_call_tool(ctx, call_next)
        assert result == "ok"
        call_next.assert_awaited_once()

    async def test_none_arguments_treated_as_empty(self, strict_on):
        """A gated call with arguments=None is blocked, not a crash."""
        mw = StrictBpsMiddleware()
        call_next = AsyncMock(return_value="ok")
        ctx = make_context("ha_config_set_helper", None)
        with pytest.raises(ToolError):
            await mw.on_call_tool(ctx, call_next)
        call_next.assert_not_awaited()


# ---------------------------------------------------------------------------
# Wiring: gated tools declare BestPracticeKey + map their first canonical file
# ---------------------------------------------------------------------------


def _function_params(tree: ast.AST, func_name: str) -> set[str]:
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == func_name
        ):
            args = node.args
            all_args = (
                list(args.args)
                + list(args.kwonlyargs)
                + list(getattr(args, "posonlyargs", []))
            )
            return {a.arg for a in all_args}
    return set()


def test_gated_tools_set_matches_expected_six():
    assert set(STRICT_BPS_GATED_TOOLS) == set(GATED_TOOL_MODULES)


@pytest.mark.parametrize("tool_name", sorted(STRICT_BPS_GATED_TOOLS))
def test_gated_tool_declares_key_and_maps_first_canonical_file(tool_name: str):
    module_name, const_name = GATED_TOOL_MODULES[tool_name]
    module = importlib.import_module(f"ha_mcp.tools.{module_name}")

    canonical_files = getattr(module, const_name)
    assert STRICT_BPS_GATED_TOOLS[tool_name] == canonical_files[0], (
        f"{tool_name} block-error ref {STRICT_BPS_GATED_TOOLS[tool_name]!r} must "
        f"equal {const_name}[0] ({canonical_files[0]!r})"
    )

    assert module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    params = _function_params(tree, tool_name)
    assert STRICT_BPS_KEY_PARAM in params, (
        f"{tool_name} in {module_name} must declare a {STRICT_BPS_KEY_PARAM} "
        f"parameter so FastMCP accepts the middleware's acknowledgment kwarg"
    )


# ---------------------------------------------------------------------------
# Server wiring + inert startup warning
# ---------------------------------------------------------------------------


def _make_server_stub(*, parent: bool, child: bool) -> MagicMock:
    stub = MagicMock()
    stub.settings = MagicMock(
        enable_mandatory_bps=parent, enable_strict_mandatory_bps=child
    )
    stub.mcp = MagicMock()
    return stub


class TestServerWiring:
    def test_middleware_installed_regardless_of_flags(self):
        from ha_mcp.server import HomeAssistantSmartMCPServer

        for parent, child in (
            (True, True),
            (False, False),
            (True, False),
            (False, True),
        ):
            stub = _make_server_stub(parent=parent, child=child)
            HomeAssistantSmartMCPServer._apply_strict_bps_middleware(stub)
            assert stub.mcp.add_middleware.call_count == 1
            args, _kwargs = stub.mcp.add_middleware.call_args
            assert isinstance(args[0], StrictBpsMiddleware)

    def test_inert_warning_when_child_on_parent_off(self, caplog):
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = _make_server_stub(parent=False, child=True)
        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            HomeAssistantSmartMCPServer._apply_strict_bps_middleware(stub)
        assert any("INERT" in r.getMessage() for r in caplog.records)

    def test_no_inert_warning_when_both_on(self, caplog):
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = _make_server_stub(parent=True, child=True)
        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            HomeAssistantSmartMCPServer._apply_strict_bps_middleware(stub)
        assert not any("INERT" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# ha_get_skill_guide Tier-3 acknowledgment-key injection
# ---------------------------------------------------------------------------


def _make_bare_server() -> object:
    from ha_mcp.server import HomeAssistantSmartMCPServer

    srv = HomeAssistantSmartMCPServer.__new__(HomeAssistantSmartMCPServer)
    srv.settings = MagicMock()
    return srv


def _best_practices_skills_dir(tmp_path: Path) -> Path:
    skill = tmp_path / "home-assistant-best-practices"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Best practices\nReal content here.\n")
    return tmp_path


class TestSkillGuideKeyInjection:
    def test_ack_line_prepended_when_strict_effective(self, monkeypatch, tmp_path):
        monkeypatch.setattr("ha_mcp.strict_bps.strict_bps_effective", lambda: True)
        srv = _make_bare_server()
        skills_dir = _best_practices_skills_dir(tmp_path)
        result = srv._handle_skill_guide_call(
            skills_dir, "home-assistant-best-practices", "SKILL.md"
        )
        assert result["success"] is True
        assert result["content"].startswith(strict_bps_ack_line())
        assert STRICT_BPS_ACK_KEY in result["content"]
        # Original body still follows the injected line.
        assert "Real content here." in result["content"]

    def test_ack_line_absent_when_strict_off(self, monkeypatch, tmp_path):
        monkeypatch.setattr("ha_mcp.strict_bps.strict_bps_effective", lambda: False)
        srv = _make_bare_server()
        skills_dir = _best_practices_skills_dir(tmp_path)
        result = srv._handle_skill_guide_call(
            skills_dir, "home-assistant-best-practices", "SKILL.md"
        )
        assert result["success"] is True
        assert STRICT_BPS_ACK_KEY not in result["content"]

    def test_ack_line_absent_for_other_skill_even_if_strict(
        self, monkeypatch, tmp_path
    ):
        """A non-best-practices skill never carries the key, even when strict."""
        monkeypatch.setattr("ha_mcp.strict_bps.strict_bps_effective", lambda: True)
        srv = _make_bare_server()
        other = tmp_path / "some-other-skill"
        other.mkdir()
        (other / "SKILL.md").write_text("# Other\nUnrelated.\n")
        result = srv._handle_skill_guide_call(tmp_path, "some-other-skill", "SKILL.md")
        assert result["success"] is True
        assert STRICT_BPS_ACK_KEY not in result["content"]
