"""The create/update discriminator in ``ha_config_set_label`` on real Core (#1860).

Bug
---
``ha_config_set_label`` chose create vs update purely on whether ``label_id``
was passed. Passing a ``label_id`` for a not-yet-existing label therefore
routed to ``config/label_registry/update``, which HA rejects with an opaque
``Command failed: Unknown error``, so callers saw a generic
``SERVICE_CALL_FAILED`` (an agent in the report retried 23 times before
finding the create path).

Fix
---
When ``label_id`` is supplied, verify it exists in the registry BEFORE routing
to update. If it does not, raise ``RESOURCE_NOT_FOUND`` with actionable
guidance ("omit label_id to create"). ``label_registry/create`` cannot honor a
caller-supplied id anyway (HA derives it from ``name``), so the contract stays
strictly update-only rather than upserting to a differently-derived id.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_labels import LabelTools

from ._client_spies import sent_messages


@pytest.fixture
async def tools(hass: HomeAssistant, core_client: HomeAssistantClient) -> LabelTools:
    """The label tools on Core with its label registry commands."""
    assert await async_setup_component(hass, "config", {})
    return LabelTools(core_client)


async def test_create_with_only_a_name_gets_cores_derived_id(
    tools: LabelTools, label_registry: lr.LabelRegistry
) -> None:
    result = await tools.ha_config_set_label(name="vendor:tapo")

    assert result["success"] is True
    assert result["label_id"] == "vendor_tapo"
    created = label_registry.async_get_label("vendor_tapo")
    assert created is not None
    assert created.name == "vendor:tapo"


async def test_update_of_an_existing_label_changes_it_in_place(
    tools: LabelTools, label_registry: lr.LabelRegistry
) -> None:
    """An existing id is updated, not created again under a derived id."""
    label_registry.async_create("vendor:tapo")

    result = await tools.ha_config_set_label(
        name="Vendor Tapo", label_id="vendor_tapo", color="blue"
    )

    assert result["success"] is True
    [label] = label_registry.async_list_labels()
    assert label.label_id == "vendor_tapo"
    assert label.name == "Vendor Tapo"
    assert label.color == "blue"


async def test_unknown_label_id_is_not_found_with_guidance(
    tools: LabelTools, label_registry: lr.LabelRegistry
) -> None:
    """Not a cryptic update failure: the message points at the create path."""
    label_registry.async_create("Other")

    with pytest.raises(ToolError) as excinfo:
        await tools.ha_config_set_label(name="Ghost", label_id="ghost")

    msg = str(excinfo.value)
    assert "RESOURCE_NOT_FOUND" in msg
    assert "ghost" in msg
    assert "omit label_id" in msg


async def test_reporters_colon_id_is_not_found_not_unknown_error(
    tools: LabelTools, label_registry: lr.LabelRegistry
) -> None:
    """The exact reporter repro: ``label_id='vendor:tapo'`` for a label Core
    stores as ``vendor_tapo``."""
    label_registry.async_create("vendor:tapo")

    with pytest.raises(ToolError) as excinfo:
        await tools.ha_config_set_label(name="vendor:tapo", label_id="vendor:tapo")

    msg = str(excinfo.value)
    assert "RESOURCE_NOT_FOUND" in msg
    assert "SERVICE_CALL_FAILED" not in msg
    assert "Unknown error" not in msg


async def test_unknown_id_never_reaches_cores_update(
    tools: LabelTools,
    core_client: HomeAssistantClient,
    label_registry: lr.LabelRegistry,
) -> None:
    """The guard fires before any update command is sent."""
    label_registry.async_create("Known")

    with sent_messages(core_client) as sent, pytest.raises(ToolError):
        await tools.ha_config_set_label(name="Ghost", label_id="ghost")

    sent_types = [call.args[0]["type"] for call in sent.await_args_list]
    assert "config/label_registry/list" in sent_types
    assert "config/label_registry/update" not in sent_types


async def test_blank_label_id_is_refused(tools: LabelTools) -> None:
    """The existence pre-check must not swallow the empty-identifier guard."""
    with pytest.raises(ToolError) as excinfo:
        await tools.ha_config_set_label(name="X", label_id="   ")

    assert "VALIDATION_INVALID_PARAMETER" in str(excinfo.value)


async def test_get_label_lists_every_label(
    tools: LabelTools, label_registry: lr.LabelRegistry
) -> None:
    label_registry.async_create("A")
    label_registry.async_create("B")

    result = await tools.ha_config_get_label()

    assert result["success"] is True
    assert result["count"] == 2


async def test_get_label_unknown_is_not_found(
    tools: LabelTools, label_registry: lr.LabelRegistry
) -> None:
    label_registry.async_create("A")

    with pytest.raises(ToolError) as excinfo:
        await tools.ha_config_get_label(label_id="missing")

    assert "RESOURCE_NOT_FOUND" in str(excinfo.value)


async def test_a_refused_registry_list_is_a_failure_not_a_missing_label(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Without Core's ``config`` integration the list command is unknown; that
    must not read as an empty registry that lacks the label."""
    lr.async_get(hass).async_create("Known")

    with pytest.raises(ToolError) as excinfo:
        await LabelTools(core_client).ha_config_set_label(name="X", label_id="known")

    msg = str(excinfo.value)
    assert "SERVICE_CALL_FAILED" in msg
    assert "RESOURCE_NOT_FOUND" not in msg
