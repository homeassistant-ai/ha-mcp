"""Helpers shared by the config set tools.

Covers skill content attached to write responses (the ``MandatoryBPS``
parameter), entity category lookup and assignment, reference-validator
metadata, and the config reload waiter.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from pydantic import ValidationError

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..utils.skill_loader import (
    BEST_PRACTICES_SKILL_NAME as _HA_BEST_PRACTICES_SKILL_NAME,
)
from .ws_waiters import _get_waiter_ws_client, _ws_cleanup, _ws_subscribe_all

logger = logging.getLogger(__name__)


async def fetch_entity_category(
    client: Any, entity_id: str, scope: str, warnings: list[str] | None = None
) -> str | None:
    """Fetch a category ID for an entity from the entity registry.

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity to look up (e.g., 'automation.morning_routine')
        scope: Category scope (e.g., 'automation', 'script', 'helpers')

    Returns:
        Category ID string if set, None otherwise

    ``None`` is also what a failed lookup returns, which reads as "no category
    assigned" - a caller that passes ``warnings`` gets a line naming the
    failure instead, so the two are distinguishable (#1947).
    """
    try:
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
        if isinstance(result, dict) and result.get("success"):
            entry = result.get("result")
            # The registry entry is a dict; anything else (a list, None) is a
            # malformed read rather than an entry without categories.
            if isinstance(entry, dict):
                categories = entry.get("categories") or {}
                cat_id = categories.get(scope) if isinstance(categories, dict) else None
                return str(cat_id) if cat_id is not None else None
            return None
        # Shape-guarded rather than relying on a broad except: a non-dict
        # payload is a malformed read, not "no category assigned".
        reason = (
            (result.get("error") or "request failed")
            if isinstance(result, dict)
            else f"unexpected response type: {type(result).__name__}"
        )
        if warnings is not None:
            warnings.append(f"category unavailable for {entity_id}: {reason}")
    except (
        HomeAssistantConnectionError,
        HomeAssistantAPIError,
        HomeAssistantAuthError,
        TimeoutError,
        OSError,
    ) as e:
        logger.warning(f"Failed to fetch category for {entity_id}: {e}")
        if warnings is not None:
            warnings.append(f"category unavailable for {entity_id}: {e}")
    return None


async def apply_entity_category(
    client: Any,
    entity_id: str,
    category: str,
    scope: str,
    result_dict: dict[str, Any],
    entity_type: str = "entity",
) -> None:
    """Apply a category to an entity via the entity registry.

    Updates result_dict in-place: sets ``'category'`` on success, or appends
    to the top-level ``'warnings'`` list on failure. The list shape mirrors
    the canonical response contract documented in ``.gemini/styleguide.md`` →
    *Tool Tags and Return Values*.

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity to update
        category: Category ID to assign
        scope: Category scope (e.g., 'automation', 'script')
        result_dict: Tool result dict to update with category status
        entity_type: Human-readable type for warning messages
    """
    # Best-effort recheck immediately before the write (issue #2159): the
    # caller validated the category at tool entry, but the config upsert and
    # registration wait sit between that check and this write — a category
    # deleted in that window would otherwise be stored dangling (HA does not
    # validate it). A failed recheck lookup falls through to the write: entry
    # validation already screened typos, and the config is saved either way.
    try:
        check = await client.send_websocket_message(
            {"type": "config/category_registry/list", "scope": scope}
        )
        if isinstance(check, dict) and check.get("success"):
            valid_ids = {
                c.get("category_id")
                for c in check.get("result") or []
                if isinstance(c, dict)
            }
            if category not in valid_ids:
                result_dict.setdefault("warnings", []).append(
                    f"{entity_type.capitalize()} saved but category "
                    f"{category!r} no longer exists in the {scope} registry — "
                    "not applied."
                )
                return
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Category recheck failed for {entity_id}: {e}")

    try:
        ws_result = await client.send_websocket_message(
            {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "categories": {scope: category},
            }
        )
        if ws_result.get("success"):
            result_dict["category"] = category
        else:
            error_detail = ws_result.get("error", {})
            error_msg = (
                error_detail.get("message", "Unknown error")
                if isinstance(error_detail, dict)
                else str(error_detail)
            )
            logger.warning(f"Failed to set category for {entity_id}: {error_msg}")
            result_dict.setdefault("warnings", []).append(
                f"{entity_type.capitalize()} saved but failed to set category: {error_msg}"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to set category for {entity_id}: {e}")
        result_dict.setdefault("warnings", []).append(
            f"{entity_type.capitalize()} saved but failed to set category: {e}"
        )


def merge_validation_meta(
    result: dict[str, Any], validation_meta: dict[str, Any]
) -> None:
    """Attach reference-validator output to a set-tool success ``result``.

    Produces a single nested ``validation`` field when there's anything
    worth reporting - warnings, skipped templates, or a blueprint
    short-circuit. Keeps the happy-path response unchanged.

    Shared between ``ha_config_set_automation`` and
    ``ha_config_set_script``; see
    :mod:`ha_mcp.tools.reference_validator` for the validator itself
    and #940 for background.
    """
    warnings = validation_meta.get("warnings") or []
    unvalidated_templates = validation_meta.get("unvalidated_templates") or 0
    blueprint_skipped = bool(validation_meta.get("blueprint_skipped"))

    if not warnings and not unvalidated_templates and not blueprint_skipped:
        return

    entry: dict[str, Any] = {}
    if warnings:
        entry["warnings"] = warnings
    if unvalidated_templates:
        entry["unvalidated_templates"] = unvalidated_templates
    if blueprint_skipped:
        entry["blueprint_skipped"] = True
    result["validation"] = entry


# ---------------------------------------------------------------------------
# Skill content assembly (write-tool MandatoryBPS parameter, issue #1182)
# ---------------------------------------------------------------------------


def build_skill_content(
    MandatoryBPS: bool,
    canonical_files: tuple[str, ...],
    referenced_files: set[str] | None,
) -> dict[str, str]:
    """Resolve and dedupe skill files (or sections) for a write-tool response.

    Shared helper for every write tool that exposes ``MandatoryBPS``
    (ha_config_set_automation / _script / _scene / _helper / _dashboard /
    _yaml). Each tool owns its own ``canonical_files`` mapping and passes
    it in; the helper unions against ``referenced_files`` from the
    best-practice checker (where applicable) and reads the bodies via
    :func:`ha_mcp.utils.skill_loader.resolve_skill_files`.

    ``referenced_files`` may carry ``#anchor`` suffixes
    (``"references/triggers-and-conditions.md#native-conditions"``); those
    resolve to just the matching markdown section. ``canonical_files``
    entries are bare paths and resolve to whole files.

    Dedup: if ``canonical_files`` already requests a bare path that some
    anchored entry in ``referenced_files`` points at, the anchored entry
    is dropped — the full file supersedes the section, no point shipping
    the same content twice in different shapes.

    Args:
        MandatoryBPS: When True, attach the canonical files for this tool.
        canonical_files: Tool-specific default mapping. Paths are relative
            to the home-assistant-best-practices skill directory
            (e.g. ``"references/triggers-and-conditions.md"``).
        referenced_files: Files (optionally with ``#anchor``) cited by
            best-practice warnings — always attached, regardless of
            ``MandatoryBPS``. Pass ``None`` for tools without
            best-practice checker integration.

    Returns:
        ``{ref: body_or_section}`` for each ref that resolves. Empty dict
        when nothing to embed or the skills-vendor submodule is absent —
        callers should omit the ``skill_content`` field from the response
        when the return is empty.
    """
    from ..config import get_global_settings
    from ..utils.skill_loader import get_skills_dir, resolve_skill_files

    # Server-side master switch (issue #1182). When the operator has set
    # ENABLE_MANDATORY_BPS=false (env var / addon config / web UI), NO
    # skill_content goes out for any write tool — neither the per-call
    # canonical files nor the BP-warning auto-embed nor the opt-out hint.
    # Sits above the per-call ``MandatoryBPS`` flag because that flag
    # controls per-call behaviour; this controls whether the feature is
    # active at all.
    #
    # Settings-load is wrapped because skill_content is opportunistic —
    # the write has already committed by the time we're consulting
    # settings here, and a settings-validation regression must not turn
    # a successful write into a tool-level INTERNAL_ERROR (which would
    # then prompt the agent to retry, double-applying the mutation).
    # Silent degrade to "no skill_content" is the documented contract.
    # Narrowed to ValidationError (the realistic config-load failure from
    # Settings()) so genuine bugs (AttributeError/ImportError/etc.) still
    # surface instead of being masked on every write — per the repo's
    # narrow-except convention.
    try:
        if not get_global_settings().enable_mandatory_bps:
            return {}
    except ValidationError:
        logger.warning("skill_content settings lookup failed; omitting", exc_info=True)
        return {}

    wanted: set[str] = set()
    if MandatoryBPS:
        wanted.update(canonical_files)
    if referenced_files:
        wanted.update(referenced_files)
    if not wanted:
        return {}

    # Bare-file canonical supersedes anchored section refs for the same
    # file (no point shipping the whole file AND a section from it).
    bare_paths = {w for w in wanted if "#" not in w}
    wanted = {w for w in wanted if "#" not in w or w.split("#", 1)[0] not in bare_paths}

    return resolve_skill_files(
        get_skills_dir(), _HA_BEST_PRACTICES_SKILL_NAME, sorted(wanted)
    )


_SKILLS_VENDOR_MISSING_WARNING = (
    "skill_content unavailable — the home-assistant-best-practices "
    "skills-vendor submodule is not initialised. The agent will not "
    "receive the relevant best-practice guidance for this write. "
    "Run `git submodule update --init` on the server install."
)

# Opt-out hint shipped alongside delivered skill_content. The param
# name (`MandatoryBPS`), absence of a Field description, and first-key
# response placement are all load-bearing — each was settled by BAT
# (failing alternatives reflexively triggered minimisation, omission,
# or invisibility on at least one model). Don't tune any of them
# casually; re-BAT before any change.
_SKILL_CONTENT_OPTOUT_HINT = (
    "Pass `MandatoryBPS=false` on subsequent calls to this tool in "
    "this session to skip this content."
)

# Suggestion appended to EVERY write-tool error response. Not every
# error has BP-checker context, so the generic skill-guide pointer is
# the floor — when BP fired, the checker's own warning text (with its
# more specific 2-route hint) is appended alongside in the suggestions
# array, and the matching section body auto-embeds inline.
_WRITE_TOOL_BP_HINT_SUGGESTION = (
    "For Home Assistant best-practice guidance, call "
    "ha_get_skill_guide() to read SKILL.md, then the reference file its "
    "table points to, before retrying."
)


def attach_skill_content(
    response: dict[str, Any],
    MandatoryBPS: bool,
    canonical_files: tuple[str, ...],
    referenced_files: set[str] | None,
) -> None:
    """In-place attach skill_content to a response, warn on degraded vendor.

    Resolves ``canonical_files`` and/or ``referenced_files`` via
    :func:`build_skill_content` and attaches the result under
    ``response["skill_content"]`` when non-empty.

    Asymmetry vs the read-side ``ha_get_skill_guide`` tool: when the
    bundled skills-vendor submodule is missing, the read tool returns a
    structured ``degraded: True`` payload, but the write tools previously
    just omitted ``skill_content`` silently — the operator never sees
    that the LLM is missing the proactive guidance the docstring
    promised. This helper appends a top-level ``warnings[]`` entry in
    that case so the omission is observable rather than silent.

    Args:
        response: The dict to mutate. ``skill_content`` and/or ``warnings``
            may be added.
        MandatoryBPS: When True, attach the canonical files for this tool.
        canonical_files: Tool-specific default mapping.
        referenced_files: Files cited by best-practice warnings.
    """
    from ..config import get_global_settings
    from ..utils.skill_loader import get_skills_dir

    content = build_skill_content(
        MandatoryBPS=MandatoryBPS,
        canonical_files=canonical_files,
        referenced_files=referenced_files,
    )
    if content:
        # Reorder so the hint is the FIRST key in the response and the
        # bulky skill_content is the LAST. LLMs (especially smaller
        # models) process responses top-down and BAT showed the
        # trailing-hint placement was getting buried under ~25KB of
        # markdown — Opus needed five tries to find it, Sonnet/Haiku
        # never found it. The mutation is in place because callers
        # pass the dict by reference and expect their handle to keep
        # pointing at the same response object.
        existing_items = list(response.items())
        response.clear()
        response["skill_content_hint"] = _SKILL_CONTENT_OPTOUT_HINT
        response.update(existing_items)
        response["skill_content"] = content
        return

    # Empty content has three distinct causes:
    # 1. Operator disabled the feature server-wide (ENABLE_MANDATORY_BPS=False).
    #    Suppression is deliberate — no warning, no nag.
    # 2. Nothing was requested (MandatoryBPS=False AND no referenced_files).
    #    Benign — return silently.
    # 3. Something was requested but the vendor submodule is missing.
    #    Degraded — append a warning so operators notice.
    # Narrowed to ValidationError (see build_skill_content). On a settings
    # lookup failure we can't know the master state, so default master_on
    # to False — that suppresses the vendor-missing warning rather than
    # emitting a misleading one whose real cause was the settings fetch.
    try:
        master_on = get_global_settings().enable_mandatory_bps
    except ValidationError:
        master_on = False
    requested_anything = MandatoryBPS or referenced_files
    if master_on and requested_anything and get_skills_dir() is None:
        response.setdefault("warnings", []).append(_SKILLS_VENDOR_MISSING_WARNING)


def augment_error_dict_with_skill_content(
    error_dict: dict[str, Any],
    bp_warnings: Any = None,
) -> None:
    """In-place: add generic BP-skill-guide hint + auto-embed any
    BP-referenced section bodies into a write-tool error response dict.

    Appends ``_WRITE_TOOL_BP_HINT_SUGGESTION`` to ``error_dict["error"]
    ["suggestions"]`` (idempotent — won't double-append) and keeps the
    legacy singular ``suggestion`` field aligned with the first entry.

    When ``bp_warnings`` has ``referenced_files``, auto-embeds the
    matching markdown sections under ``skill_content`` with
    ``skill_content_hint`` at the top of the response, matching the
    success-path attach shape. Canonical files are NOT attached on
    errors (targeted section bodies are 1-5 KB and carry the fix
    material; the 25-37 KB canonical bundle would bloat errors without
    matching benefit). The opt-out hint is still placed first so the
    LLM knows about ``MandatoryBPS=false`` for its next successful call.

    No-op when ``error_dict`` doesn't have a nested ``error`` dict
    (defensive — preserves the contract for any caller passing a
    non-standard error shape).
    """
    err = error_dict.get("error")
    if not isinstance(err, dict):
        return
    suggestions = err.setdefault("suggestions", [])
    # create_error_response emits only the singular field for one suggestion.
    # Preserve that recovery advice before appending the generic skill hint.
    primary = err.get("suggestion")
    if not suggestions and isinstance(primary, str) and primary:
        suggestions.append(primary)
    if _WRITE_TOOL_BP_HINT_SUGGESTION not in suggestions:
        suggestions.append(_WRITE_TOOL_BP_HINT_SUGGESTION)
    if suggestions:
        err["suggestion"] = suggestions[0]

    referenced_files = getattr(bp_warnings, "referenced_files", None)
    if referenced_files:
        attach_skill_content(
            error_dict,
            MandatoryBPS=False,
            canonical_files=(),
            referenced_files=referenced_files,
        )


def augment_tool_error_with_skill_content(
    te: ToolError,
    bp_warnings: Any = None,
) -> ToolError:
    """Wrap :func:`augment_error_dict_with_skill_content` around a ``ToolError``.

    Each write tool wraps its method body in:

        try:
            ...
            return result
        except ToolError as te:
            raise augment_tool_error_with_skill_content(te, bp_warnings) from None

    Decodes the error JSON, applies the dict augmentation, re-encodes,
    and returns a NEW ``ToolError`` to be raised with ``from None`` so
    the original exception chain isn't surfaced. Falls through to
    returning the original ``te`` when the body isn't JSON-decodable
    as a structured error dict.
    """
    try:
        error_dict = json.loads(str(te))
    except (json.JSONDecodeError, TypeError):
        return te
    if not isinstance(error_dict, dict):
        return te
    augment_error_dict_with_skill_content(error_dict, bp_warnings)
    return ToolError(
        json.dumps(error_dict, indent=2, default=str), log_level=te.log_level
    )


@asynccontextmanager
async def config_reload_waiter(
    client: Any, event_type: str, *, enabled: bool = True, timeout: float = 10.0
) -> AsyncIterator[Callable[[], Awaitable[bool | None]]]:
    """Subscribe to a ``<domain>_reloaded`` event around a config write.

    Home Assistant answers ``POST /config/<domain>/config/<id>`` before the
    reload it schedules has run, and that reload replaces entities (all scenes;
    an automation whose config changed). A runtime action sent in between hits
    no entity and is lost, so callers subscribe before the write and wait on
    the yielded callable before acting on the entity. HA fires
    ``automation_reloaded`` and ``scene_reloaded`` after every such reload;
    scripts fire no reload event, so they cannot use this.

    The callable returns True once a reload completed, False on timeout, and
    None when no subscription exists (``enabled`` False or no WebSocket).
    """
    reloaded = asyncio.Event()

    async def handler(event: dict[str, Any]) -> None:
        reloaded.set()

    ws_client = await _get_waiter_ws_client(client) if enabled else None
    attached_handlers: list[str] = []
    sub_ids: list[int] = []
    subscribed = ws_client is not None and await _ws_subscribe_all(
        ws_client,
        (event_type,),
        handler,
        attached_handlers,
        sub_ids,
        "config reload",
        event_type,
    )

    async def wait_for_reload() -> bool | None:
        if not subscribed:
            return None
        try:
            await asyncio.wait_for(reloaded.wait(), timeout=timeout)
        except TimeoutError:
            logger.warning("%s not received within %ss", event_type, timeout)
            return False
        return True

    try:
        yield wait_for_reload
    finally:
        if ws_client is not None:
            await _ws_cleanup(ws_client, attached_handlers, sub_ids, handler)


def note_reload_outcome(
    result: dict[str, Any], reloaded: bool | None, *, domain: str, requested: bool
) -> None:
    """Warn when a runtime change could not be ordered after a config reload.

    ``reloaded`` is the ``config_reload_waiter`` verdict: False when the
    reload was never confirmed, None when nothing could watch for it.
    """
    if not requested or reloaded:
        return
    reason = (
        f"did not confirm the {domain} reload"
        if reloaded is False
        else f"could not be watched for the {domain} reload (no WebSocket)"
    )
    result.setdefault("warnings", []).append(
        f"Home Assistant {reason}; the requested runtime change may be "
        "reverted or lost when the reload completes."
    )
