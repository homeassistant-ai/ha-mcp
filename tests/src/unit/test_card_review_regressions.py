"""Human-review regressions reproduced against the live HA runner."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from .test_card_definitions import _definitions, cc, cd, describe_mod
from .test_card_definitions import component as component


def test_custom_wrapper_children_are_not_validated_as_final_configs():
    definitions = _definitions({"tile"}, {"tile": ["entity", "vertical"]})
    config = {
        "views": [
            {
                "cards": [
                    {
                        "type": "custom:config-template-card",
                        "card": {
                            "type": "tile",
                            "entity": '${"light.test"}',
                            "vertical": "${true}",
                        },
                    }
                ]
            }
        ]
    }
    _, checked, _ = definitions._triage(config)
    assert checked == []


def test_empty_frontend_index_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(cd.CardDefinitions, "_index", lambda self: None)
    with pytest.raises(ValueError, match="index"):
        cd.CardDefinitions(tmp_path)


@pytest.mark.asyncio
async def test_refresh_timeout_retains_published_cache(monkeypatch):
    release = asyncio.Event()
    cached = cc.CustomCards("dom")
    cached._card_types = [{"type": "custom:existing"}]

    async def refresh(hass):
        await release.wait()
        return cached

    monkeypatch.setattr(cc, "_async_refresh", refresh)
    monkeypatch.setattr(cc, "_refresh_task", None)
    monkeypatch.setattr(cc, "_custom", cached)
    hass = MagicMock()
    hass.async_create_background_task = lambda coro, name: asyncio.create_task(coro)
    try:
        result = await cc.async_get_custom_cards(hass, timeout=0.01)
        assert result is cached
        assert result.card_types() == [{"type": "custom:existing"}]
    finally:
        release.set()
        await cc._refresh_task


@pytest.mark.asyncio
async def test_blank_describe_type_lists_cards(component):
    component.send_command.return_value = {
        "result": {"success": True, "card_types": [{"type": "tile"}]}
    }
    result = await describe_mod.describe_card_response(MagicMock(), "")
    assert result == {"success": True, "card_types": [{"type": "tile"}]}


@pytest.mark.asyncio
async def test_custom_without_form_does_not_claim_container_semantics(component):
    component.send_command.return_value = {"result": {"success": True, "fields": None}}
    result = await describe_mod.describe_card_response(MagicMock(), "custom:example")
    assert "stack" not in result["note"]
    assert "inspect" in result["note"]


@pytest.mark.asyncio
async def test_builtin_describe_does_not_load_custom_resources(monkeypatch):
    import voluptuous as vol

    custom = AsyncMock(return_value=None)
    definitions = MagicMock()
    definitions.describe.return_value = {"type": "tile", "fields": []}
    monkeypatch.setattr(
        cd, "async_get_definitions", AsyncMock(return_value=definitions)
    )
    monkeypatch.setattr(cd, "async_get_custom_cards", custom)
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    result = await cd.command_specs(vol)[0][2](hass, {"card_type": "tile"})
    assert result["result"]["success"]
    custom.assert_not_awaited()
