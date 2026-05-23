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
    on_policy_change: Callable[[Policy], None] | None = None,
) -> dict[str, Callable[[Request], Any]]:

    async def get_config(_: Request) -> JSONResponse:
        return JSONResponse(load_policy(data_dir).model_dump(mode="json"))

    async def put_config(request: Request) -> JSONResponse:
        try:
            policy = Policy.model_validate(await request.json())
        except (ValidationError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        save_policy(data_dir, policy)
        if on_policy_change:
            on_policy_change(policy)
        return JSONResponse({"saved": True})

    async def get_pending(_: Request) -> JSONResponse:
        return JSONResponse({"pending": [
            {
                "token": e.token,
                "tool_name": e.tool_name,
                "args_preview": e.args_preview,
                "created_at": e.created_at.isoformat(),
                "expires_at": e.expires_at.isoformat(),
            }
            for e in queue.list_pending()
        ]})

    async def post_approve(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        token = body.get("token")
        if not token or queue.get(token) is None:
            return JSONResponse({"error": "unknown token"}, status_code=404)
        queue.approve(token)
        # remember_minutes is applied by middleware on the next call,
        # which is when the matching rule's remember_minutes is read.
        return JSONResponse({"approved": True})

    async def post_deny(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        token = body.get("token")
        if not token or queue.get(token) is None:
            return JSONResponse({"error": "unknown token"}, status_code=404)
        queue.deny(token)
        return JSONResponse({"denied": True})

    return {
        "policy_get_config": get_config,
        "policy_put_config": put_config,
        "policy_get_pending": get_pending,
        "policy_post_approve": post_approve,
        "policy_post_deny": post_deny,
    }
