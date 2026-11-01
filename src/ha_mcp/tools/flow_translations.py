"""Home Assistant's own strings for config, options and subentry flows.

``frontend/get_translations`` returns an integration's ``strings.json`` for one
top-level category, flattened by Core to ``component.<domain>.<category>.<...>``
keys (``homeassistant.helpers.translation.recursive_flatten``). These are the
texts the UI shows for a flow's fields, errors and abort reasons, so the tools
read them from Core instead of keeping their own wording.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


async def fetch_flow_strings(client: Any, domain: str, category: str) -> dict[str, Any]:
    """One ``frontend/get_translations`` read in English; ``{}`` on any failure.

    ``category`` is a top-level ``strings.json`` key (``config``, ``options``,
    ``config_subentries``). Best effort: missing strings only cost help text.
    """
    try:
        result = await client.send_websocket_message(
            {
                "type": "frontend/get_translations",
                "language": "en",
                "category": category,
                "integration": [domain],
            }
        )
    except Exception as err:  # noqa: BLE001
        logger.debug("translations for %s failed: %s", domain, err)
        return {}
    if not isinstance(result, dict):
        return {}
    payload = result.get("result") or result
    resources = payload.get("resources") if isinstance(payload, dict) else None
    return resources if isinstance(resources, dict) else {}


def field_help(
    strings: Mapping[str, Any], domain: str, path: str, step_id: str | None
) -> dict[str, str]:
    """HA's help text for a flow step's fields, keyed by field name.

    ``path`` is the flow's place in ``strings.json``: ``config``, ``options``
    or ``config_subentries.<type>``. A field with no help text of its own gets
    its UI label instead.
    """
    if not step_id:
        return {}
    prefix = f"component.{domain}.{path}.step.{step_id}."
    descriptions: dict[str, str] = {}
    labels: dict[str, str] = {}
    for key, text in strings.items():
        if not key.startswith(prefix) or not isinstance(text, str):
            continue
        # [sections.<section>.]data_description.<field> is the help text;
        # [sections.<section>.]data.<field> the field's label.
        parts = key[len(prefix) :].split(".")
        if len(parts) >= 2 and parts[-2] == "data_description":
            descriptions[parts[-1]] = text
        elif len(parts) >= 2 and parts[-2] == "data":
            labels[parts[-1]] = text
    return {**labels, **descriptions}


def flow_text(
    strings: Mapping[str, Any],
    domain: str,
    key: str,
    placeholders: Mapping[str, Any] | None,
) -> str | None:
    """The string at ``component.<domain>.<key>`` with its placeholders filled.

    ``key`` continues from the domain, e.g. ``config.abort.<reason>``.
    ``placeholders`` is the flow step's ``description_placeholders``; a
    ``{name}`` with no value stays as written. ``None`` when Core ships no
    string for the key.
    """
    text = strings.get(f"component.{domain}.{key}")
    if not isinstance(text, str) or not text:
        return None
    values = placeholders or {}
    return _PLACEHOLDER.sub(
        lambda m: str(values[m[1]]) if m[1] in values else m[0], text
    )
