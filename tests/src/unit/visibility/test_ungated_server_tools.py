"""Server-text tools are never gated by entity visibility enforce mode (#2949).

``ha_get_skill_guide``, ``ha_search_tools`` and the ``ha_dev_*`` tools return
bundled docs, tool schemas and server controls, whose examples can name
entity_ids that collide with a user's hidden set.
"""

import pytest

from ha_mcp.visibility import resolver

from .test_enforcement import FakeClient, _returns, make_context, make_mw, text_result

_TEXT = "action: light.turn_on\ntarget:\n  entity_id: light.kitchen"
_GUIDE_ARGS = {"file": "references/automation-actions.md"}


@pytest.mark.parametrize(
    "context",
    [
        make_context("ha_get_skill_guide", _GUIDE_ARGS),
        make_context(
            "ha_call_read_tool",
            {"name": "ha_get_skill_guide", "arguments": _GUIDE_ARGS},
        ),
        make_context("ha_search_tools", {"tools": ["ha_call_service"]}),
        make_context("ha_dev_manage_server", {"action": "info"}),
    ],
    ids=["skill_guide", "proxied_skill_guide", "search_tools", "dev_prefix"],
)
async def test_server_text_naming_hidden_entity_is_not_refused(set_config, context):
    set_config(enabled=True, enforce=True, deny_entity_ids=["light.kitchen"])
    client = FakeClient()

    result = await make_mw(get_client=lambda: client).on_call_tool(
        context, _returns(text_result(_TEXT))
    )

    assert result.content[0].text == _TEXT
    assert client.get_states_calls == 0


async def test_skill_guide_is_served_when_visibility_config_cannot_load(
    monkeypatch, tmp_path
):
    def _broken(_d):
        raise ValueError("entity_visibility.json is not valid JSON")

    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(resolver, "load_visibility_config", _broken)

    result = await make_mw(get_client=FakeClient).on_call_tool(
        make_context("ha_get_skill_guide", _GUIDE_ARGS), _returns(text_result(_TEXT))
    )

    assert result.content[0].text == _TEXT
