"""Attribute Core's stored system-log entries using integration manifests."""

from collections.abc import Iterable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.loader import IntegrationNotFound, async_get_integration
from homeassistant.setup import async_get_loaded_integrations


def _package_domains(names: Iterable[str]) -> set[str]:
    """The domains whose packages these logger names sit in."""
    domains = set()
    for name in names:
        parts = name.split(".")
        if parts[0] == "custom_components" and len(parts) > 1:
            domains.add(parts[1])
        elif parts[:2] == ["homeassistant", "components"] and len(parts) > 2:
            domains.add(parts[2])
    return domains


async def integration_loggers(
    hass: HomeAssistant, selected: str | None = None, names: Iterable[str] = ()
) -> dict[str, set[str]]:
    """Logger prefixes of the loaded integrations and of those ``names`` log from.

    An integration whose setup failed is not loaded, and its log lines are the
    ones being looked for. With ``selected``, only that integration's prefixes
    are returned, and it not existing raises.
    """
    loaded = set(async_get_loaded_integrations(hass))
    domains = {selected} if selected else loaded | _package_domains(names)
    prefixes: dict[str, set[str]] = {}
    for domain in domains:
        try:
            integration = await async_get_integration(hass, domain)
        except IntegrationNotFound:
            if domain == selected:
                raise
            continue
        for name in (integration.pkg_path, *(integration.loggers or ())):
            prefixes.setdefault(name, set()).add(domain)
    return prefixes


def logger_domains(name: str, prefixes: dict[str, set[str]]) -> list[str]:
    matches: set[str] = set()
    while name:
        matches.update(prefixes.get(name, ()))
        name = name.rpartition(".")[0]
    return sorted(matches)


async def _entries(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    from homeassistant.components.system_log.const import DOMAIN

    rows = hass.data[DOMAIN].records.to_list()
    selected = msg.get("integration")
    try:
        prefixes = await integration_loggers(
            hass, selected, (row["name"] for row in rows)
        )
    except IntegrationNotFound as exc:
        return {"error": str(exc)}
    for row in rows:
        row["integrations"] = logger_domains(row["name"], prefixes)
    if selected:
        rows = [row for row in rows if selected in row["integrations"]]
    return {"entries": rows}


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    return [
        (
            {
                vol.Required("type"): "ha_mcp_tools/system_log_entries",
                vol.Optional("integration"): str,
            },
            lambda hass, msg, **result: result,
            _entries,
        )
    ]
