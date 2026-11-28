"""Automations and scripts defined in YAML outside Home Assistant's editable
storage (a package, or a file ``configuration.yaml`` includes).

Core's config API reads and writes only ``automations.yaml`` and
``scripts.yaml``, where the UI and the config tools store an item. One defined
elsewhere exists only as Core loaded it. The component's
``ha_mcp_tools/loaded_config`` returns that definition with the file and line
Core's loader read it from; without the component, Core's own
``automation/config`` / ``script/config`` WebSocket command returns the same
body and the file stays unknown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import HomeAssistantConnectionError
from ..errors import ErrorCode, create_error_response
from ..redaction import redaction_enabled
from .component_api import (
    COMPONENT_INSTALL_HINT,
    component_supports,
    get_component_caps,
    invalidate_caps,
)
from .config_write_helpers import fetch_registry_entry
from .entity_registration import script_entity_or_warning
from .helpers import tool_error_from_response

logger = logging.getLogger(__name__)

WS_LOADED_CONFIG = "ha_mcp_tools/loaded_config"
CAPABILITY = "loaded_config"
Domain = Literal["automation", "script"]
STORAGE_SOURCE: dict[str, Any] = {"kind": "storage", "editable": True}
FILE_NEEDS_COMPONENT = (
    "The YAML file that defines it is only known through the ha_mcp_tools "
    f"component's loaded_config command. {COMPONENT_INSTALL_HINT}"
)


@dataclass(frozen=True)
class LoadedDefinition:
    """A YAML-defined item as Core loaded it."""

    domain: Domain
    entity_id: str
    config: dict[str, Any]
    file: str | None
    line: int | None
    from_component: bool
    # The body's !secret values are not redacted: Core's own command returned
    # it, or the component could not read secrets.yaml.
    unredacted: bool = False
    # redact_secrets is on, so the unredacted body was left out of ``config``.
    withheld: bool = False

    @property
    def where(self) -> str:
        if self.file is None:
            return "a YAML file or package"
        return self.file if self.line is None else f"{self.file}, line {self.line}"

    @property
    def source(self) -> dict[str, Any]:
        return {
            "kind": "yaml",
            "editable": False,
            "file": self.file,
            "line": self.line,
        }

    @property
    def warnings(self) -> list[str]:
        note = (
            f"{self.entity_id} is defined in YAML ({self.where}), outside the "
            "storage Home Assistant's UI and the config tools write to. "
            "config is the definition Home Assistant has loaded: a change to "
            f"the file shows after {self.domain}s are reloaded, and "
            f"ha_config_set_{self.domain} and ha_config_remove_{self.domain} "
            "cannot change it."
        )
        notes = [note]
        if not self.from_component:
            notes.append(FILE_NEEDS_COMPONENT)
        elif self.unredacted:
            notes.append(SECRETS_UNREAD)
        return notes


LEAVE_FILE_TO_USER = (
    "It changes only in its YAML file; leave that to the user unless they "
    "ask for the file to be edited"
)
ENABLE_TO_READ = (
    "Enable its entity with ha_set_entity to read the definition Home Assistant loads"
)
SECRETS_UNREAD = (
    "secrets.yaml could not be read, so !secret values in config are not redacted"
)
WITHHELD = (
    " Its definition is withheld: redact_secrets is on, and the !secret "
    "values Home Assistant resolved in it could not be redacted"
)
WITHHELD_WHY = {
    True: f"{WITHHELD}, since secrets.yaml could not be read.",
    False: f"{WITHHELD} without the ha_mcp_tools component. {COMPONENT_INSTALL_HINT}",
}


def _refusal(definition: LoadedDefinition, context: dict[str, Any]) -> ToolError:
    domain, noun = definition.domain, definition.domain.capitalize()
    read = (
        f"Read it with ha_config_get_{domain}, which returns the definition "
        "Home Assistant has loaded"
    )
    write = (
        f"Create and edit {domain}s with ha_config_set_{domain}: it stores "
        "them where Home Assistant's UI does, never in a YAML file or package"
    )
    return tool_error_from_response(
        create_error_response(
            ErrorCode.CONFIG_NOT_FOUND,
            f"{noun} {definition.entity_id} is defined in YAML "
            f"({definition.where}), outside the storage Home Assistant's "
            f"config API edits, so ha_config_set_{domain} and "
            f"ha_config_remove_{domain} cannot change or remove it."
            + (WITHHELD_WHY[definition.from_component] if definition.withheld else ""),
            context={**context, "source": definition.source},
            suggestions=[write, LEAVE_FILE_TO_USER]
            if definition.withheld
            else [read, write, LEAVE_FILE_TO_USER],
        )
    )


class YamlDefinedError(ToolError):
    """CONFIG_NOT_FOUND for an item Core's config API does not store, carrying
    the definition Core loaded so a read can return it."""

    def __init__(self, definition: LoadedDefinition, context: dict[str, Any]) -> None:
        error = _refusal(definition, context)
        super().__init__(str(error), log_level=error.log_level)
        self.definition = definition


async def _from_component(client: Any, entity_id: str) -> dict[str, Any] | None:
    """The component's reply; None when it gave no usable one."""
    if not component_supports(await get_component_caps(client), CAPABILITY):
        return None
    try:
        result = await client.send_websocket_message(
            {"type": WS_LOADED_CONFIG, "entity_id": entity_id}
        )
    except HomeAssistantConnectionError as exc:
        logger.warning("%s got no answer: %s", WS_LOADED_CONFIG, exc)
        return None
    if not result.get("success"):
        if result.get("error_code") == "unknown_command":
            invalidate_caps(client)
        logger.warning("%s failed: %s", WS_LOADED_CONFIG, result.get("error"))
        return None
    reply = result.get("result")
    # A reply without the verdict is malformed: read from Core instead.
    usable = isinstance(reply, dict) and isinstance(reply.get("loaded"), bool)
    return reply if usable else None


async def loaded_definition(
    client: Any, domain: Domain, entity_id: str
) -> LoadedDefinition | None:
    """The definition Core has loaded for ``entity_id``; None when it holds none."""
    reply = await _from_component(client, entity_id)
    if reply is not None:
        config = reply.get("config")
        # in_storage: a removed item Core still holds until its reload ends.
        if (
            not reply.get("loaded")
            or reply.get("in_storage")
            or not isinstance(config, dict)
        ):
            return None
        unredacted = bool(reply.get("scrub_degraded"))
        return _definition(
            domain, entity_id, config, reply.get("file"), reply.get("line"), unredacted
        )
    result = await client.send_websocket_message(
        {"type": f"{domain}/config", "entity_id": entity_id}
    )
    config = (result.get("result") or {}).get("config")
    if not result.get("success") or not isinstance(config, dict):
        return None
    return _definition(domain, entity_id, config, None, None, True, component=False)


def _definition(
    domain: Domain,
    entity_id: str,
    config: dict[str, Any],
    file: str | None,
    line: int | None,
    unredacted: bool,
    *,
    component: bool = True,
) -> LoadedDefinition:
    """The definition, its body left out when it is unredacted under redact_secrets."""
    withheld = unredacted and redaction_enabled()
    return LoadedDefinition(
        domain,
        entity_id,
        {} if withheld else config,
        file,
        line,
        component,
        unredacted,
        withheld,
    )


async def raise_not_in_storage(
    client: Any,
    domain: Domain,
    entity_id: str | None,
    context: dict[str, Any],
    *,
    registered: bool | None,
) -> None:
    """Raise for an item Core's config API stores nothing for, when Core knows it.

    A loaded one is YAML-defined (:class:`YamlDefinedError`, which a read
    returns the definition from). A ``registered`` one Core has not loaded, a
    disabled entity, is CONFIG_NOT_FOUND; None reads that from the entity
    registry. Returns when Core knows neither.
    """
    if entity_id and entity_id.startswith(f"{domain}."):
        definition = await loaded_definition(client, domain, entity_id)
        if definition is not None and definition.withheld:
            raise _refusal(definition, context)
        if definition is not None:
            raise YamlDefinedError(definition, context)
    if registered is None:
        row = await fetch_registry_entry(client, entity_id) if entity_id else None
        registered = bool(row and row.get("disabled_by"))
    if not registered:
        return
    raise tool_error_from_response(
        create_error_response(
            ErrorCode.CONFIG_NOT_FOUND,
            f"{domain.capitalize()} {entity_id} exists but Home Assistant's "
            "config API stores nothing for it: it is defined in YAML or a "
            "package, and Home Assistant has no loaded definition for it (a "
            "disabled entity is not loaded).",
            context=context,
            suggestions=[ENABLE_TO_READ, LEAVE_FILE_TO_USER],
        )
    )


async def raise_if_script_in_yaml(
    client: Any, script_id: str, key: str | None = None, *, stored: bool = False
) -> None:
    """Refuse a script Core's config API stores nothing for when Core loaded
    it from YAML outside ``scripts.yaml``; ``key`` is the storage key a write
    resolved, ``stored`` whether Core's config API holds it."""
    if stored:
        return
    entity_id: str | None = script_id
    if key is not None or not script_id.startswith("script."):
        entity_id = await script_entity_or_warning(
            client, script_id, key or script_id, [], ""
        )
    await raise_not_in_storage(
        client, "script", entity_id, {"script_id": script_id}, registered=None
    )


async def raise_automation_not_stored(client: Any, entity_id: str, target: Any) -> None:
    """:func:`raise_not_in_storage` for an automation: ``target`` is the tool's
    resolved target (its ``entity_id`` and ``config_id``), None when the
    registry does not know ``entity_id``."""
    registered_id = target.entity_id if target is not None else None
    loaded_id = registered_id if target is not None else entity_id
    await raise_not_in_storage(
        client,
        "automation",
        loaded_id,
        {"automation_id": loaded_id, "config_id": target and target.config_id},
        registered=bool(registered_id),
    )
