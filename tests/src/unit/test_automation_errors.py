"""Errors for an automation Home Assistant rejected.

The failed section comes from Core's validation status or its
``validate_config`` verdicts, the closest keys from the platforms Core
registered, and the hints from Core's own rejection text.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools import automation_errors, component_automation
from ha_mcp.tools.automation_errors import (
    REJECTION_PREFIX,
    _text_suggestions,
    closest_matches,
    failed_section_from_core,
    raise_core_rejection,
    section_from_status,
)

from ._component_routing_helpers import make_ws, patch_ws
from .test_component_todo_items import _Client
from .test_ha_call_service_component_routing import (
    _clear_caps_cache,  # noqa: F401 - autouse fixture shared across routing tests
)
from .test_ha_config_automation_component_routing import _NO_CAPS, Client

# --- the failed section ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "section"),
    [
        # Core's automation ValidationStatus failures.
        ("failed_schema", "schema"),
        ("failed_triggers", "triggers"),
        ("failed_conditions", "conditions"),
        ("failed_actions", "actions"),
        ("failed_blueprint", "blueprint"),
        ("unknown", None),
        (None, None),
    ],
)
def test_rejection_names_the_section_cores_status_names(
    status: str | None, section: str | None
) -> None:
    """The caller is told where to look from Core's own status; a status that
    names no failure names no section."""
    assert section_from_status(status) == section


async def test_blueprint_automation_failure_is_the_blueprint() -> None:
    """A blueprint automation has no sections of its own for Core to check,
    so its rejection points at the blueprint without asking Core."""
    client = _Client()
    config = {"use_blueprint": {"path": "motion.yaml", "input": {}}}

    assert await failed_section_from_core(client, config) == "blueprint"
    assert client.sent == []


async def test_legacy_root_keys_are_checked_under_cores_names() -> None:
    """A config written with ``trigger``/``action`` still has its sections
    checked: ``validate_config`` takes only the plural names."""
    triggers = [{"trigger": "state", "entity_id": "light.porch"}]
    actions = [{"delay": {"seconds": 1}}]
    client = _Client(
        {
            "success": True,
            "result": {
                "triggers": {"valid": True, "error": None},
                "actions": {"valid": False, "error": "bad delay"},
            },
        }
    )

    got = await failed_section_from_core(
        client, {"trigger": triggers, "action": actions}
    )

    assert got == "actions"
    assert client.sent == [
        {"type": "validate_config", "triggers": triggers, "actions": actions}
    ]


@pytest.mark.parametrize(
    "client",
    [
        _Client(raise_=HomeAssistantConnectionError("socket closed")),
        _Client({"success": False, "error": "Unknown command."}),
    ],
    ids=["no-answer", "refused"],
)
async def test_no_verdict_from_core_names_no_section(client: _Client) -> None:
    """Without Core's verdicts any section named would be a guess."""
    assert await failed_section_from_core(client, {"triggers": []}) is None


# --- closest keys from the platform subscription ---------------------------------

_UNKNOWN_TRIGGER = "Message malformed: Invalid trigger 'stat' specified"


async def _rejection() -> dict[str, Any]:
    with (
        patch_ws(make_ws("unused", info_result=_NO_CAPS), component_automation),
        pytest.raises(ToolError) as exc,
    ):
        await raise_core_rejection(
            Client(),
            _UNKNOWN_TRIGGER,
            {"triggers": [{"trigger": "stat"}], "actions": []},
            failed_section="triggers",
            context={},
            bp_warnings=[],
        )
    body: dict[str, Any] = json.loads(str(exc.value))
    return body


async def test_platforms_that_never_answer_release_the_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first event that never came left the subscription open on the
    shared socket; the rejection still carries Core's text."""
    monkeypatch.setattr(automation_errors, "_DESCRIPTIONS_TIMEOUT_S", 0.01)
    ws = AsyncMock()
    ws.subscribe_command = AsyncMock(return_value=(7, asyncio.Queue()))

    with patch.object(
        automation_errors, "get_websocket_client", AsyncMock(return_value=ws)
    ):
        body = await _rejection()

    ws.release_subscription.assert_awaited_once_with(7)
    assert body["error"]["message"] == f"{REJECTION_PREFIX}{_UNKNOWN_TRIGGER}"
    assert "closest_matches" not in body


async def test_unreachable_platforms_still_raise_cores_rejection() -> None:
    """Failing to open the socket for the closest keys must not replace
    Core's rejection with a connection error."""
    connect = AsyncMock(side_effect=HomeAssistantConnectionError("refused"))

    with patch.object(automation_errors, "get_websocket_client", connect):
        body = await _rejection()

    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["error"]["message"] == f"{REJECTION_PREFIX}{_UNKNOWN_TRIGGER}"
    assert "closest_matches" not in body


# --- closest keys: the domain first -------------------------------------------------

_SCHEDULE_AND_LOOKALIKES = [
    "climate.turned_on",
    "schedule.block_ended",
    "schedule.block_started",
    "siren.turned_on",
    "switch.turned_on",
]


@pytest.mark.parametrize(
    ("key", "candidates", "expected"),
    [
        # The 2026.7 rename: lookalikes from other domains outscored the
        # replacement, which shares only the domain with the old key.
        (
            "schedule.turned_on",
            _SCHEDULE_AND_LOOKALIKES,
            ["schedule.block_started", "schedule.block_ended"],
        ),
        # A near miss inside a registered domain ranks by similarity.
        (
            "battery.low",
            ["battery.became_low", "battery.level_changed", "gate.closed"],
            ["battery.became_low", "battery.level_changed"],
        ),
        # A misspelt domain registers nothing, so every domain is searched.
        (
            "batery.low",
            ["battery.became_low", "switch.turned_on"],
            ["battery.became_low"],
        ),
        # A key without a domain is searched across every key.
        ("stat", ["state", "tag", "zone"], ["state", "tag"]),
    ],
    ids=["renamed-in-domain", "near-miss-in-domain", "misspelt-domain", "no-domain"],
)
def test_closest_keys_come_from_the_keys_domain_first(
    key: str, candidates: list[str], expected: list[str]
) -> None:
    assert closest_matches(key, candidates) == expected


# --- hints from Core's text -----------------------------------------------------


@pytest.mark.parametrize(
    ("core_text", "names"),
    [
        (
            "Message malformed: Script.__init__() got an unexpected keyword "
            "argument 'triggers'",
            "root",
        ),
        (
            "Message malformed: expected a dictionary for dictionary value "
            "@ data['variables']",
            "variables",
        ),
    ],
    ids=["root-field-in-action-step", "variables-not-a-dict"],
)
def test_rejection_hint_names_what_core_objected_to(core_text: str, names: str) -> None:
    suggestions = _text_suggestions(core_text, {})
    assert len(suggestions) == 1
    assert names in suggestions[0]


def test_service_key_rejection_gets_no_rename_hint() -> None:
    """Core renames ``service`` to ``action`` inside an action step, so a
    rejected ``service`` key sits outside one; telling the caller to rename
    it sent them after the wrong fix."""
    text = "Message malformed: extra keys not allowed @ data['service']"
    assert not any("service:" in s for s in _text_suggestions(text, {}))
