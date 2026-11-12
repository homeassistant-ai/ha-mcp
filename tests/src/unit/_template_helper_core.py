"""A Template sensor helper made by Core's own config flow, for backup tests."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component


def state_template(value: str) -> str:
    """The sensor's state template, which renders ``value``."""
    return f"{{{{ '{value}' }}}}"


async def create_template_sensor(hass: HomeAssistant, value: str) -> ConfigEntry:
    """Create the helper through Core's config flow; its state renders ``value``.

    ``config`` is set up too: it serves the registry commands a capture reads.
    """
    assert await async_setup_component(hass, "config", {})
    result = await hass.config_entries.flow.async_init(
        "template", context={"source": "user"}
    )
    for answer in (
        {"next_step_id": "sensor"},
        {"name": "Example", "state": state_template(value)},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], answer
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


async def set_template_state(
    hass: HomeAssistant, entry: ConfigEntry, value: str
) -> None:
    """Change the helper's options, as an edit in Home Assistant would."""
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "state": state_template(value)}
    )
    await hass.async_block_till_done()
