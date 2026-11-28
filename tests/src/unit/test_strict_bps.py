"""The strict mandatory best-practices gate (#1779), on a real server.

Covers:

* ``strict_bps_effective``: on only with both flags, read from the settings
  in the environment, and its two fail-open degrades (corrupt settings, a
  best-practices skill the guide cannot serve), each logging one warning per
  process, which a fresh process shows.
* The acknowledgment key (#1924): an attestation phrase whose suffix rotates
  hourly from a per-process salt, and the schema's framing of it as a
  read-receipt.
* The gate, through tool calls on ``HomeAssistantSmartMCPServer`` built on
  ``core_client``: a keyless, wrong, stale or malformed key blocks a gated
  write before it reaches Core; the current or previous hour's key lets it
  through with the key taken out of the arguments the tool is logged with;
  every other tool passes; the gate passes an unregistered gate-map tool
  through and gates when its catalog lookup fails or comes back empty.
* For each gated write, the round trip a model makes: the block error names a
  file, ``ha_get_skill_guide`` serves the key with it, and the write that
  carries the key delivers that file in its ``skill_content``.
* Server wiring: every tool that delivers best-practice content is gated and
  publishes ``BestPracticeKey``; the startup warns when the child flag is on
  and the parent off; ``ha_get_skill_guide`` serves the key only when strict
  mode is in force.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import typing
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

import ha_mcp.server as server_module
from ha_mcp import strict_bps
from ha_mcp._vendor.fastmcp import Client
from ha_mcp._vendor.fastmcp.client.client import CallToolResult
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import reset_global_settings
from ha_mcp.errors import ErrorCode
from ha_mcp.strict_bps import (
    STRICT_BPS_ACK_KEY_PREFIX,
    STRICT_BPS_GATED_TOOLS,
    STRICT_BPS_KEY_PARAM,
    BestPracticeKeyParam,
    StrictBpsMiddleware,
    current_strict_bps_ack_key,
    strict_bps_ack_line,
    strict_bps_effective,
)
from ha_mcp.tools import entity_registration
from ha_mcp.utils.usage_logger import get_recent_logs

from ._config_dir_yaml import setup_from_configuration, write_config_files
from ._core_setup import set_up_tools_entry
from ._real_server import build_server
from ._tools_config_automations_core import require_vendored_skills

Server = server_module.HomeAssistantSmartMCPServer

# The key rotates hourly: the documented period every prose mention promises.
HOUR = 3600
# The start of a day, so also of every hour and of any longer period that
# divides a day: a key that held for more than an hour would hold past it.
BUCKET_START = 1_000_000_000.0 - (1_000_000_000.0 % 86_400)

AUTOMATION = "ha_config_set_automation"
AUTOMATION_CALL: dict[str, Any] = {
    "config": {"alias": "Strict", "triggers": [], "actions": []},
    "wait": False,
}

# One write per gated tool that Core, as ``_gated_core`` sets it up, accepts.
GATED_WRITES: dict[str, dict[str, Any]] = {
    AUTOMATION: AUTOMATION_CALL,
    "ha_config_set_script": {
        "script_id": "strict",
        "config": {"alias": "Strict", "sequence": []},
        "wait": False,
    },
    "ha_config_set_scene": {
        "scene_id": "strict",
        "config": {"name": "Strict", "entities": {}},
        "wait": False,
    },
    "ha_config_set_helper": {
        "helper_type": "input_boolean",
        "name": "Strict",
        "wait": False,
    },
    "ha_config_set_dashboard": {
        "url_path": "strict-gate",
        "title": "Strict",
        "config": {"views": []},
    },
    "ha_config_set_yaml": {
        "yaml_path": "command_line",
        "action": "add",
        "content": "- sensor:\n    name: Uptime\n    command: uptime\n",
    },
}

# ---------------------------------------------------------------------------
# Settings, skills and the clock
# ---------------------------------------------------------------------------


def _set_flags(monkeypatch: pytest.MonkeyPatch, *, parent: bool, child: bool) -> None:
    """Set both flags in the environment the settings are read from."""
    monkeypatch.setenv("ENABLE_MANDATORY_BPS", str(parent).lower())
    monkeypatch.setenv("ENABLE_STRICT_MANDATORY_BPS", str(child).lower())
    reset_global_settings()


def _patch_skills_dir(monkeypatch: pytest.MonkeyPatch, value: Path | None) -> None:
    monkeypatch.setattr("ha_mcp.utils.skill_loader.get_skills_dir", lambda: value)


def _best_practices_skills_dir(tmp_path: Path) -> Path:
    skill = tmp_path / "home-assistant-best-practices"
    skill.mkdir(exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: home-assistant-best-practices\ndescription: Best practices.\n---\n"
        "# Best practices\nReal content here.\n"
    )
    return tmp_path


def _unservable_skills_dir(tmp_path: Path) -> Path:
    """A best-practices skill whose SKILL.md has no frontmatter."""
    skill = tmp_path / "home-assistant-best-practices"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# No frontmatter here\n")
    return tmp_path


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    """Make the key derivation read ``now`` as the time."""
    monkeypatch.setattr(strict_bps, "time", SimpleNamespace(time=lambda: now))


def _key_at(monkeypatch: pytest.MonkeyPatch, now: float) -> str:
    _freeze_clock(monkeypatch, now)
    return current_strict_bps_ack_key()


# ---------------------------------------------------------------------------
# strict_bps_effective
# ---------------------------------------------------------------------------


class TestStrictBpsEffective:
    def test_both_on_is_effective(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _set_flags(monkeypatch, parent=True, child=True)
        _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
        assert strict_bps_effective() is True

    def test_parent_off_is_not_effective(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Child on but parent off ⇒ inert (no config-level cascade)."""
        _set_flags(monkeypatch, parent=False, child=True)
        _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
        assert strict_bps_effective() is False

    def test_child_off_is_not_effective(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _set_flags(monkeypatch, parent=True, child=False)
        _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
        assert strict_bps_effective() is False


# Three gate decisions in a process of their own, which has warned about
# nothing yet; prints each decision and the gate's warnings. The first
# argument, when not empty, is the skills root; the others are settings
# (NAME=value) the running server is given after it started.
_THREE_DECISIONS = """
import json, logging, os, sys
from pathlib import Path
from ha_mcp.config import reset_global_settings
from ha_mcp.strict_bps import strict_bps_effective
from ha_mcp.utils import skill_loader
skills, *settings = sys.argv[1:]
if skills:
    skill_loader.get_skills_dir = lambda: Path(skills)
os.environ.update(setting.split("=", 1) for setting in settings)
reset_global_settings()
warnings = []
class Kept(logging.Handler):
    def emit(self, record):
        warnings.append(record.getMessage())
logging.getLogger("ha_mcp.strict_bps").addHandler(Kept(logging.WARNING))
print(json.dumps([[strict_bps_effective() for _ in range(3)], warnings]))
"""


def _three_decisions_in_a_new_process(
    skills: str, *settings: str
) -> tuple[list[bool], list[str]]:
    shown = subprocess.run(
        [sys.executable, "-c", _THREE_DECISIONS, skills, *settings],
        capture_output=True,
        check=False,
        text=True,
    )
    assert shown.returncode == 0, shown.stderr
    decisions, warnings = json.loads(shown.stdout.splitlines()[-1])
    return decisions, warnings


class TestDegradedGateWarnsOnce:
    """The gate decides on every gated write and every guide read, so a
    degraded install logs why once, not once per write."""

    def test_corrupt_settings_fail_open_with_one_warning(self) -> None:
        """Settings that stop loading while the server runs must not brick
        every gated write."""
        # Not a boolean: loading the settings raises a pydantic ValidationError.
        decisions, warnings = _three_decisions_in_a_new_process(
            "", "ENABLE_MANDATORY_BPS=garbage"
        )

        assert decisions == [False, False, False]
        assert len(warnings) == 1, warnings
        assert "settings lookup failed" in warnings[0]

    def test_an_unservable_skill_fails_open_with_one_warning(
        self, tmp_path: Path
    ) -> None:
        """Without a guide to read the key from, the gate would lock every
        gated write out; it lets them through and says why, once."""
        decisions, warnings = _three_decisions_in_a_new_process(
            str(_unservable_skills_dir(tmp_path)),
            "ENABLE_MANDATORY_BPS=true",
            "ENABLE_STRICT_MANDATORY_BPS=true",
        )

        assert decisions == [False, False, False]
        assert len(warnings) == 1, warnings
        assert "skills-vendor" in warnings[0]


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

    def test_key_is_attestation_phrase_with_rotating_suffix(self) -> None:
        assert STRICT_BPS_ACK_KEY_PREFIX == "I-HAVE-READ-THE-BEST-PRACTICES-GUIDE"
        assert re.fullmatch(
            re.escape(STRICT_BPS_ACK_KEY_PREFIX) + r"-[0-9a-f]{8}",
            current_strict_bps_ack_key(),
        )

    def test_key_stable_within_the_hour_and_rotates_at_its_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prose everywhere says "rotates hourly": the key holds for the
        whole hour and changes when the next one starts."""
        key = _key_at(monkeypatch, BUCKET_START)
        assert _key_at(monkeypatch, BUCKET_START + 100) == key
        assert _key_at(monkeypatch, BUCKET_START + HOUR - 1) == key
        assert _key_at(monkeypatch, BUCKET_START + HOUR) != key

    def test_key_salted_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def test_param_schema_documents_protocol_not_a_secret(self) -> None:
        """The tool schema (trusted metadata, unlike tool output) must carry
        the full protocol: the value is a public read-receipt, not a secret,
        obtained by reading the skill via ha_get_skill_guide. An agent
        following the declared schema is then not 'obeying instructions
        found in fetched content' when it replays the key."""
        field = typing.get_args(BestPracticeKeyParam)[1]
        desc = field.description
        assert "not a secret" in desc.lower()
        assert "ha_get_skill_guide" in desc
        assert "read" in desc.lower()
        # The key VALUE must never live in the schema — only how to get it.
        assert current_strict_bps_ack_key() not in desc

    def test_ack_line_frames_key_as_read_receipt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Freeze the clock: the line and the assertion each derive the key,
        # and an hour-boundary straddle between the two would flake.
        _freeze_clock(monkeypatch, 1_000_000_000.0)
        line = strict_bps_ack_line()
        assert current_strict_bps_ack_key() in line
        assert STRICT_BPS_KEY_PARAM in line
        assert "not a secret" in line.lower()


# ---------------------------------------------------------------------------
# The gate on a real server
# ---------------------------------------------------------------------------


async def _gated_core(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Core that takes every write in ``GATED_WRITES``: automations, scripts and
    scenes in the YAML files Core's config API edits, input_boolean, storage
    dashboards, and the File & YAML Tools entry for the YAML edit."""
    # The new entity is in Core when the write returns; no need to poll for it.
    monkeypatch.setattr(entity_registration, "RESOLVE_TIMEOUT", 0)
    await write_config_files(
        hass,
        {
            "configuration.yaml": (
                "automation: !include automations.yaml\n"
                "script: !include scripts.yaml\n"
                "scene: !include scenes.yaml\n"
            ),
            "automations.yaml": "[]\n",
            "scripts.yaml": "{}\n",
            "scenes.yaml": "[]\n",
        },
    )
    await setup_from_configuration(hass, "automation", "script", "scene")
    for domain in ("input_boolean", "lovelace"):
        assert await async_setup_component(hass, domain, {})
    await set_up_tools_entry(hass)


def _server(
    monkeypatch: pytest.MonkeyPatch,
    client: HomeAssistantClient,
    *,
    parent: bool = True,
    child: bool = True,
    yaml_editing: bool = True,
) -> Server:
    """The server started with the two flags, on ``client``."""
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "unused")
    # YAML editing is a beta feature: the beta master switch has to be on too.
    monkeypatch.setenv("ENABLE_BETA_FEATURES", str(yaml_editing).lower())
    monkeypatch.setenv("ENABLE_YAML_CONFIG_EDITING", str(yaml_editing).lower())
    _set_flags(monkeypatch, parent=parent, child=child)
    return build_server(monkeypatch, client)


@pytest.fixture
async def strict_server(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Server:
    """A server with strict mode in force, on Core that takes every gated write."""
    await _gated_core(hass, monkeypatch)
    _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
    return _server(monkeypatch, core_client)


async def _call(server: Server, tool: str, arguments: dict[str, Any]) -> CallToolResult:
    async with Client(server.mcp) as client:
        return await client.call_tool(tool, arguments, raise_on_error=False)


def _body(result: CallToolResult) -> dict[str, Any]:
    text = getattr(result.content[0], "text", "")
    body: dict[str, Any] = json.loads(text)
    return body


def _blocked(result: CallToolResult) -> dict[str, Any]:
    """The block error's body; fails when the call was not blocked."""
    assert result.is_error, result
    body = _body(result)
    assert body["error"]["code"] == ErrorCode.BPS_ACKNOWLEDGMENT_REQUIRED.value, body
    return body


def _automations(hass: HomeAssistant) -> list[str]:
    return hass.states.async_entity_ids("automation")


def _logged_key(tool: str) -> Any:
    """The key the last logged call of ``tool`` reached the tool with."""
    entry = next(e for e in get_recent_logs(50) if e["tool_name"] == tool)
    return entry["parameters"].get(STRICT_BPS_KEY_PARAM)


class TestTheGateOnTheServer:
    async def test_a_keyless_write_is_blocked_and_told_how_to_get_the_key(
        self, hass: HomeAssistant, strict_server: Server
    ) -> None:
        result = await _call(strict_server, AUTOMATION, AUTOMATION_CALL)

        body = _blocked(result)
        assert body["strict_mandatory_bps"] is True
        assert body["tool_name"] == AUTOMATION
        # The message must say the key rotates and must be refreshed by
        # re-reading — an agent holding an expired key would otherwise
        # replay the stale value in a loop (#1924's failure class).
        message = body["error"]["message"]
        assert "rotates" in message
        assert "re-read" in message
        suggestion = body["error"]["suggestion"]
        assert "ha_get_skill_guide" in suggestion
        assert STRICT_BPS_KEY_PARAM in suggestion
        # The key value must NEVER appear in the block error.
        assert current_strict_bps_ack_key() not in json.dumps(body)
        assert _automations(hass) == []

    async def test_the_block_error_guides_clients_with_a_stale_schema(
        self, strict_server: Server
    ) -> None:
        """The block error pre-arms the model for schema-validating clients
        that reject the BestPracticeKey retry against a stale cached tool
        schema (#1901). That rejection happens client-side — the call never
        reaches the server — so this error is the only server surface that
        can carry the recovery path."""
        result = await _call(strict_server, AUTOMATION, AUTOMATION_CALL)

        suggestions = _blocked(result)["error"]["suggestions"]
        assert len(suggestions) == 2
        # The primary suggestion stays the key-recovery call.
        assert "ha_get_skill_guide" in suggestions[0]
        stale_hint = suggestions[1]
        # Names the client-side error verbatim so the model can match it.
        assert "must NOT have additional properties" in stale_hint
        assert "Developer: Reload Window" in stale_hint

    @pytest.mark.parametrize(
        "key",
        ["not-the-key", {"nested": "object"}],
        ids=["wrong", "not-a-string"],
    )
    async def test_a_write_with_a_key_that_is_not_the_current_one_is_blocked(
        self, hass: HomeAssistant, strict_server: Server, key: Any
    ) -> None:
        """An off-spec client's object key reaches the gate before argument
        validation; it gets the block error, not a crash."""
        result = await _call(
            strict_server, AUTOMATION, {**AUTOMATION_CALL, STRICT_BPS_KEY_PARAM: key}
        )

        _blocked(result)
        assert _automations(hass) == []

    async def test_a_call_with_no_arguments_at_all_is_blocked(
        self, strict_server: Server
    ) -> None:
        async with Client(strict_server.mcp) as client:
            result = await client.session.call_tool("ha_config_set_helper", None)

        assert result.isError, result
        body = json.loads(getattr(result.content[0], "text", ""))
        assert body["error"]["code"] == ErrorCode.BPS_ACKNOWLEDGMENT_REQUIRED.value

    async def test_the_current_key_lets_the_write_through_without_the_key(
        self, hass: HomeAssistant, strict_server: Server
    ) -> None:
        """The gate takes the key out before the tool runs, so it reaches
        neither the tool nor what records its arguments (the policy gate's
        approval hash, the usage log), where an hourly key would churn."""
        result = await _call(
            strict_server,
            AUTOMATION,
            {**AUTOMATION_CALL, STRICT_BPS_KEY_PARAM: current_strict_bps_ack_key()},
        )

        assert not result.is_error, result
        assert _automations(hass) == ["automation.strict"]
        assert _logged_key(AUTOMATION) is None

    async def test_the_key_is_taken_out_with_strict_mode_off_too(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A client that keeps sending the key after strict mode is turned off
        must not leak it downstream either."""
        await _gated_core(hass, monkeypatch)
        server = _server(monkeypatch, core_client, child=False)

        result = await _call(
            server,
            AUTOMATION,
            {**AUTOMATION_CALL, STRICT_BPS_KEY_PARAM: current_strict_bps_ack_key()},
        )

        assert not result.is_error, result
        assert _logged_key(AUTOMATION) is None

    async def test_the_last_hours_key_still_lets_the_write_through(
        self,
        hass: HomeAssistant,
        strict_server: Server,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A key read just before the hourly rotation must not strand the
        agent mid-workflow: the previous hour's key holds for one more hour."""
        previous = _key_at(monkeypatch, BUCKET_START + 100 - HOUR)
        _freeze_clock(monkeypatch, BUCKET_START + 100)

        result = await _call(
            strict_server,
            AUTOMATION,
            {**AUTOMATION_CALL, STRICT_BPS_KEY_PARAM: previous},
        )

        assert not result.is_error, result
        assert _automations(hass) == ["automation.strict"]

    async def test_a_key_two_hours_old_is_blocked(
        self,
        hass: HomeAssistant,
        strict_server: Server,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A key kept in an agent's memory past the grace hour is stale: the
        agent must read the guide again."""
        stale = _key_at(monkeypatch, BUCKET_START + 100 - 2 * HOUR)
        _freeze_clock(monkeypatch, BUCKET_START + 100)

        result = await _call(
            strict_server, AUTOMATION, {**AUTOMATION_CALL, STRICT_BPS_KEY_PARAM: stale}
        )

        _blocked(result)
        assert _automations(hass) == []

    async def test_a_tool_outside_the_gate_needs_no_key(
        self, hass: HomeAssistant, strict_server: Server
    ) -> None:
        hass.states.async_set("light.kitchen", "on")

        result = await _call(
            strict_server, "ha_get_state", {"entity_id": "light.kitchen"}
        )

        assert not result.is_error, result

    async def test_with_strict_mode_off_a_keyless_write_goes_through(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _gated_core(hass, monkeypatch)
        server = _server(monkeypatch, core_client, child=False)

        result = await _call(server, AUTOMATION, AUTOMATION_CALL)

        assert not result.is_error, result
        assert _automations(hass) == ["automation.strict"]

    async def test_an_unservable_skill_lets_a_keyless_write_through(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """With no guide that can publish the key, blocking would lock every
        gated write out with no way back."""
        await _gated_core(hass, monkeypatch)
        _patch_skills_dir(monkeypatch, _unservable_skills_dir(tmp_path))
        server = _server(monkeypatch, core_client)

        result = await _call(server, AUTOMATION, AUTOMATION_CALL)

        assert not result.is_error, result
        assert _automations(hass) == ["automation.strict"]

    @pytest.mark.parametrize(
        ("parent", "child"),
        [(True, True), (False, False), (True, False), (False, True)],
    )
    async def test_gate_engages_whatever_the_flags_were_at_startup(
        self,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        parent: bool,
        child: bool,
    ) -> None:
        """The settings UI turns strict mode on without a restart, so the gate
        is part of every server and a keyless write is blocked once both
        flags are on."""
        _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
        server = _server(monkeypatch, core_client, parent=parent, child=child)
        _set_flags(monkeypatch, parent=True, child=True)

        _blocked(await _call(server, AUTOMATION, AUTOMATION_CALL))


class TestTheGatesToolCatalog:
    """``ha_config_set_yaml`` registers only with YAML editing on; the gate
    checks the catalog so a tool that is not there is not gated."""

    YAML_CALL: typing.ClassVar[dict[str, Any]] = {"yaml_path": "automations.yaml"}

    async def test_an_unregistered_gate_map_tool_is_not_gated(
        self,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        tmp_path: Path,
    ) -> None:
        """Without it the caller is told the tool does not exist, not sent to
        fetch a key for it."""
        _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
        server = _server(monkeypatch, core_client, yaml_editing=False)

        result = await _call(server, "ha_config_set_yaml", self.YAML_CALL)

        assert result.is_error
        text = getattr(result.content[0], "text", "")
        assert ErrorCode.BPS_ACKNOWLEDGMENT_REQUIRED.value not in text
        assert "ha_config_set_yaml" in text

    async def test_a_registered_gate_map_tool_is_gated(
        self,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        tmp_path: Path,
    ) -> None:
        """The catalog check must not weaken the gate for a registered tool."""
        _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
        server = _server(monkeypatch, core_client)

        _blocked(await _call(server, "ha_config_set_yaml", self.YAML_CALL))

    @pytest.mark.parametrize(
        "list_tools",
        [AsyncMock(side_effect=RuntimeError("catalog")), AsyncMock(return_value=[])],
        ids=["lookup-fails", "empty-catalog"],
    )
    async def test_a_catalog_it_cannot_read_makes_the_gate_block(
        self,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        list_tools: AsyncMock,
    ) -> None:
        """Passing a keyless call through on a failed or empty catalog would be
        a gate bypass; gating a tool that turns out not to exist only costs one
        confusing error. The server's own gate passes the unregistered tool
        on; a second gate whose catalog lookup fails or is empty blocks it."""
        _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
        server = _server(monkeypatch, core_client, yaml_editing=False)
        server.mcp.add_middleware(StrictBpsMiddleware(list_tools=list_tools))

        _blocked(await _call(server, "ha_config_set_yaml", self.YAML_CALL))


# ---------------------------------------------------------------------------
# The round trip a model makes, for every gated write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"), GATED_WRITES.items(), ids=list(GATED_WRITES)
)
async def test_the_file_the_block_error_names_comes_with_the_keyed_write(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    """The block error sends the model to one file. The guide serves the key
    with that file, and the write that carries the key delivers the same
    file in its ``skill_content``: the model reads one file, not two."""
    require_vendored_skills()
    await _gated_core(hass, monkeypatch)
    server = _server(monkeypatch, core_client)
    # The guide's key and the gate's check are read a moment apart.
    _freeze_clock(monkeypatch, BUCKET_START + 100)

    suggestion = _blocked(await _call(server, tool, arguments))["error"]["suggestion"]
    named = re.search(r"file='([^']+)'", suggestion)
    assert named is not None, suggestion
    guide = await _call(server, "ha_get_skill_guide", {"file": named[1]})
    assert not guide.is_error, guide
    key = current_strict_bps_ack_key()
    assert key in _body(guide)["content"]
    written = await _call(server, tool, {**arguments, STRICT_BPS_KEY_PARAM: key})

    assert not written.is_error, written
    assert named[1] in _body(written)["skill_content"]


# ---------------------------------------------------------------------------
# Server wiring + inert startup warning
# ---------------------------------------------------------------------------


async def test_every_tool_that_delivers_best_practices_is_gated_and_takes_the_key(
    monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
) -> None:
    """A write tool with ``MandatoryBPS`` that the gate does not know would let
    strict mode write without the read; one whose schema lacks the key would
    have schema-validating clients refuse the retry."""
    server = _server(monkeypatch, core_client)
    async with Client(server.mcp) as client:
        tools = await client.list_tools()

    delivering = {
        t.name: t for t in tools if "MandatoryBPS" in t.inputSchema["properties"]
    }
    assert set(delivering) == set(STRICT_BPS_GATED_TOOLS)
    for name, tool in delivering.items():
        key_param = tool.inputSchema["properties"].get(STRICT_BPS_KEY_PARAM)
        assert key_param is not None, name
        # The shared protocol framing, not a stale per-tool description (#1924).
        assert "not a secret" in key_param["description"].lower(), name
        assert "ha_get_skill_guide" in key_param["description"], name


def test_inert_warning_when_child_on_parent_off(
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
        _server(monkeypatch, core_client, parent=False, child=True)
    assert any("INERT" in r.getMessage() for r in caplog.records)


def test_no_inert_warning_when_both_on(
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
        _server(monkeypatch, core_client, parent=True, child=True)
    assert not any("INERT" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# ha_get_skill_guide acknowledgment-key injection
# ---------------------------------------------------------------------------


async def _skill_guide(
    monkeypatch: pytest.MonkeyPatch,
    client: HomeAssistantClient,
    tmp_path: Path,
    *,
    strict: bool,
) -> dict[str, Any]:
    _patch_skills_dir(monkeypatch, _best_practices_skills_dir(tmp_path))
    server = _server(monkeypatch, client, child=strict)
    result = await _call(server, "ha_get_skill_guide", {})
    assert not result.is_error, result.content
    return _body(result)


class TestSkillGuideKeyInjection:
    async def test_ack_line_prepended_when_strict_effective(
        self,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        tmp_path: Path,
    ) -> None:
        """A model reads the key from the top of the served file; without
        it every gated write stays blocked."""
        # Freeze the clock: content generation and the assertions each
        # derive the key; an hour-boundary straddle between them would flake.
        _freeze_clock(monkeypatch, 1_000_000_000.0)
        result = await _skill_guide(monkeypatch, core_client, tmp_path, strict=True)
        assert result["content"].startswith(strict_bps_ack_line())
        assert current_strict_bps_ack_key() in result["content"]
        assert "Real content here." in result["content"]

    async def test_ack_line_absent_when_strict_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        tmp_path: Path,
    ) -> None:
        result = await _skill_guide(monkeypatch, core_client, tmp_path, strict=False)
        assert result["success"] is True
        assert "Real content here." in result["content"]
        assert current_strict_bps_ack_key() not in result["content"]
