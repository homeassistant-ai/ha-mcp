"""Starlette handler factories for /api/policy/*."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from anyio.to_thread import run_sync as run_in_thread
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..utils.config_write_lock import config_write_guard
from .approval_queue import ApprovalQueue
from .decision_pin import clear_pin, is_pin_set, pin_status, set_pin, validate_pin
from .model import Policy
from .persistence import load_policy, save_policy
from .value_sources import (
    all_value_sources_for,
    fetch_value_source,
)

logger = logging.getLogger(__name__)


def _is_write_or_destructive(tool: Any) -> bool:
    """True iff the tool may mutate state. The UI only invests in path /
    value pickers for these; read-only tools still gate correctly if a
    user adds a rule, they just get the free-text predicate fallback in
    the UI."""
    ann = getattr(tool, "annotations", None)
    # No annotations = treat as potentially write (safe default for the
    # UI, matches the runtime gate which doesn't skip read-only).
    return ann is None or ann.read_only_hint is not True


def _extract_arg_paths(parameters: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a tool's JSON-schema ``parameters`` into a list of
    ``{path, type, enum, description, required}`` entries the UI can
    render as a dropdown.

    Only top-level properties are surfaced — nested objects exist in a
    few tools but the predicate language already supports dotted paths,
    so users can fall back to free-text for those if needed.
    """
    if not isinstance(parameters, dict):
        return []
    props = parameters.get("properties") or {}
    required = set(parameters.get("required") or [])
    out: list[dict[str, Any]] = []
    for name, schema in props.items():
        if not isinstance(schema, dict):
            continue
        out.append(
            {
                "path": f"args.{name}",
                "label": name,
                "type": schema.get("type"),
                "enum": schema.get("enum"),
                "description": (schema.get("description") or "")[:200],
                "required": name in required,
            }
        )
    return out


async def _get_config(data_dir: Path) -> JSONResponse:
    try:
        return JSONResponse(load_policy(data_dir).model_dump(mode="json"))
    except ValueError as e:
        # Surface a corrupt or schema-invalid tool_policy.json to the
        # UI so the user has a visible repair path; without this the
        # tab would just spinner forever on an opaque 500.
        return JSONResponse(
            {"error": str(e), "policy_file_corrupt": True},
            status_code=500,
        )


async def _put_config(
    data_dir: Path, queue: ApprovalQueue, request: Request
) -> JSONResponse:
    try:
        new_policy = Policy.model_validate(await request.json())
    except (ValidationError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    # Optimistic concurrency: reject if the on-disk version moved
    # between this caller's GET and PUT. Returns the current policy
    # so the client can rebase if it wants to retry.
    # Serialize the version-check + save against the developer tool
    # (set_policy / set_tool) AND against other processes (the stdio
    # sidecar runs this same handler in its own process) so a concurrent
    # writer can't slip between the read and the write and lose an update.
    async with config_write_guard():
        current = load_policy(data_dir)
        # Inside the guard: DELETE /api/policy/decision-pin clears the PIN
        # under this same lock, and when the stored policy already has the
        # switch off it does so without bumping the version. Checked before
        # the lock, that delete could land in between and this write would
        # then persist the switch with no PIN behind it -- the version check
        # would not notice, because nothing about the policy changed.
        if new_policy.event_decisions_enabled and not is_pin_set(data_dir):
            # Enabling without a PIN would advertise a channel that decides
            # nothing (the listener refuses every event without one), so the
            # tab would show a switch the server does not honour.
            return JSONResponse(
                {
                    "error": (
                        "set an approval PIN before allowing approve/deny over "
                        "the Home Assistant event bus"
                    ),
                    "pin_required": True,
                },
                status_code=400,
            )
        if new_policy.version != current.version:
            return JSONResponse(
                {
                    "error": "policy version mismatch — reload before saving",
                    "current_version": current.version,
                    "current_policy": current.model_dump(mode="json"),
                },
                status_code=409,
            )
        save_policy(data_dir, new_policy)
        # Drop the remember-cache only when rules actually changed.
        # Editing just wait_seconds / approval_ttl_minutes shouldn't
        # invalidate in-flight remembered approvals; only a rule change
        # could make a previously-approved call now want a different
        # outcome.
        if current.rules != new_policy.rules:
            queue.clear_remember_cache()
    return JSONResponse({"saved": True, "version": new_policy.version + 1})


async def _get_pending(queue: ApprovalQueue) -> JSONResponse:
    return JSONResponse(
        {
            "pending": [
                {
                    "token": e.token,
                    "tool_name": e.tool_name,
                    "args": e.args,
                    "created_at": e.created_at.isoformat(),
                    "expires_at": e.expires_at.isoformat(),
                }
                for e in queue.list_pending()
            ]
        }
    )


async def _post_approve(queue: ApprovalQueue, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    token = body.get("token")
    if not token:
        return JSONResponse({"error": "unknown token"}, status_code=404)
    entry = queue.get(token)
    if entry is None:
        return JSONResponse({"error": "unknown token"}, status_code=404)
    if not queue.approve(token):
        # Token exists but already decided (idempotent retry, or a
        # second approver hitting the button after the first). 409
        # so the UI can show "already approved/denied" rather than a
        # generic 500.
        return JSONResponse(
            {"error": "already decided", "current_decision": entry.decision},
            status_code=409,
        )
    # remember_minutes is applied by middleware as soon as the blocked
    # call wakes up (event.set in approve fires the wait); later calls
    # within the window hit the remember cache and bypass approval entirely.
    return JSONResponse({"approved": True})


async def _post_deny(queue: ApprovalQueue, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    token = body.get("token")
    if not token:
        return JSONResponse({"error": "unknown token"}, status_code=404)
    entry = queue.get(token)
    if entry is None:
        return JSONResponse({"error": "unknown token"}, status_code=404)
    if not queue.deny(token):
        return JSONResponse(
            {"error": "already decided", "current_decision": entry.decision},
            status_code=409,
        )
    return JSONResponse({"denied": True})


async def _get_decision_pin(data_dir: Path) -> JSONResponse:
    """Whether a PIN exists, and when it was last set. Never the PIN itself."""
    return JSONResponse(pin_status(data_dir))


async def _post_decision_pin(data_dir: Path, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    try:
        pin = validate_pin(body.get("pin"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    async with config_write_guard():
        # Off the event loop: deriving the digest is 200k PBKDF2 rounds --
        # a quarter of a second of pure CPU by design -- and the file write
        # blocks on top of that. Verification already runs in a worker; this
        # is the other half. The guard stays outside, so the write is still
        # serialised against the policy writers it shares a lock with.
        try:
            await run_in_thread(set_pin, data_dir, pin)
        except OSError as e:
            logger.exception("approval PIN could not be stored")
            return JSONResponse(
                {
                    "error": f"could not store the approval PIN: {e}",
                    "storage_failed": True,
                },
                status_code=500,
            )
    logger.info("approval PIN set (event-bus decisions)")
    return JSONResponse(pin_status(data_dir))


async def _delete_decision_pin(data_dir: Path) -> JSONResponse:
    """Remove the PIN, and with it the switch that depends on it.

    Leaving ``event_decisions_enabled`` on while the PIN is gone would
    leave the tab claiming a channel that now refuses every event, so the
    two are cleared together rather than drifting apart.

    Which is why the policy is read BEFORE anything is deleted: a corrupt
    policy file raises, and raising after the delete would remove the PIN
    and report a 500, leaving the caller to guess what happened. Both
    remaining failures are reported for what they are -- the delete itself
    failing changes nothing, while a failing save leaves the PIN gone and
    the toggle still persisted, which the user has to know about because
    the tab would otherwise show a channel that no longer has a PIN.
    """
    async with config_write_guard():
        try:
            policy = load_policy(data_dir)
        except ValueError as e:
            return JSONResponse(
                {
                    "error": (
                        f"the policy file must be readable before the PIN can "
                        f"be removed: {e}"
                    ),
                    "policy_file_corrupt": True,
                },
                status_code=500,
            )
        disabled = policy.event_decisions_enabled
        try:
            existed = clear_pin(data_dir)
        except OSError as e:
            logger.exception("approval PIN could not be removed")
            return JSONResponse(
                {
                    "error": f"could not remove the approval PIN: {e}",
                    "storage_failed": True,
                },
                status_code=500,
            )
        if disabled:
            try:
                save_policy(
                    data_dir,
                    policy.model_copy(update={"event_decisions_enabled": False}),
                )
            except OSError as e:
                logger.exception(
                    "approval PIN removed, but the event-decisions toggle "
                    "could not be switched off with it"
                )
                return JSONResponse(
                    {
                        "error": (
                            f"the PIN was removed, but switching off deciding "
                            f"over the event bus failed: {e}. The channel is "
                            f"closed either way -- every event is refused "
                            f"without a PIN -- but the saved setting still "
                            f"says it is on. Save the global settings again "
                            f"to correct it."
                        ),
                        "pin_removed": True,
                        "event_decisions_disabled": False,
                        "storage_failed": True,
                    },
                    status_code=500,
                )
    if existed:
        logger.info(
            "approval PIN removed%s",
            "; deciding from the event bus was switched off with it"
            if disabled
            else "",
        )
    return JSONResponse({"set": False, "event_decisions_disabled": disabled})


async def _get_tool_schema(server: Any | None, request: Request) -> JSONResponse:
    """Return the predicate-builder hints for one tool.

    Powers the schema-driven path/value pickers in the Tool Security
    Policies tab. Returns ``paths: []`` for read-only tools so the
    UI knows to hide the pickers (free-text fallback still works).
    Returns 503 when the sidecar/stub backend is in use — the
    sidecar has no FastMCP registry to introspect.
    """
    name = request.query_params.get("name") or ""
    if not name:
        return JSONResponse({"error": "missing 'name' query param"}, 400)
    if server is None:
        return JSONResponse(
            {"error": "tool schema introspection unavailable in this mode"},
            503,
        )
    # `local_provider._list_tools()` is a private FastMCP API but the
    # public ``mcp.list_tools()`` filters out tools the operator has
    # disabled via the Tools tab — the same disabled tools the user
    # may still want to author gating rules for.
    try:
        tools = await server.mcp.local_provider._list_tools()
    except Exception as e:
        logger.exception("tool-schema: failed to list tools")
        return JSONResponse({"error": f"tool list failed: {e}"}, 500)
    tool = next((t for t in tools if getattr(t, "name", None) == name), None)
    if tool is None:
        return JSONResponse({"error": f"tool not found: {name}"}, 404)
    if not _is_write_or_destructive(tool):
        return JSONResponse(
            {
                "tool_name": name,
                "is_write_or_destructive": False,
                "paths": [],
                "value_sources": {},
            }
        )
    return JSONResponse(
        {
            "tool_name": name,
            "is_write_or_destructive": True,
            "paths": _extract_arg_paths(getattr(tool, "parameters", {}) or {}),
            "value_sources": all_value_sources_for(name),
        }
    )


async def _get_value_source(server: Any | None, request: Request) -> JSONResponse:
    """Return live legal values for a named value source.

    Sources are defined in ``policy/value_sources.py``. Extra query
    params (e.g. ``domain=light``) are passed through to the
    fetcher to support cascading selects.
    """
    source = request.query_params.get("source") or ""
    if not source:
        return JSONResponse({"error": "missing 'source' query param"}, 400)
    if server is None:
        return JSONResponse(
            {"error": "value-source fetch unavailable in this mode"}, 503
        )
    params = {k: v for k, v in request.query_params.items() if k != "source"}
    try:
        values = await fetch_value_source(source, client=server.client, params=params)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    except Exception as e:
        logger.exception(
            "value-source fetch failed: source=%s params=%s", source, params
        )
        return JSONResponse({"error": f"value-source fetch failed: {e}"}, 502)
    return JSONResponse({"source": source, "values": values})


def build_decision_pin_handlers(
    *, data_dir: Path
) -> dict[str, Callable[[Request], Any]]:
    """The PIN endpoints, which need no approval queue.

    Split out because the sidecar's stub handler set serves these for real
    while 503-ing everything that touches the in-memory queue: the PIN is a
    file in the data dir, and the config PUT next to it refuses to enable
    event-bus decisions without one.
    """

    async def get_decision_pin(_: Request) -> JSONResponse:
        return await _get_decision_pin(data_dir)

    async def post_decision_pin(request: Request) -> JSONResponse:
        return await _post_decision_pin(data_dir, request)

    async def delete_decision_pin(_: Request) -> JSONResponse:
        return await _delete_decision_pin(data_dir)

    return {
        "policy_get_decision_pin": get_decision_pin,
        "policy_post_decision_pin": post_decision_pin,
        "policy_delete_decision_pin": delete_decision_pin,
    }


def build_policy_handlers(
    *,
    data_dir: Path,
    queue: ApprovalQueue,
    server: Any | None = None,
) -> dict[str, Callable[[Request], Any]]:

    async def get_config(_: Request) -> JSONResponse:
        return await _get_config(data_dir)

    async def put_config(request: Request) -> JSONResponse:
        return await _put_config(data_dir, queue, request)

    async def get_pending(_: Request) -> JSONResponse:
        return await _get_pending(queue)

    async def post_approve(request: Request) -> JSONResponse:
        return await _post_approve(queue, request)

    async def post_deny(request: Request) -> JSONResponse:
        return await _post_deny(queue, request)

    async def get_tool_schema(request: Request) -> JSONResponse:
        return await _get_tool_schema(server, request)

    async def get_value_source(request: Request) -> JSONResponse:
        return await _get_value_source(server, request)

    return {
        "policy_get_config": get_config,
        "policy_put_config": put_config,
        "policy_get_pending": get_pending,
        "policy_post_approve": post_approve,
        "policy_post_deny": post_deny,
        "policy_get_tool_schema": get_tool_schema,
        "policy_get_value_source": get_value_source,
        **build_decision_pin_handlers(data_dir=data_dir),
    }
