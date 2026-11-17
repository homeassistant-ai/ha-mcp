"""Unit tests for the strict mandatory best-practices gate (#1779).

Covers:

* ``strict_bps_effective`` truth table + the two fail-open degrade paths
  (settings ValidationError, missing skills-vendor submodule), driven by the
  real settings from the environment.
* ``StrictBpsMiddleware.on_call_tool`` — driven directly with a fake
  ``call_next`` (mirrors ``test_read_only.py``): keyless / wrong-key
  blocks on a gated tool, correct-key passes, non-gated tools pass
  untouched, strict-off passthrough. Asserts the structured error never
  contains the key value.
* Ack-key shape (#1924): attestation prefix, hourly salted-suffix
  rotation with previous-bucket grace, non-string key degradation, and
  the schema-as-protocol framing on ``BestPracticeKeyParam``.
* Wiring: every ``STRICT_BPS_GATED_TOOLS`` tool declares a
  ``BestPracticeKey`` parameter and points its block error at a file the
  bundled skill serves.
* Server wiring, through a real server: the gate engages whatever the flags
  were at startup, passes an unregistered gate-map tool through, and the
  startup warns when the child flag is on but the parent is off.
* Skill-guide injection: ``ha_get_skill_guide`` serves the acknowledgment
  line when strict is effective, and does not when it is off.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import subprocess
import sys
import typing
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import ha_mcp.server as server_module
from ha_mcp import strict_bps
from ha_mcp._vendor.fastmcp import Client
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.config import reset_global_settings
from ha_mcp.errors import ErrorCode
from ha_mcp.strict_bps import (
    STRICT_BPS_ACK_KEY_PREFIX,
    STRICT_BPS_GATED_TOOLS,
    STRICT_BPS_KEY_PARAM,
    StrictBpsMiddleware,
    current_strict_bps_ack_key,
    strict_bps_ack_line,
    strict_bps_effective,
)
from ha_mcp.tools.config_helpers.schemas import HELPER_SKILL_FILES
from ha_mcp.tools.tools_config_automations import AUTOMATION_SKILL_FILES
from ha_mcp.tools.tools_config_dashboards import DASHBOARD_SKILL_FILES
from ha_mcp.tools.tools_config_scenes import SCENE_SKILL_FILES
from ha_mcp.tools.tools_config_scripts import SCRIPT_SKILL_FILES
from ha_mcp.tools.tools_yaml_config import YAML_SKILL_FILES
from ha_mcp.utils.skill_loader import (
    best_practices_skill,
    get_skills_dir,
    list_skill_files,
)

# tool name -> module under ha_mcp.tools that registers it
GATED_TOOL_MODULES: dict[str, str] = {
    "ha_config_set_automation": "tools_config_automations",
    "ha_config_set_script": "tools_config_scripts",
    "ha_config_set_scene": "tools_config_scenes",
    "ha_config_set_helper": "tools_config_helpers",
    "ha_config_set_dashboard": "tools_config_dashboards",
    "ha_config_set_yaml": "tools_yaml_config",
}

# The key rotates hourly: the documented period every prose mention promises.
HOUR = 3600
# The start of a day, so also of every hour and of any longer period that
# divides a day: a key that held for more than an hour would hold past it.
BUCKET_START = 1_000_000_000.0 - (1_000_000_000.0 % 86_400)

# ---------------------------------------------------------------------------
# strict_bps_effective
# ---------------------------------------------------------------------------


def _set_flags(monkeypatch: pytest.MonkeyPatch, *, parent: bool, child: bool) -> None:
    """Set both flags in the environment the settings are read from."""
    monkeypatch.setenv("ENABLE_MANDATORY_BPS", str(parent).lower())
    monkeypatch.setenv("ENABLE_STRICT_MANDATORY_BPS", str(child).lower())
    reset_global_settings()


def _patch_skills_dir(monkeypatch: pytest.MonkeyPatch, value: Path | None) -> None:
    monkeypatch.setattr("ha_mcp.utils.skill_loader.get_skills_dir", lambda: value)


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    """Make the key derivation read ``now`` as the time."""
    monkeypatch.setattr(strict_bps, "time", SimpleNamespace(time=lambda: now))


def _key_at(monkeypatch: pytest.MonkeyPatch, now: float) -> str:
    _freeze_clock(monkeypatch, now)
    return current_strict_bps_ack_key()


class TestStrictBpsEffective:
    def test_both_on_is_effective(self, monkeypatch, tmp_path):
        _set_flags(monkeypatch, parent=True, child=True)
        _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
        assert strict_bps_effective() is True

    def test_parent_off_is_not_effective(self, monkeypatch, tmp_path):
        """Child on but parent off ⇒ inert (no config-level cascade)."""
        _set_flags(monkeypatch, parent=False, child=True)
        _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
        assert strict_bps_effective() is False

    def test_child_off_is_not_effective(self, monkeypatch, tmp_path):
        _set_flags(monkeypatch, parent=True, child=False)
        _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
        assert strict_bps_effective() is False

    def test_validation_error_fails_open(self, monkeypatch, caplog):
        """A corrupt settings env degrades to False, not an exception."""
        # Not a boolean: loading the settings raises a pydantic ValidationError.
        monkeypatch.setenv("ENABLE_MANDATORY_BPS", "garbage")
        reset_global_settings()
        # Warn-once memory is process-wide; start from none warned yet.
        monkeypatch.setattr(strict_bps, "DEGRADE_WARNED", set())
        with caplog.at_level(logging.WARNING, logger="ha_mcp.strict_bps"):
            assert strict_bps_effective() is False
            # Warn-once: a second degraded call must not log again.
            assert strict_bps_effective() is False
        warned = [
            r for r in caplog.records if "settings lookup failed" in r.getMessage()
        ]
        assert len(warned) == 1

    def test_unservable_skill_fails_open(self, monkeypatch, caplog, tmp_path):
        """Both flags on, skills root present, but a best-practices skill the
        guide cannot serve: the key is unobtainable, so gated writes must
        pass instead of deadlocking on a guide that cannot publish it. The
        gate runs on every gated write, so the log must carry one warning,
        not one per write."""
        skill = tmp_path / "home-assistant-best-practices"
        skill.mkdir()
        (skill / "SKILL.md").write_text("# No frontmatter here\n")
        _set_flags(monkeypatch, parent=True, child=True)
        _patch_skills_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(strict_bps, "DEGRADE_WARNED", set())
        with caplog.at_level(logging.WARNING):
            assert strict_bps_effective() is False
            assert strict_bps_effective() is False
            assert strict_bps_effective() is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        assert "skills-vendor" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# Acknowledgment key shape + schema-published protocol framing (#1924)
# ---------------------------------------------------------------------------


class TestAckKeyShape:
    """#1924: a static opaque ``bps-ack-*`` token invited two failure modes —
    an agent misread the extract-and-replay round-trip as a credential
    exfiltration ("prompt injection") and refused it until the user disabled
    strict mode, and a static literal in a public repo (or an agent's own
    persistent memory) can satisfy the gate without any actual read. The key
    is therefore a plain-English attestation phrase whose suffix rotates
    hourly, derived from a per-process salt."""

    def test_key_is_attestation_phrase_with_rotating_suffix(self):
        assert STRICT_BPS_ACK_KEY_PREFIX == "I-HAVE-READ-THE-BEST-PRACTICES-GUIDE"
        assert re.fullmatch(
            re.escape(STRICT_BPS_ACK_KEY_PREFIX) + r"-[0-9a-f]{8}",
            current_strict_bps_ack_key(),
        )

    def test_key_stable_within_the_hour_and_rotates_at_its_end(self, monkeypatch):
        """The prose everywhere says "rotates hourly": the key holds for the
        whole hour and changes when the next one starts."""
        key = _key_at(monkeypatch, BUCKET_START)
        assert _key_at(monkeypatch, BUCKET_START + 100) == key
        assert _key_at(monkeypatch, BUCKET_START + HOUR - 1) == key
        assert _key_at(monkeypatch, BUCKET_START + HOUR) != key

    def test_key_salted_per_process(self, monkeypatch):
        """Without the process salt the suffix is not derivable — a key
        computed by another process (or reconstructed from the public
        formula) for the same moment does not satisfy this process's gate."""
        print_key = (
            "import sys, time; time.time = lambda: float(sys.argv[1]); "
            "from ha_mcp.strict_bps import current_strict_bps_ack_key; "
            "print(current_strict_bps_ack_key())"
        )
        other_process = subprocess.run(
            [sys.executable, "-c", print_key, str(BUCKET_START)],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()

        assert other_process.startswith(STRICT_BPS_ACK_KEY_PREFIX)
        assert other_process != _key_at(monkeypatch, BUCKET_START)

    def test_param_schema_documents_protocol_not_a_secret(self):
        """The tool schema (trusted metadata, unlike tool output) must carry
        the full protocol: the value is a public read-receipt, not a secret,
        obtained by reading the skill via ha_get_skill_guide. An agent
        following the declared schema is then not 'obeying instructions
        found in fetched content' when it replays the key."""
        from ha_mcp.strict_bps import BestPracticeKeyParam

        field = typing.get_args(BestPracticeKeyParam)[1]
        desc = field.description
        assert "not a secret" in desc.lower()
        assert "ha_get_skill_guide" in desc
        assert "read" in desc.lower()
        # The key VALUE must never live in the schema — only how to get it.
        assert current_strict_bps_ack_key() not in desc

    def test_ack_line_frames_key_as_read_receipt(self, monkeypatch):
        # Freeze the clock: the line and the assertion each derive the key,
        # and an hour-boundary straddle between the two would flake.
        _freeze_clock(monkeypatch, 1_000_000_000.0)
        line = strict_bps_ack_line()
        assert current_strict_bps_ack_key() in line
        assert STRICT_BPS_KEY_PARAM in line
        assert "not a secret" in line.lower()


# ---------------------------------------------------------------------------
# StrictBpsMiddleware
# ---------------------------------------------------------------------------


class _FakeMessage:
    """Minimal stand-in for CallToolRequestParams supporting model_copy."""

    def __init__(self, name: str, arguments: dict | None):
        self.name = name
        self.arguments = arguments

    def model_copy(self, update: dict | None = None) -> _FakeMessage:
        fields = {"name": self.name, "arguments": self.arguments, **(update or {})}
        return _FakeMessage(fields["name"], fields["arguments"])


class _FakeContext:
    """Minimal stand-in for MiddlewareContext supporting copy(message=...)."""

    def __init__(self, message: _FakeMessage):
        self.message = message

    def copy(self, **kwargs) -> _FakeContext:
        return _FakeContext(kwargs.get("message", self.message))


def make_context(name: str, arguments: dict | None = None):
    return _FakeContext(_FakeMessage(name, arguments if arguments is not None else {}))


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
        # The message must say the key rotates and must be refreshed by
        # re-reading — an agent holding an expired key would otherwise
        # replay the stale value in a loop (#1924's failure class).
        message = body["error"]["message"]
        assert "rotates" in message
        assert "re-read" in message
        # The key value must NEVER appear in the block error.
        assert current_strict_bps_ack_key() not in raw
        # The suggestion names the exact recovery call for this tool.
        suggestion = body["error"]["suggestion"]
        assert "ha_get_skill_guide" in suggestion
        assert "references/triggers-and-conditions.md" in suggestion
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

    async def test_block_error_guides_stale_schema_clients(self, strict_on):
        """The block error pre-arms the model for schema-validating clients
        that reject the BestPracticeKey retry against a stale cached tool
        schema (#1901). That rejection happens client-side — the call never
        reaches the server — so this error is the only server surface that
        can carry the recovery path."""
        mw = StrictBpsMiddleware()
        call_next = AsyncMock(return_value="ok")
        ctx = make_context("ha_config_set_dashboard", {"url_path": "x"})
        with pytest.raises(ToolError) as excinfo:
            await mw.on_call_tool(ctx, call_next)
        raw = excinfo.value.args[0]
        body = json.loads(raw)
        suggestions = body["error"]["suggestions"]
        assert len(suggestions) == 2
        # The primary suggestion stays the key-recovery call.
        assert "ha_get_skill_guide" in suggestions[0]
        stale_hint = suggestions[1]
        # Names the client-side error verbatim so the model can match it.
        assert "must NOT have additional properties" in stale_hint
        assert "Developer: Reload Window" in stale_hint
        # The key value must still never appear anywhere in the error.
        assert current_strict_bps_ack_key() not in raw

    async def test_previous_rotation_key_accepted_as_grace(
        self, strict_on, monkeypatch
    ):
        """A key obtained just before an hourly rotation must not strand
        the agent mid-workflow — the previous bucket's key stays valid for
        one rotation period."""
        prev_key = _key_at(monkeypatch, BUCKET_START + 100 - HOUR)
        _freeze_clock(monkeypatch, BUCKET_START + 100)
        mw = StrictBpsMiddleware()
        call_next = AsyncMock(return_value="ok")
        ctx = make_context(
            "ha_config_set_automation",
            {"config": {}, "BestPracticeKey": prev_key},
        )
        assert await mw.on_call_tool(ctx, call_next) == "ok"
        call_next.assert_awaited_once()

    async def test_key_two_rotations_old_rejected(self, strict_on, monkeypatch):
        """A key held past the grace window is stale — an agent that saved
        the key to persistent memory must re-read the guide."""
        stale = _key_at(monkeypatch, BUCKET_START + 100 - 2 * HOUR)
        _freeze_clock(monkeypatch, BUCKET_START + 100)
        mw = StrictBpsMiddleware()
        call_next = AsyncMock(return_value="ok")
        ctx = make_context(
            "ha_config_set_automation",
            {"config": {}, "BestPracticeKey": stale},
        )
        with pytest.raises(ToolError):
            await mw.on_call_tool(ctx, call_next)
        call_next.assert_not_awaited()

    async def test_gated_with_correct_key_passes_and_strips_key(self, strict_on):
        mw = StrictBpsMiddleware()
        call_next = AsyncMock(return_value="ok")
        ctx = make_context(
            "ha_config_set_automation",
            {"config": {"alias": "x"}, "BestPracticeKey": current_strict_bps_ack_key()},
        )
        result = await mw.on_call_tool(ctx, call_next)
        assert result == "ok"
        call_next.assert_awaited_once()
        # The gate consumes the key: the forwarded context must not carry it
        # (it would otherwise churn the policy middleware's approval
        # args-hash and leak into downstream logging), while every other
        # argument passes through untouched.
        assert call_next.await_args is not None
        forwarded = call_next.await_args.args[0]
        assert STRICT_BPS_KEY_PARAM not in forwarded.message.arguments
        assert forwarded.message.arguments == {"config": {"alias": "x"}}

    async def test_strict_off_still_strips_stale_key(self, strict_off):
        """A client habitually sending the key while strict is off must not
        leak it downstream either — hash-stable across strict toggles."""
        mw = StrictBpsMiddleware()
        call_next = AsyncMock(return_value="ok")
        ctx = make_context(
            "ha_config_set_automation",
            {"config": {}, "BestPracticeKey": current_strict_bps_ack_key()},
        )
        result = await mw.on_call_tool(ctx, call_next)
        assert result == "ok"
        assert call_next.await_args is not None
        forwarded = call_next.await_args.args[0]
        assert STRICT_BPS_KEY_PARAM not in forwarded.message.arguments

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

    async def test_non_string_key_treated_as_missing(self, strict_on):
        """A malformed (non-string) BestPracticeKey from an off-spec client
        must degrade to the actionable block error, not a TypeError — the
        middleware reads raw transport arguments before pydantic validation,
        and an unhashable value must not crash the set-membership check."""
        mw = StrictBpsMiddleware()
        call_next = AsyncMock(return_value="ok")
        ctx = make_context(
            "ha_config_set_automation",
            {"config": {}, "BestPracticeKey": {"nested": "object"}},
        )
        with pytest.raises(ToolError) as excinfo:
            await mw.on_call_tool(ctx, call_next)
        call_next.assert_not_awaited()
        body = json.loads(excinfo.value.args[0])
        assert body["error"]["code"] == ErrorCode.BPS_ACKNOWLEDGMENT_REQUIRED.value

    async def test_none_arguments_treated_as_empty(self, strict_on):
        """A gated call with arguments=None is blocked, not a crash."""
        mw = StrictBpsMiddleware()
        call_next = AsyncMock(return_value="ok")
        ctx = make_context("ha_config_set_helper", None)
        with pytest.raises(ToolError):
            await mw.on_call_tool(ctx, call_next)
        call_next.assert_not_awaited()

    async def test_unregistered_gated_tool_passes_through(self, strict_on):
        """A gate-map tool absent from the live catalog is NOT gated — the
        caller gets FastMCP's unknown-tool error instead of being sent to
        fetch a key for a tool that doesn't exist (e.g. ha_config_set_yaml
        with yaml editing off)."""
        catalog = [
            SimpleNamespace(name=n)
            for n in STRICT_BPS_GATED_TOOLS
            if n != "ha_config_set_yaml"
        ]
        mw = StrictBpsMiddleware(list_tools=AsyncMock(return_value=catalog))
        call_next = AsyncMock(return_value="ok")
        ctx = make_context("ha_config_set_yaml", {"yaml_path": "automations.yaml"})
        result = await mw.on_call_tool(ctx, call_next)
        assert result == "ok"
        call_next.assert_awaited_once()

    async def test_registered_gated_tool_still_blocked_with_catalog(self, strict_on):
        """The catalog check must not weaken the gate for registered tools."""
        catalog = [SimpleNamespace(name=n) for n in STRICT_BPS_GATED_TOOLS]
        mw = StrictBpsMiddleware(list_tools=AsyncMock(return_value=catalog))
        call_next = AsyncMock(return_value="ok")
        ctx = make_context("ha_config_set_yaml", {"yaml_path": "automations.yaml"})
        with pytest.raises(ToolError):
            await mw.on_call_tool(ctx, call_next)
        call_next.assert_not_awaited()

    async def test_catalog_lookup_failure_gates_conservatively(self, strict_on):
        """A raising catalog lookup must gate (block), never pass a keyless
        call through — pass-through on error would be a gate bypass."""
        mw = StrictBpsMiddleware(list_tools=AsyncMock(side_effect=RuntimeError("x")))
        call_next = AsyncMock(return_value="ok")
        ctx = make_context("ha_config_set_automation", {"config": {}})
        with pytest.raises(ToolError):
            await mw.on_call_tool(ctx, call_next)
        call_next.assert_not_awaited()

    async def test_empty_catalog_gates_conservatively(self, strict_on):
        """An empty catalog is abnormal — gate rather than pass through."""
        mw = StrictBpsMiddleware(list_tools=AsyncMock(return_value=[]))
        call_next = AsyncMock(return_value="ok")
        ctx = make_context("ha_config_set_automation", {"config": {}})
        with pytest.raises(ToolError):
            await mw.on_call_tool(ctx, call_next)
        call_next.assert_not_awaited()


# ---------------------------------------------------------------------------
# Wiring: gated tools declare BestPracticeKey + name a file the skill serves
# ---------------------------------------------------------------------------


def _registered_tools_in_module(module) -> dict[str, object]:
    """Map registered tool name -> bound method for every ``@tool``-decorated
    method on the module's own classes.

    Uses the ``__fastmcp__`` metadata the decorator attaches — the SAME
    registered ``name=`` the live server publishes — so a tool rename that
    forgets ``STRICT_BPS_GATED_TOOLS`` fails here instead of silently
    un-gating the tool (the map keys are matched against
    ``context.message.name`` at call time).
    """
    import inspect

    found: dict[str, object] = {}
    for cls in vars(module).values():
        if not (inspect.isclass(cls) and cls.__module__ == module.__name__):
            continue
        tool_attrs = {
            attr_name: meta.name
            for attr_name, member in vars(cls).items()
            if (meta := getattr(member, "__fastmcp__", None)) is not None
            and getattr(meta, "name", None)
        }
        if not tool_attrs:
            # Skip non-tool classes (TypedDicts etc.) — some don't even
            # support inspect.signature().
            continue
        required = [
            p
            for p in inspect.signature(cls).parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
        instance = cls(*[MagicMock() for _ in required])
        for attr_name, registered_name in tool_attrs.items():
            found[registered_name] = getattr(instance, attr_name)
    return found


def test_gated_tools_set_matches_expected_six():
    assert set(STRICT_BPS_GATED_TOOLS) == set(GATED_TOOL_MODULES)


def test_the_gate_names_the_file_each_tools_own_hint_lists_first() -> None:
    """A tool's write hint and its block error send the model to the same
    file: the gate's map is a second copy of each module's own first entry."""
    assert dict(STRICT_BPS_GATED_TOOLS) == {
        "ha_config_set_automation": AUTOMATION_SKILL_FILES[0],
        "ha_config_set_script": SCRIPT_SKILL_FILES[0],
        "ha_config_set_scene": SCENE_SKILL_FILES[0],
        "ha_config_set_helper": HELPER_SKILL_FILES[0],
        "ha_config_set_dashboard": DASHBOARD_SKILL_FILES[0],
        "ha_config_set_yaml": YAML_SKILL_FILES[0],
    }


@pytest.mark.parametrize("tool_name", sorted(STRICT_BPS_GATED_TOOLS))
def test_gated_tool_declares_key_and_names_a_servable_file(tool_name: str) -> None:
    """The block error sends the model to ``ha_get_skill_guide(file=...)``
    for the key: a file the bundled skill does not serve leaves it nothing
    to read the key from."""
    from ha_mcp._vendor.fastmcp.tools import Tool

    skill = best_practices_skill(get_skills_dir())
    assert skill is not None
    assert STRICT_BPS_GATED_TOOLS[tool_name] in list_skill_files(skill[1])

    module_name = GATED_TOOL_MODULES[tool_name]
    module = importlib.import_module(f"ha_mcp.tools.{module_name}")
    registered = _registered_tools_in_module(module)
    assert tool_name in registered, (
        f"{tool_name} is in STRICT_BPS_GATED_TOOLS but no @tool method in "
        f"{module_name} registers that name — a rename must update the gate map "
        f"or strict mode silently stops covering the tool"
    )

    # Derive the REAL pydantic schema the server publishes for this tool and
    # pin that the acknowledgment kwarg survives derivation with its
    # description — this is the layer schema-validating clients see.
    tool = Tool.from_function(registered[tool_name], name=tool_name)
    props = tool.parameters.get("properties", {})
    assert STRICT_BPS_KEY_PARAM in props, (
        f"{tool_name} must declare {STRICT_BPS_KEY_PARAM} so FastMCP accepts "
        f"the middleware's acknowledgment kwarg and clients can send it"
    )
    # The per-tool derived schema must carry the shared protocol framing —
    # a tool diverging from BestPracticeKeyParam with its own stale
    # description would silently drop the not-a-secret protocol (#1924).
    tool_desc = props[STRICT_BPS_KEY_PARAM].get("description", "")
    assert "not a secret" in tool_desc.lower()
    assert "ha_get_skill_guide" in tool_desc


# ---------------------------------------------------------------------------
# Server wiring + inert startup warning, on a real server
# ---------------------------------------------------------------------------

AUTOMATION_CALL = {"config": {"alias": "x", "triggers": [], "actions": []}}


@asynccontextmanager
async def _no_lifespan(_server: Any) -> AsyncIterator[dict[str, Any]]:
    yield {}


def _server(
    monkeypatch: pytest.MonkeyPatch, *, parent: bool, child: bool
) -> server_module.HomeAssistantSmartMCPServer:
    """A server started with the two flags; its startup checks are left out."""
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "unused")
    monkeypatch.setattr(server_module, "server_lifespan", _no_lifespan)
    _set_flags(monkeypatch, parent=parent, child=child)
    return server_module.HomeAssistantSmartMCPServer()


async def _call(
    server: server_module.HomeAssistantSmartMCPServer,
    tool: str,
    arguments: dict[str, Any],
) -> Any:
    async with Client(server.mcp) as client:
        return await client.call_tool(tool, arguments, raise_on_error=False)


class TestServerWiring:
    @pytest.mark.parametrize(
        ("parent", "child"),
        [(True, True), (False, False), (True, False), (False, True)],
    )
    async def test_gate_engages_whatever_the_flags_were_at_startup(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        parent: bool,
        child: bool,
    ) -> None:
        """The settings UI turns strict mode on without a restart, so the gate
        is part of every server and a keyless write is blocked once both
        flags are on."""
        _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
        server = _server(monkeypatch, parent=parent, child=child)
        _set_flags(monkeypatch, parent=True, child=True)

        result = await _call(server, "ha_config_set_automation", AUTOMATION_CALL)

        assert result.is_error
        assert ErrorCode.BPS_ACKNOWLEDGMENT_REQUIRED.value in result.content[0].text

    async def test_an_unregistered_gate_map_tool_is_not_gated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``ha_config_set_yaml`` registers only with YAML editing on. Without
        it the caller is told the tool does not exist, not sent to fetch a
        key for it."""
        monkeypatch.setenv("ENABLE_YAML_CONFIG_EDITING", "false")
        _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
        server = _server(monkeypatch, parent=True, child=True)

        result = await _call(
            server, "ha_config_set_yaml", {"yaml_path": "automations.yaml"}
        )

        assert result.is_error
        text = result.content[0].text
        assert ErrorCode.BPS_ACKNOWLEDGMENT_REQUIRED.value not in text
        assert "ha_config_set_yaml" in text

    def test_inert_warning_when_child_on_parent_off(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            _server(monkeypatch, parent=False, child=True)
        assert any("INERT" in r.getMessage() for r in caplog.records)

    def test_no_inert_warning_when_both_on(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            _server(monkeypatch, parent=True, child=True)
        assert not any("INERT" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# ha_get_skill_guide Tier-3 acknowledgment-key injection
# ---------------------------------------------------------------------------


def _best_practices_skills_dir(tmp_path: Path) -> Path:
    skill = tmp_path / "home-assistant-best-practices"
    skill.mkdir(exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: best-practices\ndescription: Best practices.\n---\n"
        "# Best practices\nReal content here.\n"
    )
    return tmp_path


async def _skill_guide(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, strict: bool
) -> dict[str, Any]:
    _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
    server = _server(monkeypatch, parent=True, child=strict)
    result = await _call(server, "ha_get_skill_guide", {})
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


class TestSkillGuideKeyInjection:
    async def test_ack_line_prepended_when_strict_effective(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A model reads the key from the top of the served file; without
        it every gated write stays blocked."""
        # Freeze the clock: content generation and the assertions each
        # derive the key; an hour-boundary straddle between them would flake.
        _freeze_clock(monkeypatch, 1_000_000_000.0)
        result = await _skill_guide(monkeypatch, tmp_path, strict=True)
        assert result["content"].startswith(strict_bps_ack_line())
        assert current_strict_bps_ack_key() in result["content"]
        assert "Real content here." in result["content"]

    async def test_ack_line_absent_when_strict_off(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        result = await _skill_guide(monkeypatch, tmp_path, strict=False)
        assert result["success"] is True
        assert "Real content here." in result["content"]
        assert current_strict_bps_ack_key() not in result["content"]
