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


def build_policy_handlers(
    *,
    data_dir: Path,
    queue: ApprovalQueue,
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

    return {
        "policy_get_config": get_config,
        "policy_put_config": put_config,
        "policy_get_pending": get_pending,
        "policy_post_approve": post_approve,
        "policy_post_deny": post_deny,
    }
