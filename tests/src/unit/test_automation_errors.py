"""Errors for an automation Home Assistant rejected.

The rejection text, the ``validate_config`` verdicts and the trigger keys come
from Core on the real ``hass`` through ``core_client``; the component's own
describe answer comes from ``component_core_client``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.websocket_api import commands as ws_commands
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
)
from ha_mcp.client.websocket_client import get_websocket_client
from ha_mcp.tools import automation_errors
from ha_mcp.tools.automation_errors import (
    REJECTION_PREFIX,
    closest_matches,
    core_rejection_text,
    failed_section_from_core,
    platform_descriptions,
    raise_core_rejection,
    section_from_status,
    text_suggestions,
)

from ._core_client import refused_url

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


async def test_blueprint_automation_failure_is_the_blueprint(
    core_client: HomeAssistantClient,
) -> None:
    """A blueprint automation has no sections of its own for Core to check,
    so its rejection points at the blueprint without asking Core."""
    config = {"use_blueprint": {"path": "motion.yaml", "input": {}}}

    with patch.object(
        core_client,
        "send_websocket_message",
        AsyncMock(wraps=core_client.send_websocket_message),
    ) as ask_core:
        assert await failed_section_from_core(core_client, config) == "blueprint"

    ask_core.assert_not_awaited()


async def test_legacy_root_keys_are_checked_under_cores_names(
    core_client: HomeAssistantClient,
) -> None:
    """A config written with ``trigger``/``action`` still has its sections
    checked: ``validate_config`` takes only the plural names."""
    triggers = [{"trigger": "state", "entity_id": "light.porch"}]
    actions = [{"delay": {"seconds": 1}, "bogus": 1}]

    with patch.object(
        core_client,
        "send_websocket_message",
        AsyncMock(wraps=core_client.send_websocket_message),
    ) as ask_core:
        got = await failed_section_from_core(
            core_client, {"trigger": triggers, "action": actions}
        )

    assert got == "actions"
    ask_core.assert_awaited_once_with(
        {"type": "validate_config", "triggers": triggers, "actions": actions}
    )


@pytest.fixture
async def unreachable_client(
    hass_access_token: str, socket_enabled: None
) -> AsyncIterator[HomeAssistantClient]:
    """A client whose Home Assistant refuses every connection."""
    client = HomeAssistantClient(
        base_url=refused_url(), token=hass_access_token, verify_ssl=False
    )
    try:
        yield client
    finally:
        await client.close()


async def test_no_answer_from_core_names_no_section(
    unreachable_client: HomeAssistantClient,
) -> None:
    """Without Core's verdicts any section named would be a guess."""
    assert await failed_section_from_core(unreachable_client, {"triggers": []}) is None


# --- Core's rejection text --------------------------------------------------------


async def _core_rejection(
    hass: HomeAssistant, core_client: HomeAssistantClient, config: dict[str, Any]
) -> str:
    """Core's own text for ``config``, from the write Core refuses with a 400."""
    assert await async_setup_component(hass, "config", {})
    try:
        await core_client.upsert_automation_config(config, None, _resolved=True)
    except HomeAssistantAPIError as exc:
        text = core_rejection_text(exc)
        assert text is not None
        return text
    pytest.fail("Core accepted a config this test expects it to reject")


async def _rejection(
    client: HomeAssistantClient, core_text: str, config: dict[str, Any]
) -> dict[str, Any]:
    with pytest.raises(ToolError) as exc:
        await raise_core_rejection(
            client,
            core_text,
            config,
            failed_section="triggers",
            context={},
            bp_warnings=[],
        )
    body: dict[str, Any] = json.loads(str(exc.value))
    return body


@pytest.fixture
async def schedule_triggers(hass: HomeAssistant) -> None:
    """Load the integrations whose trigger keys Core describes."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "schedule", {})
    await hass.async_block_till_done()


async def test_rejection_offers_the_registered_trigger_closest_to_the_typo(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    schedule_triggers: None,
) -> None:
    """A trigger key one letter off gets Core's registered key as its match,
    read from the platforms Core describes."""
    config = {
        "alias": "porch",
        "triggers": [{"trigger": "schedule.block_strt"}],
        "actions": [],
    }
    core_text = await _core_rejection(hass, core_client, config)

    body = await _rejection(core_client, core_text, config)

    assert body["error"]["message"] == f"{REJECTION_PREFIX}{core_text}"
    assert body["closest_matches"] == ["schedule.block_started", "schedule.block_ended"]


async def test_component_describe_offers_the_same_closest_trigger(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    schedule_triggers: None,
) -> None:
    """With the component installed its describe command answers the match."""
    config = {
        "alias": "porch",
        "triggers": [{"trigger": "schedule.block_strt"}],
        "actions": [],
    }
    core_text = await _core_rejection(hass, component_core_client, config)

    body = await _rejection(component_core_client, core_text, config)

    assert body["closest_matches"] == ["schedule.block_started", "schedule.block_ended"]


async def test_unreachable_core_still_raises_cores_rejection(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    unreachable_client: HomeAssistantClient,
    schedule_triggers: None,
) -> None:
    """Failing to reach Home Assistant for the closest keys must not replace
    Core's rejection with a connection error."""
    config = {
        "alias": "porch",
        "triggers": [{"trigger": "schedule.block_strt"}],
        "actions": [],
    }
    core_text = await _core_rejection(hass, core_client, config)

    body = await _rejection(unreachable_client, core_text, config)

    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["error"]["message"] == f"{REJECTION_PREFIX}{core_text}"
    assert "closest_matches" not in body


async def test_platforms_that_never_answer_release_the_subscription(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first event that never came left the subscription open on the shared
    socket; the rejection still carries Core's text.

    Core acknowledges the subscription, then holds its trigger descriptions.
    """
    released = asyncio.Event()

    async def descriptions_never_ready(core: HomeAssistant) -> str:
        await released.wait()
        return "{}"

    monkeypatch.setattr(
        ws_commands,
        "_async_get_all_trigger_descriptions_json",
        descriptions_never_ready,
    )
    monkeypatch.setattr(automation_errors, "DESCRIPTIONS_TIMEOUT_S", 0.05)
    ws = await get_websocket_client(
        url=core_client.base_url, token=core_client.token, verify_ssl=False
    )
    monkeypatch.setattr(
        ws, "release_subscription", AsyncMock(wraps=ws.release_subscription)
    )
    config = {"alias": "porch", "triggers": [{"trigger": "stat"}], "actions": []}
    core_text = await _core_rejection(hass, core_client, config)
    try:
        body = await _rejection(core_client, core_text, config)
    finally:
        released.set()
        await hass.async_block_till_done()

    assert body["error"]["message"] == f"{REJECTION_PREFIX}{core_text}"
    assert "closest_matches" not in body
    cast("AsyncMock", ws.release_subscription).assert_awaited_once()


# --- closest keys: the domain first -------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        # A renamed key's replacement lives in the key's own domain.
        ("schedule.turned_on", ["schedule.block_started", "schedule.block_ended"]),
        # A near miss inside a registered domain ranks by similarity.
        (
            "schedule.block_strt",
            ["schedule.block_started", "schedule.block_ended"],
        ),
        # A misspelt domain registers nothing, so every domain is searched.
        (
            "schedul.block_started",
            ["schedule.block_started", "schedule.block_ended"],
        ),
        # A key without a domain is searched across every key.
        (
            "restart_required",
            ["homeassistant.restart_required", "homeassistant.host_reboot_required"],
        ),
        # Nothing in the domain comes close: its other keys are not corrections.
        ("schedule.qwzxvjkpqwzxvjkp", []),
    ],
    ids=[
        "renamed-in-domain",
        "near-miss-in-domain",
        "misspelt-domain",
        "no-domain",
        "nothing-close-in-domain",
    ],
)
async def test_closest_keys_come_from_the_keys_domain_first(
    core_client: HomeAssistantClient,
    schedule_triggers: None,
    key: str,
    expected: list[str],
) -> None:
    keys = sorted(await platform_descriptions(core_client, "trigger") or {})

    assert closest_matches(key, keys) == expected


# --- hints from Core's text -----------------------------------------------------


@pytest.mark.parametrize(
    ("config", "names"),
    [
        (
            {
                "alias": "porch",
                "triggers": [{"trigger": "state", "entity_id": "light.porch"}],
                "actions": [{"triggers": [{"trigger": "state"}]}],
            },
            "root",
        ),
        (
            {
                "alias": "porch",
                "triggers": [{"trigger": "state", "entity_id": "light.porch"}],
                "actions": [],
                "variables": "porch",
            },
            "variables",
        ),
    ],
    ids=["root-field-in-action-step", "variables-not-a-dict"],
)
async def test_rejection_hint_names_what_core_objected_to(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    config: dict[str, Any],
    names: str,
) -> None:
    core_text = await _core_rejection(hass, core_client, config)

    suggestions = text_suggestions(core_text, config)

    assert len(suggestions) == 1
    assert names in suggestions[0]


async def test_a_misspelt_action_gets_no_root_field_hint(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core answers a misspelt step the way it answers a root field inside
    one; only the step that really carries a root field gets that hint."""
    config = {
        "alias": "porch",
        "triggers": [{"trigger": "state", "entity_id": "light.porch"}],
        "actions": [{"delya": {"seconds": 1}}],
    }
    core_text = await _core_rejection(hass, core_client, config)

    assert text_suggestions(core_text, config) == []


async def test_service_key_rejection_gets_no_rename_hint(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core renames ``service`` to ``action`` inside an action step, so a
    rejected ``service`` key sits outside one; telling the caller to rename
    it sent them after the wrong fix. A condition written as a trigger, which
    Core does reject, still gets its hint, so the hints are not simply off."""
    service_config = {
        "alias": "porch",
        "triggers": [{"trigger": "state", "entity_id": "light.porch", "service": "x"}],
        "actions": [],
    }
    service_text = await _core_rejection(hass, core_client, service_config)
    assert not any(
        "service:" in s for s in text_suggestions(service_text, service_config)
    )

    platform_config = {
        "alias": "porch",
        "triggers": [{"trigger": "state", "entity_id": "light.porch"}],
        "conditions": [{"platform": "state", "entity_id": "light.porch"}],
        "actions": [],
    }
    platform_text = await _core_rejection(hass, core_client, platform_config)
    assert any(
        "'platform'" in s for s in text_suggestions(platform_text, platform_config)
    )
