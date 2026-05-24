"""Starlette handler factories for /api/policy/*."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .approval_queue import ApprovalQueue
from .model import Policy
from .persistence import load_policy, save_policy
from .value_sources import (
    all_value_sources_for,
    fetch_value_source,
)


def _is_write_or_destructive(tool: Any) -> bool:
    """True iff the tool can mutate state (not read-only) — the only
    surface gating-UX polish is worth investing in. Read-only tools
    still gate correctly if a user adds a rule, they just get the
    free-text predicate fallback in the UI."""
    ann = getattr(tool, "annotations", None)
    # No annotations = treat as potentially write (safe default for the
    # UI, matches the runtime gate which doesn't skip read-only).
    return ann is None or getattr(ann, "readOnlyHint", None) is not True


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


def build_policy_handlers(
    *,
    data_dir: Path,
    queue: ApprovalQueue,
    server: Any | None = None,
) -> dict[str, Callable[[Request], Any]]:

    async def get_config(_: Request) -> JSONResponse:
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

    async def put_config(request: Request) -> JSONResponse:
        try:
            new_policy = Policy.model_validate(await request.json())
        except (ValidationError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        # Optimistic concurrency: reject if the on-disk version moved
        # between this caller's GET and PUT. Returns the current policy
        # so the client can rebase if it wants to retry.
        current = load_policy(data_dir)
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
        return JSONResponse({"saved": True, "version": new_policy.version + 1})

    async def get_pending(_: Request) -> JSONResponse:
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

    async def post_approve(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must be a JSON object"}, status_code=400
            )
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

    async def post_deny(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must be a JSON object"}, status_code=400
            )
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

    async def get_tool_schema(request: Request) -> JSONResponse:
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
        try:
            tools = await server.mcp.local_provider._list_tools()
        except Exception as e:
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

    async def get_value_source(request: Request) -> JSONResponse:
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
            values = await fetch_value_source(
                source, client=server.client, params=params
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, 400)
        except Exception as e:
            return JSONResponse({"error": f"value-source fetch failed: {e}"}, 502)
        return JSONResponse({"source": source, "values": values})

    return {
        "policy_get_config": get_config,
        "policy_put_config": put_config,
        "policy_get_pending": get_pending,
        "policy_post_approve": post_approve,
        "policy_post_deny": post_deny,
        "policy_get_tool_schema": get_tool_schema,
        "policy_get_value_source": get_value_source,
    }
