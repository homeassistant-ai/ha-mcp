"""Human-review regressions reproduced against the live HA runner."""

import asyncio
import sys
from types import SimpleNamespace
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


def test_skipped_resources_report_coverage_instead_of_claiming_unknown(
    tmp_path, monkeypatch
):
    files = [tmp_path / f"card-{i}.js" for i in range(2)]
    for path in files:
        path.write_text("card")
    monkeypatch.setattr(cc, "_TOTAL_MEMORY", cc._BUNDLE_MEMORY)
    monkeypatch.setattr(
        cc, "_Bundle", lambda *args, **kwargs: MagicMock(cards=[], tags=[])
    )
    custom = cc.CustomCards("dom")
    custom.refresh(files)
    assert custom.status()["state"] == "partial"
    assert custom.status()["resources"] == [
        {"resource": str(files[1]), "reason": "memory limit"}
    ]
    assert cd._custom_warnings(custom, [("card", "custom:unknown", {})]) == []


def test_unregistered_custom_type_is_only_a_resource_inventory_advisory():
    custom = cc.CustomCards("")
    warnings = cd._custom_warnings(custom, [("card", "custom:typo", {})])
    assert "not found in dashboard resources" in warnings[0]
    assert "extra JavaScript" in warnings[0]


def test_failed_refresh_cannot_turn_cached_empty_inventory_into_unknown_warning(
    monkeypatch,
):
    custom = cc.CustomCards("")
    monkeypatch.setattr(cc, "_resource_error", "DOM download failed")
    monkeypatch.setattr(cc, "_refresh_task", None)
    assert cd._custom_warnings(custom, [("card", "custom:installed", {})]) == []


@pytest.mark.asyncio
async def test_static_card_guide_survives_without_capability(monkeypatch):
    from ha_mcp.tools import tools_config_dashboards as dashboards

    from .test_card_definitions import _caps

    monkeypatch.setattr(
        dashboards, "get_component_caps", AsyncMock(return_value=_caps("search"))
    )
    attach = MagicMock()
    monkeypatch.setattr(dashboards, "attach_skill_content", attach)
    await dashboards._attach_dashboard_skill({}, True, MagicMock())
    assert "references/dashboard-cards.md" in attach.call_args.kwargs["canonical_files"]


@pytest.mark.parametrize("provider", ["quickjs", "quickjs-ng"])
def test_existing_incompatible_provider_is_never_replaced(monkeypatch, provider):
    from custom_components.ha_mcp_tools import card_runtime as runtime

    def version(name):
        if name == provider:
            return "0.16.2.1"
        raise runtime.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(runtime.metadata, "version", version)
    with pytest.raises(ValueError):
        runtime._provider_state()


@pytest.mark.asyncio
async def test_optional_runtime_install_uses_ha_manager_and_failure_is_advisory(
    monkeypatch,
):
    from custom_components.ha_mcp_tools import card_runtime as runtime

    install = AsyncMock(side_effect=RuntimeError("no compatible wheel"))
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.requirements",
        SimpleNamespace(async_process_requirements=install),
    )
    monkeypatch.setattr(runtime, "_check_core_constraints", lambda: None)
    monkeypatch.setattr(runtime, "_provider_state", lambda: "missing")
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn: fn())
    assert await runtime.async_ensure_runtime(hass) is False
    install.assert_awaited_once_with(
        hass,
        "ha_mcp_tools_dashboard_cards",
        [runtime.QUICKJS_REQUIREMENT],
        is_built_in=False,
    )


def test_conflicting_installed_consumer_disables_optional_runtime(monkeypatch):
    from custom_components.ha_mcp_tools import card_runtime as runtime

    monkeypatch.setattr(
        runtime.metadata,
        "distributions",
        lambda: [SimpleNamespace(requires=["quickjs-ng==0.16.2.1"])],
    )
    with pytest.raises(ValueError, match="another installed package"):
        runtime._check_consumers()


def test_core_constraints_cannot_be_overridden_by_the_optional_runtime(
    tmp_path, monkeypatch
):
    import homeassistant

    from custom_components.ha_mcp_tools import card_runtime as runtime

    monkeypatch.setattr(
        homeassistant, "__file__", str(tmp_path / "__init__.py"), raising=False
    )
    (tmp_path / "package_constraints.txt").write_text("quickjs-ng==0.0.0\n")
    with pytest.raises(ValueError, match="Home Assistant requires"):
        runtime._check_core_constraints()
