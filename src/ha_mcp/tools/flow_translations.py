"""Home Assistant's own strings for flows, repairs, system health and names.

``frontend/get_translations`` returns an integration's ``strings.json`` for one
top-level category, flattened by Core to ``component.<domain>.<category>.<...>``
keys (``homeassistant.helpers.translation.recursive_flatten``). These are the
texts the UI shows for a flow's fields, errors and abort reasons, a repair's
title and description, a system health key's label and an integration's name,
so the tools read them from Core instead of keeping their own wording.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def strings_unread_warning(domain: str) -> str:
    """The warning for a response whose Core strings could not be read."""
    return (
        f"Home Assistant's strings for {domain} could not be read, so its flow "
        "texts (help, errors, reasons, progress) are missing"
    )


async def configured_language(client: Any, subject: str) -> tuple[str, str | None]:
    """Core's configured language, plus a warning when it could not be read.

    ``frontend/get_translations`` requires a language. Core serves the configured
    one in ``/api/config``; falling back to English keeps the text usable when
    that read is unavailable, but the caller is told that ``subject`` may be
    untranslated.
    """
    try:
        config = await client.get_config()
    except Exception as exc:
        logger.warning("Could not read the configured language", exc_info=True)
        return "en", f"{subject} may be untranslated: language unavailable: {exc}"
    language = config.get("language") if isinstance(config, Mapping) else None
    if isinstance(language, str) and language:
        return language, None
    logger.warning("Home Assistant config carries no usable language: %r", config)
    return "en", f"{subject} may be untranslated: language unavailable"


async def fetch_flow_strings(
    client: Any,
    domain: str | Sequence[str],
    category: str,
    *,
    language: str = "en",
) -> dict[str, Any] | None:
    """One ``frontend/get_translations`` read; ``None`` if it failed.

    ``category`` is a top-level ``strings.json`` key (``config``, ``options``,
    ``config_subentries``, ``issues``, ``system_health``, ``title``) and ``domain`` one integration or several.
    ``None`` (no answer, or an error answer) is kept apart from ``{}`` (Core
    ships no strings) so a caller can say so.
    """
    try:
        result = await client.send_websocket_message(
            {
                "type": "frontend/get_translations",
                "language": language,
                "category": category,
                "integration": [domain] if isinstance(domain, str) else list(domain),
            }
        )
    except Exception:
        logger.warning("translations for %s failed", domain, exc_info=True)
        return None
    if not isinstance(result, dict) or result.get("success") is False:
        logger.warning("translations for %s failed: %s", domain, result)
        return None
    payload = result.get("result") or result
    resources = payload.get("resources") if isinstance(payload, dict) else None
    return resources if isinstance(resources, dict) else {}


def field_help(
    strings: Mapping[str, Any], domain: str, path: str, step_id: str
) -> dict[str, str]:
    """HA's help text for a flow step's fields, keyed by field name, or by
    ``<section>.<field>`` for a field inside a section.

    ``path`` is the flow's place in ``strings.json``: ``config``, ``options``
    or ``config_subentries.<type>``. A field with no help text of its own gets
    its UI label instead.
    """
    prefix = f"component.{domain}.{path}.step.{step_id}."
    descriptions: dict[str, str] = {}
    labels: dict[str, str] = {}
    for key, text in strings.items():
        if not key.startswith(prefix) or not isinstance(text, str):
            continue
        # [sections.<section>.]data_description.<field> is the help text;
        # [sections.<section>.]data.<field> the field's label.
        parts = key[len(prefix) :].split(".")
        if len(parts) < 2 or parts[-2] not in ("data", "data_description"):
            continue
        name = f"{parts[1]}.{parts[-1]}" if parts[0] == "sections" else parts[-1]
        if parts[-2] == "data_description":
            descriptions[name] = text
        else:
            labels[name] = text
    return {**labels, **descriptions}


def flow_text(
    strings: Mapping[str, Any],
    domain: str,
    key: str,
    placeholders: Mapping[str, Any] | None,
) -> str | None:
    """The string at ``component.<domain>.<key>`` with its placeholders filled.

    ``key`` continues from the domain, e.g. ``config.abort.<reason>``.
    ``placeholders`` fills the ``{name}`` fields, for example a flow step's
    ``description_placeholders``; a ``{name}`` with no value stays as written.
    ``None`` when Core ships no string for the key.
    """
    text = strings.get(f"component.{domain}.{key}")
    if not isinstance(text, str) or not text:
        return None
    values = placeholders or {}
    return _PLACEHOLDER.sub(
        lambda m: str(values[m[1]]) if m[1] in values else m[0], text
    )
