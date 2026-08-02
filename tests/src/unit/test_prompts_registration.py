"""Unit tests for the MCP prompts layer.

Two contracts are locked here.

**Every module reaches the server.** ``register_all_prompts`` is the single
entry point ``HomeAssistantSmartMCPServer._initialize_prompts`` calls, and that
call is wrapped in a ``try/except`` that logs and continues — a prompt module
that stopped registering would therefore fail silently in production. The count
check is what makes that failure loud here instead.

**Every prompt still renders.** This is a regression test for a real break: the
prompts were originally written against an older FastMCP that accepted a
``list[PromptMessage]`` return. FastMCP 3.4.4 rejects it at *render* time with
``messages[0] must be Message or str, got PromptMessage`` — so registration and
``list_prompts`` both kept passing while every actual invocation raised. Only
rendering catches it, which is why each prompt is rendered rather than merely
counted.
"""

import pytest
from fastmcp import FastMCP

from ha_mcp.prompts import register_all_prompts

# Minimal valid arguments per prompt. Keyed by name so an added prompt that is
# not listed here fails ``test_every_prompt_renders_a_user_message`` by KeyError
# rather than being silently skipped.
PROMPT_ARGUMENTS: dict[str, dict[str, str]] = {
    "confirm_action": {"action": "turn_off", "entity_id": "light.kitchen"},
    "quiet_hours_check": {"entity_id": "light.kitchen", "purpose": "cleaning"},
    "read_before_write": {"entity_id": "light.kitchen"},
    "bulk_action_review": {"entities": "light.a, light.b", "action": "turn_off"},
    "diagnose_automation": {"automation_id": "automation.morning"},
    "diagnose_entity": {"entity_id": "light.kitchen"},
    "create_automation": {"description": "turn on lights at sunset"},
    "review_automation": {"automation_id": "automation.morning"},
    "system_status": {},
    "area_status": {"area_name": "kitchen"},
    "lock_workflow": {"entity_id": "lock.front_door", "action": "lock"},
    "alarm_workflow": {"entity_id": "alarm_control_panel.home", "action": "arm", "code": "1234"},
}


@pytest.fixture
def server() -> FastMCP:
    mcp = FastMCP("test-prompts")
    register_all_prompts(mcp)
    return mcp


@pytest.mark.asyncio
async def test_all_prompt_modules_register(server: FastMCP) -> None:
    """Every prompt across the five modules is registered exactly once."""
    registered = {prompt.name for prompt in await server.list_prompts()}
    assert registered == set(PROMPT_ARGUMENTS), (
        "prompt registration drifted from the expected set — a module either "
        "stopped registering (silently swallowed by _initialize_prompts) or a "
        "new prompt was added without arguments in PROMPT_ARGUMENTS"
    )


@pytest.mark.asyncio
async def test_every_prompt_renders_a_user_message(server: FastMCP) -> None:
    """Rendering is what catches an unsupported return type, not registration."""
    for prompt in await server.list_prompts():
        result = await server.render_prompt(prompt.name, PROMPT_ARGUMENTS[prompt.name])

        assert len(result.messages) == 1, f"{prompt.name} should yield one message"
        message = result.messages[0]
        assert message.role == "user", f"{prompt.name} should address the user"
        assert message.content.text.strip(), f"{prompt.name} rendered empty text"


@pytest.mark.asyncio
async def test_prompt_arguments_are_interpolated(server: FastMCP) -> None:
    """A prompt echoes its arguments, so callers get entity-specific guidance."""
    result = await server.render_prompt(
        "confirm_action", {"action": "turn_off", "entity_id": "light.kitchen"}
    )

    text = result.messages[0].content.text
    assert "turn_off" in text
    assert "light.kitchen" in text
