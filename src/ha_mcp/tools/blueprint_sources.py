"""Tiered acquisition of an installed blueprint's YAML text.

Home Assistant core's ``blueprint/list`` returns metadata only — never the file
body — so every consumer that needs a blueprint's actual YAML (``get``'s round
trip, and the pre-delete auto-backup snapshot) has to find it somewhere else.
Four places can serve it, and which ones exist depends entirely on how this
server was installed. This module is the single ladder both consumers walk, and
it reports which rung answered so callers can be honest about the copy they got:

1. ``file`` — the in-process (embedded) server reads
   ``<config_dir>/blueprints/<domain>/<path>`` directly. Available only when the
   ha_mcp_tools component registered a ``config_dir`` (see
   :func:`ha_mcp.config.get_embedded_config_dir`); the path is computed here
   from ``domain`` + ``path``, jailed under that root, and opened read-only in a
   worker thread. This tier NEVER consults ``ALLOWED_READ_DIRS`` / the operator's
   extra directories and never registers or enables a filesystem tool.
2. ``component`` — ``ha_mcp_tools/blueprint_get`` returns the on-disk text when
   the component advertises the ``blueprint_text`` capability. A component
   advertising only ``blueprint_get`` still supplies the parsed ``config``, which
   is kept while the ladder keeps looking for text.
3. ``tools_entry`` — the privileged ``read_file`` service of the File & YAML
   Tools entry (``blueprints`` is a read-allowed directory there).
4. ``source_url`` — a re-download from the URL core recorded at import time.
   This is what the author publishes NOW, not the installed file, so callers
   must say so.

Nothing available means no text; a caller then serves metadata only.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..config import get_embedded_config_dir
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)

logger = logging.getLogger(__name__)

BlueprintSourceName = Literal["file", "component", "tools_entry", "source_url"]

COMPONENT_BODY_WARNING = (
    "Blueprint body could not be read or parsed by the ha_mcp_tools "
    "component; returning metadata only"
)


@dataclass(frozen=True)
class BlueprintSource:
    """What the ladder found for one installed blueprint.

    ``text`` is the raw YAML, ``config`` its parsed body (``!input`` kept as an
    ``{"__input__": name}`` marker), ``source`` names the rung that answered, and
    ``warning`` carries a caller-facing note — currently only "the component is
    installed but could not read this blueprint's body". When no rung could
    serve the blueprint, ``text``, ``config`` and ``source`` are all ``None``;
    ``warning`` may still be set, and is exactly how "the component has it but
    cannot read it" is told apart from "nothing knows about it".
    """

    text: str | None
    config: dict[str, Any] | None
    source: BlueprintSourceName | None
    warning: str | None


class _BlueprintLoader(yaml.SafeLoader):
    """SafeLoader for blueprint text: keep ``!input``, neutralize all other tags."""


def _construct_input(loader: Any, node: Any) -> dict[str, str]:
    """Represent ``!input <name>`` as ``{"__input__": <name>}``."""
    return {"__input__": str(getattr(node, "value", ""))}


def _drop_tag(loader: Any, tag_suffix: Any, node: Any) -> None:
    """Neutralize every non-``!input`` custom tag to ``None``, never resolving it."""
    return None


_BlueprintLoader.add_constructor("!input", _construct_input)
_BlueprintLoader.add_multi_constructor("!", _drop_tag)


def parse_blueprint_body(text: str) -> dict[str, Any] | None:
    """Parse blueprint YAML into a display body, or ``None`` when it will not parse.

    Mirrors the component's ``_BlueprintLoader`` exactly so the same file yields
    the same body whichever rung of the ladder produced the text: ``!input`` is
    preserved as a JSON-safe marker and every other custom tag (``!secret`` /
    ``!include`` / …) resolves to ``None``, so no secret plaintext can enter the
    parsed body.
    """
    # Instance form (not yaml.load) mirrors the component's own loader usage;
    # _BlueprintLoader is a SafeLoader subclass, so no !!python/object tag can
    # construct an arbitrary type.
    loader = _BlueprintLoader(text)
    try:
        parsed = loader.get_single_data()
    except yaml.YAMLError as exc:
        logger.debug("Blueprint body did not parse: %r", exc)
        return None
    finally:
        loader.dispose()
    return parsed if isinstance(parsed, dict) else None


# --- tier 1: direct read (embedded server only) -------------------------------


def _read_jailed_blueprint(config_dir: str, domain: str, path: str) -> str | None:
    """Read ``<config_dir>/blueprints/<domain>/<path>``, or ``None``.

    The jail mirrors the component's ``_read_blueprint_file``: resolve the RAW
    candidate (following symlinks) and the base, then require containment, so a
    symlink inside the blueprints directory cannot point out of it. An absolute
    ``path`` is rejected before any resolution. A missing file, a non-file
    target, or an unreadable one yields ``None`` — the caller falls to the next
    tier rather than raising.

    Blocking; call it through :func:`asyncio.to_thread`.
    """
    if not path or path.startswith(("/", "\\")) or PurePath(path).is_absolute():
        return None
    try:
        base = Path(config_dir) / "blueprints" / domain
        real = (base / path).resolve()
        base_real = base.resolve()
    except (OSError, ValueError):
        return None
    if not (real == base_real or real.is_relative_to(base_real)):
        logger.debug("Blueprint path %r escapes the blueprints jail; not read", path)
        return None
    try:
        if not real.is_file():
            return None
        return real.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.debug("Direct blueprint read of %r failed: %r", path, exc)
        return None


async def _read_direct(domain: str, path: str) -> str | None:
    """Tier 1: the embedded server's own read, or ``None`` when not embedded."""
    config_dir = get_embedded_config_dir()
    if not config_dir:
        return None
    return await asyncio.to_thread(_read_jailed_blueprint, config_dir, domain, path)


# --- tier 2: the component's blueprint_get ------------------------------------


@dataclass(frozen=True)
class _ComponentBody:
    """What ``ha_mcp_tools/blueprint_get`` could serve for one blueprint."""

    text: str | None
    config: dict[str, Any] | None
    warning: str | None


_NO_COMPONENT_BODY = _ComponentBody(None, None, None)


async def _via_component(client: Any, domain: str, path: str) -> _ComponentBody:
    """Tier 2: the component's parsed body, plus its raw text on 2.1.3+.

    Returns:

    - text and/or config — the component served the blueprint. ``text`` is
      present only when the component advertises ``blueprint_text``; an older
      build supplies ``config`` alone and the ladder keeps looking for text.
    - all ``None`` — metadata-only is the expected outcome: the component is
      absent or lacks ``blueprint_get``, was downgraded (``unknown_command`` →
      the cached caps are invalidated), errored, or the WS transport failed.
      There is no legacy full-body fetch, so a transport failure simply leaves
      the caller with the metadata it already has.
    - a ``warning`` with no body — the component is present and the caller has
      already confirmed the path is a real installed blueprint, yet it returned
      a null ``config`` (corrupt / unparseable file, read error). Silently
      serving metadata would be indistinguishable from component-not-installed.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "blueprint_get"):
        return _NO_COMPONENT_BODY
    wants_text = component_supports(caps, "blueprint_text")
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        raw = await ws.send_command(
            "ha_mcp_tools/blueprint_get", domain=domain, path=path
        )
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning(
                "ha_mcp_tools/blueprint_get failed; served metadata-only: %r", exc
            )
        return _NO_COMPONENT_BODY
    except Exception as exc:
        # HomeAssistantConnectionError / plain establish Exception → metadata-only
        # (no full-body legacy fetch exists; the base metadata is already served).
        logger.warning(
            "ha_mcp_tools/blueprint_get connection error; served metadata-only: %r",
            exc,
        )
        return _NO_COMPONENT_BODY
    result = raw.get("result") or {}
    config = result.get("config")
    config = config if isinstance(config, dict) else None
    text = result.get("yaml") if wants_text else None
    text = text if isinstance(text, str) and text else None
    if config is None and text is None:
        return _ComponentBody(None, None, COMPONENT_BODY_WARNING)
    return _ComponentBody(text, config, None)


# --- tier 3: the File & YAML Tools entry's read_file ---------------------------


async def _via_tools_entry(client: Any, domain: str, path: str) -> str | None:
    """Tier 3: the privileged ``read_file`` service, or ``None`` when unusable.

    Every failure mode degrades to the next tier: the File & YAML Tools entry is
    absent (the caller-token gate raises ``ToolError``), the component is too
    old, the read failed, or the client is not one that can reach HA's REST API
    at all. None of those is a fault of the blueprint being asked for, so the
    catch is deliberately broad and only ever costs one debug line.
    """
    from ..backup_manager import _fetch_file

    try:
        content = await _fetch_file(client, f"blueprints/{domain}/{path}")
    except Exception as exc:
        logger.debug(
            "File & YAML Tools read of blueprint %r unavailable (%r)", path, exc
        )
        return None
    return content if isinstance(content, str) and content else None


# --- tier 4: re-download from the recorded source_url -------------------------


async def _via_source_url(
    client: Any, domain: str, path: str, source_url: str
) -> str | None:
    """Tier 4: re-download ``source_url``, accepted only if it still serves ``path``.

    Second-best copy by construction: it is what the author publishes NOW, which
    can differ from the installed file (a local edit, or an upstream update since
    the import). Accepted only while the re-fetched blueprint still reports
    ``domain`` AND its own suggested filename still resolves to ``path`` —
    otherwise the URL now serves a different blueprint and using it would hand
    back the wrong content.
    """
    try:
        imported = await client.send_websocket_message(
            {"type": "blueprint/import", "url": source_url}
        )
    except Exception as exc:
        logger.debug(
            "Re-fetch of blueprint %r from %s failed: %r", path, source_url, exc
        )
        return None
    if not isinstance(imported, dict) or not imported.get("success"):
        return None
    result = imported.get("result") or {}
    # The filename says nothing about the domain, and blueprint paths are
    # scoped by it: an author who replaces an automation blueprint with a
    # script one at the same URL and filename would otherwise be served back
    # as this automation's body.
    refetched_domain = ((result.get("blueprint") or {}).get("metadata") or {}).get(
        "domain"
    )
    if refetched_domain != domain:
        logger.debug(
            "%s now serves a %r blueprint, not %r -- not using it for %r",
            source_url,
            refetched_domain,
            domain,
            path,
        )
        return None
    if f"{result.get('suggested_filename')}.yaml" != path:
        logger.debug(
            "%s no longer serves blueprint %r (suggests %r) — not using a "
            "different blueprint",
            source_url,
            path,
            result.get("suggested_filename"),
        )
        return None
    raw_data = result.get("raw_data")
    if not isinstance(raw_data, str) or not raw_data:
        return None
    logger.warning(
        "Blueprint %r was re-fetched from %s, not read from the installed file "
        "— the two can differ if the blueprint was edited locally or updated "
        "upstream",
        path,
        source_url,
    )
    return raw_data


# --- the ladder ---------------------------------------------------------------


def _resolved(
    text: str | None,
    config: dict[str, Any] | None,
    source: BlueprintSourceName | None,
    warning: str | None,
) -> BlueprintSource:
    """Assemble the result, parsing ``text`` when no component config was served.

    A parse failure never masks text that was read: ``config`` simply stays
    ``None``. The component's warning is dropped once a body was obtained by any
    route -- TEXT counts, not just parsed config, because the warning says
    "returning metadata only" and a response carrying ``yaml`` is not that.
    """
    if config is None and text is not None:
        config = parse_blueprint_body(text)
    served_a_body = config is not None or text is not None
    return BlueprintSource(
        text=text,
        config=config,
        source=source,
        warning=None if served_a_body else warning,
    )


async def resolve_blueprint_source(
    client: Any, domain: str, path: str, *, source_url: str | None
) -> BlueprintSource:
    """Find an installed blueprint's YAML text, best copy first.

    Walks the four tiers documented in the module docstring and reports which
    one answered. ``source_url`` is the URL core recorded for the blueprint (from
    ``blueprint/list`` metadata); pass ``None`` when there is none, and the last
    tier is skipped.
    """
    text = await _read_direct(domain, path)
    if text is not None:
        return _resolved(text, None, "file", None)

    body = await _via_component(client, domain, path)
    if body.text is not None:
        return _resolved(body.text, body.config, "component", body.warning)

    text = await _via_tools_entry(client, domain, path)
    if text is not None:
        return _resolved(text, body.config, "tools_entry", body.warning)

    if source_url:
        text = await _via_source_url(client, domain, path, source_url)
        if text is not None:
            return _resolved(text, body.config, "source_url", body.warning)

    if body.config is not None:
        return _resolved(None, body.config, "component", body.warning)
    return BlueprintSource(None, None, None, body.warning)
