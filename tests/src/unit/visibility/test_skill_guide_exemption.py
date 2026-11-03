"""ha_get_skill_guide is never gated by entity visibility enforce mode (#2949).

It serves only the bundled best-practices docs, whose examples can name
entity_ids that collide with a user's hidden set.
"""

import pytest

from ha_mcp.visibility import resolver

from .test_enforcement import FakeClient, _returns, make_context, make_mw, text_result

_GUIDE_ARGS = {"file": "references/automation-actions.md"}
_GUIDE_TEXT = "action: light.turn_on\ntarget:\n  entity_id: light.kitchen"


def _guide_context(proxied: bool):
    if proxied:
        return make_context(
            "ha_call_read_tool",
            {"name": "ha_get_skill_guide", "arguments": _GUIDE_ARGS},
        )
    return make_context("ha_get_skill_guide", _GUIDE_ARGS)


@pytest.mark.parametrize("proxied", [False, True])
async def test_skill_guide_example_naming_hidden_entity_is_not_refused(
    set_config, proxied
):
    set_config(enabled=True, enforce=True, deny_entity_ids=["light.kitchen"])
    client = FakeClient(fail=True)

    result = await make_mw(get_client=lambda: client).on_call_tool(
        _guide_context(proxied), _returns(text_result(_GUIDE_TEXT))
    )

    assert result.content[0].text == _GUIDE_TEXT
    assert client.get_states_calls == 0


async def test_skill_guide_is_served_when_visibility_config_cannot_load(
    monkeypatch, tmp_path
):
    def _broken(_d):
        raise ValueError("entity_visibility.json is not valid JSON")

    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(resolver, "load_visibility_config", _broken)

    result = await make_mw(get_client=FakeClient).on_call_tool(
        _guide_context(proxied=False), _returns(text_result(_GUIDE_TEXT))
    )

    assert result.content[0].text == _GUIDE_TEXT
