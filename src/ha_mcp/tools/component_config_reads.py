"""Config-resolution + reference reads over the ``ha_mcp_tools`` component gate.

Two consumers in the automation/script/scene family read through the
component:

- Entity resolution maps storage keys to live registry entity IDs.
  ``entity_registration.resolve_entity_id_after_write`` polls after scene/script
  writes against one deadline. ``_resolve_scene_entity_id`` uses a single retry
  before scene removal. The component's ``entity_lookup`` returns only matching
  rows instead of a full registry/state listing. A hit is authoritative at once,
  but an empty read can precede HA's asynchronous entity registration even when
  performed in-process, so post-write callers must continue polling.
- The reference validator (``validate_config_references``) asks the
  component's ``ha_mcp_tools/config_references`` for Core's own verdict on a
  proposed config's references; Core serves nothing like it for an unsaved
  config, so there is no legacy path and an absent component is reported.

This module owns the caps-gated fetches so the routing discipline — probe caps,
send one frame, invalidate on ``unknown_command`` — lives in one place instead
of being duplicated per consumer (the pattern ``component_devices`` established
for the ``device_get`` / ``device_list`` capabilities).
``fetch_entity_lookup_via_component`` returns ``None`` to mean "component
unavailable — use the legacy path"; a component that answers authoritatively
returns its payload (an EMPTY ``matches`` list is authoritative — "no registry
entry with that unique_id" — kept distinct from the ``None`` miss). It maps a
transport failure (``HomeAssistantConnectionError`` off ``send_command`` or from
a failed (re)build of the pooled socket, or ``HomeAssistantAuthError``) to
``None`` too, NEVER propagating it: its consumers fall back to the legacy
WebSocket registry bridge or REST state reads, which an escaping error would
skip. The post-write resolver catches known API/transport failures and retains
a best-effort entity ID; later wait and category operations report any
verification or assignment failure. Removal resolvers retain their own error
policies.

**GET-path invariant:** the automation/script/scene *config-get* tools never
read the config body through the component — its in-process ``raw_config``
freshness lags the config file between a write and the next completed reload,
so the body always comes from Core's REST config endpoint. The script and scene
resolvers gate the whole component branch (caps probe included) behind
``allow_component`` for removals, and the dedicated post-write resolver is never
called from a config-get. The automation tools resolve through
``component_automation.automation_lookup`` instead, which reads the registry,
never the body.
"""

from __future__ import annotations

import logging
from typing import Any

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)

logger = logging.getLogger(__name__)

WS_ENTITY_LOOKUP = "ha_mcp_tools/entity_lookup"
WS_CONFIG_REFERENCES = "ha_mcp_tools/config_references"
CONFIG_REFERENCES = "config_references"


async def fetch_entity_lookup_via_component(
    client: Any,
    unique_id: str,
    *,
    domain: str | None = None,
) -> list[dict[str, Any]] | None:
    """One ``ha_mcp_tools/entity_lookup`` read; ``None`` ⇒ use the legacy path.

    Returns the component's ``matches`` list — every entity-registry entry whose
    ``unique_id`` equals ``unique_id`` (each ``{entity_id, unique_id, platform,
    domain, config_entry_id, categories, disabled_by, hidden_by}``), optionally
    narrowed by the entity's own ``domain``. Multiple matches (one ``unique_id``
    across platforms) are all returned — the caller picks. An AUTHORITATIVE empty
    list (no registry entry with that unique_id) is kept distinct from the
    ``None`` miss (component unavailable → legacy).

    ``None`` on capability miss, downgrade (``unknown_command`` → invalidate the
    cached caps), command error/timeout (logged), a connection-establishment
    failure (logged — see the module docstring: the resolvers' legacy paths ride
    the WS bridge / REST, NOT this pooled socket, so a transport
    failure must fall back rather than abort a landed write), or a shape-drift
    payload (no ``matches`` list). Same error-taxonomy and silent fallback as
    ``component_devices.fetch_device_via_component``.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "entity_lookup"):
        return None
    kwargs: dict[str, Any] = {"unique_id": unique_id}
    if domain is not None:
        kwargs["domain"] = domain
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        raw = await ws.send_command(WS_ENTITY_LOOKUP, **kwargs)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_ENTITY_LOOKUP, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        # HomeAssistantConnectionError (a pooled-WS drop or a failed
        # (re)connect) or any other error.
        # The resolvers' legacy paths do NOT die identically (see module docstring),
        # so route to legacy rather than escape.
        logger.warning(
            "%s connection error; falling back to legacy: %r", WS_ENTITY_LOOKUP, exc
        )
        return None
    result = raw.get("result")
    matches = result.get("matches") if isinstance(result, dict) else None
    if not isinstance(matches, list):
        logger.debug(
            "%s returned a malformed result (no 'matches' list); falling back to legacy",
            WS_ENTITY_LOOKUP,
        )
        return None
    return matches


_INSTALL_HINT = (
    "install the ha_mcp_tools component via HACS (the Marketplace on Home "
    "Assistant 2026.11+) and restart Home Assistant"
)


def _unavailable(error: str) -> dict[str, Any]:
    return {"status": "unavailable", "error": error}


def _answer_shape_error(result: Any) -> str | None:
    """Why *result* is not a ``config_references`` answer, or None if it is."""
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        return "no status"
    if result["status"] != "ok":
        return None if result.get("error") else "no error for a failed check"
    fields = {
        "missing": ("kind", "value"),
        "mismatches": ("path", "kind", "name", "entity_id"),
    }
    for key, strings in fields.items():
        items = result.get(key)
        if not isinstance(items, list) or not all(
            isinstance(item, dict)
            and all(isinstance(item.get(k), str) for k in strings)
            and (
                key != "mismatches"
                or (
                    isinstance(item.get("offered"), list)
                    and all(isinstance(o, str) for o in item["offered"])
                )
            )
            for item in items
        ):
            return f"malformed {key}"
    return None


async def fetch_config_references_via_component(
    client: Any, kind: str, config: dict[str, Any]
) -> dict[str, Any]:
    """Core's verdict on *config*'s references, through ``ha_mcp_tools/config_references``.

    The component validates *config* (an automation, script or scene, per
    *kind*) with Core, collects its references with Core's helpers, checks them
    against Core's registries and puts each entity target to Core's target
    lookups. It answers ``{"status": "ok", "missing": [...], "mismatches":
    [...]}``, ``{"status": "invalid", "error": ...}`` when Core's validator
    refuses the config, or ``{"status": "unavailable", "error": ...}``. An
    absent or outdated component, a command or transport failure and an answer
    of the wrong shape are answered the same unavailable way, naming the cause:
    the write goes on and the caller reports the references as unchecked. There
    is no REST/WebSocket fallback by design: Core serves the target lookups,
    ``validate_config`` for a config's trigger, condition and action lists, and
    the registries over its API, but does not collect an unsaved config's
    references, which is where the check starts; a server-side copy of that
    part is the walker this replaced.
    """
    caps = await get_component_caps(client)
    if caps is None:
        return _unavailable(
            "references were not checked: Home Assistant checks them through the "
            "ha_mcp_tools component, which is not installed or did not answer; "
            + _INSTALL_HINT
        )
    outdated = _unavailable(
        "references were not checked: the installed ha_mcp_tools component "
        "predates the reference check; update it via HACS (the Marketplace on "
        "Home Assistant 2026.11+)"
    )
    if not component_supports(caps, CONFIG_REFERENCES):
        return outdated
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        raw = await ws.send_command(WS_CONFIG_REFERENCES, kind=kind, config=config)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
            return outdated
        logger.warning("%s failed: %r", WS_CONFIG_REFERENCES, exc)
        return _unavailable(str(exc) or type(exc).__name__)
    except Exception as exc:
        # A soft check must never abort the write it accompanies.
        logger.warning("%s connection error", WS_CONFIG_REFERENCES, exc_info=True)
        return _unavailable(str(exc) or type(exc).__name__)
    result = raw.get("result")
    if (shape := _answer_shape_error(result)) is not None or not isinstance(
        result, dict
    ):
        logger.warning("%s answered with %s: %r", WS_CONFIG_REFERENCES, shape, result)
        return _unavailable(f"{WS_CONFIG_REFERENCES} answered with {shape}")
    return result
