"""Home context tools: versioned descriptions of the home that agents keep current."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, NoReturn

from pydantic import Field

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.tools import tool

from ..errors import ErrorCode, create_error_response
from .component_home_context import (
    WS_HOME_CONTEXT_GET,
    WS_HOME_CONTEXT_WRITE,
    send,
)
from .helpers import log_tool_usage, raise_tool_error, register_tool_methods
from .tool_hints import read_only_hints, write_hints

# Mirrors the component's SCOPE_PATTERN so a typo gets a usable error here.
_SCOPE = re.compile(r"^(home|(area|floor|device|entity|topic):[a-z0-9_.\-]{1,200})$")
SCOPE_DESCRIPTION = (
    "What the document describes: 'home' for the whole house, or "
    "'area:<area_id>', 'floor:<floor_id>', 'device:<device_id>', "
    "'entity:<entity_id>' or 'topic:<slug>' (e.g. 'topic:heating'). "
    "Lowercase letters, digits, '_', '-' and '.' only."
)


def _client_name(ctx: Context | None) -> str | None:
    try:
        info = ctx.session.client_params.clientInfo  # type: ignore[union-attr]
    except AttributeError:
        return None
    name = getattr(info, "name", None)
    return name if isinstance(name, str) and name else None


def _raise_invalid(message: str, **context: Any) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER, message, context=context
        )
    )


def _check_scope(scope: str) -> None:
    if not _SCOPE.match(scope):
        _raise_invalid(f"Invalid scope {scope!r}. {SCOPE_DESCRIPTION}", scope=scope)


def _raise_component_error(scope: str, result: dict[str, Any]) -> NoReturn:
    error = result.get("error") or {}
    code = error.get("code")
    latest = error.get("latest_revision")
    context = {
        "scope": scope,
        **({"latest_revision": latest} if latest is not None else {}),
    }
    if code == "conflict":
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                error.get("message", f"{scope} changed since it was read"),
                context={**context, "conflict": True},
                suggestions=[
                    f"Read the current text with ha_get_home_context(scope='{scope}'), "
                    + "apply your change to it and retry with "
                    + f"expected_revision={latest}",
                ],
            )
        )
    if code == "not_found":
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                error.get("message", f"{scope} not found"),
                context={**context, "kept_revisions": error.get("kept_revisions")},
            )
        )
    _raise_invalid(
        error.get("message", "The home context write was refused"), **context
    )


class HomeContextTools:
    """Read and edit the versioned home context documents."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _get(
        self, scope: str, revision: int | None = None, include_history: bool = False
    ) -> dict[str, Any]:
        _check_scope(scope)
        kwargs: dict[str, Any] = {"scope": scope, "include_history": include_history}
        if revision is not None:
            kwargs["revision"] = revision
        result = await send(self._client, WS_HOME_CONTEXT_GET, **kwargs)
        if result.get("success") is not True:
            _raise_component_error(scope, result)
        return result

    @tool(
        name="ha_get_home_context",
        tags={"Home Context"},
        annotations=read_only_hints("Get Home Context", open_world=False),
    )
    @log_tool_usage
    async def ha_get_home_context(
        self,
        scope: Annotated[
            str | None,
            Field(
                description=SCOPE_DESCRIPTION
                + " Omit to list every stored document with its revision.",
                default=None,
            ),
        ] = None,
        revision: Annotated[
            int | None,
            Field(
                description="An older revision to read instead of the current one.",
                default=None,
                ge=1,
            ),
        ] = None,
        include_history: Annotated[
            bool,
            Field(
                description="Also list the stored revisions with author and note.",
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Get what is known about the home itself: how rooms, devices and systems
        are set up and the rules the household wants kept.

        Call this before changing anything in the home (automations, scripts,
        scenes, helpers, devices, dashboards) and use the returned
        `latest_revision` as `expected_revision` when you update the document
        with ha_manage_home_context. A scope's result also includes the
        documents of the broader scopes it belongs to (`related`: device, area,
        floor and 'home'), so reading the narrowest scope is enough. Without a
        scope, the call lists the stored documents; an empty list means nobody
        has described the home yet.
        """
        if scope is None:
            result = await send(self._client, WS_HOME_CONTEXT_GET)
            scopes = result.get("scopes") or []
            return {"success": True, "scopes": scopes, "count": len(scopes)}
        return await self._get(scope, revision, include_history)

    @tool(
        name="ha_manage_home_context",
        tags={"Home Context"},
        annotations=write_hints(
            "Manage Home Context",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )
    @log_tool_usage
    async def ha_manage_home_context(
        self,
        action: Annotated[
            Literal["set", "append", "replace_text", "restore", "delete"],
            Field(
                description="'set' replaces the whole text, 'append' adds a "
                "section at the end, 'replace_text' swaps one exact passage, "
                "'restore' makes an older revision current again, 'delete' "
                "removes the document (its history is kept)."
            ),
        ],
        scope: Annotated[str, Field(description=SCOPE_DESCRIPTION)],
        expected_revision: Annotated[
            int,
            Field(
                description="The `latest_revision` you last read; 0 to create a "
                "new document. The write is refused when the document changed "
                "since.",
                ge=0,
            ),
        ],
        content: Annotated[
            str | None,
            Field(
                description="Markdown text: the full document for 'set', the "
                "added section for 'append', the replacement for 'replace_text'.",
                default=None,
            ),
        ] = None,
        find: Annotated[
            str | None,
            Field(
                description="For 'replace_text': the exact passage to replace; "
                "it must occur exactly once.",
                default=None,
            ),
        ] = None,
        revision: Annotated[
            int | None,
            Field(
                description="For 'restore': the revision to restore.",
                default=None,
                ge=1,
            ),
        ] = None,
        note: Annotated[
            str | None,
            Field(
                description="Short change note stored with the revision, e.g. "
                "'added morning heating schedule'.",
                default=None,
                max_length=500,
            ),
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create, update, restore or delete a home context document.

        Do not use it to record what Home Assistant already stores (entity
        names, areas, automation configs); write down what an agent cannot see
        there: purpose, constraints, household preferences, wiring and
        dependencies between systems. Read the document with
        ha_get_home_context first and pass its `latest_revision` as
        `expected_revision`. Update the document whenever a change you made to
        the home makes it outdated. Every write adds a revision, so 'restore'
        can undo one.

        EXAMPLES:
        - ha_manage_home_context(action="set", scope="area:bathroom", expected_revision=0, content="# Bathroom\\nFloor heating reacts slowly; never set below 19 °C.")
        - ha_manage_home_context(action="append", scope="topic:pv", expected_revision=3, content="## Battery\\nKeep 20 % reserve overnight.", note="battery reserve")
        - ha_manage_home_context(action="restore", scope="home", expected_revision=7, revision=5)
        """
        new_content = await self._new_content(
            action, scope, expected_revision, content, find, revision
        )
        payload: dict[str, Any] = {
            "scope": scope,
            "content": new_content,
            "expected_revision": expected_revision,
            "note": note
            or (f"restored revision {revision}" if action == "restore" else None),
            "author": _client_name(ctx),
        }
        result = await send(self._client, WS_HOME_CONTEXT_WRITE, **payload)
        if result.get("success") is not True:
            _raise_component_error(scope, result)
        return {
            "success": True,
            "action": action,
            "scope": scope,
            "revision": result.get("revision"),
            "previous_revision": result.get("previous_revision"),
            "deleted": bool(result.get("deleted")),
        }

    async def _new_content(
        self,
        action: str,
        scope: str,
        expected_revision: int,
        content: str | None,
        find: str | None,
        revision: int | None,
    ) -> str | None:
        """The full text to store, or ``None`` for a delete."""
        _check_scope(scope)
        if action == "delete":
            return None
        if action == "restore":
            return await self._restored_content(scope, revision)
        if not content:
            _raise_invalid(f"action='{action}' requires content", scope=scope)
        if action == "set":
            return content
        current = await self._get(scope)
        latest = current["latest_revision"]
        if latest != expected_revision:
            _raise_component_error(
                scope,
                {
                    "error": {
                        "code": "conflict",
                        "message": f"{scope} is at revision {latest}, "
                        f"not {expected_revision}",
                        "latest_revision": latest,
                    }
                },
            )
        text = (current.get("document") or {}).get("content") or ""
        return edit_text(action, scope, text, content, find)

    async def _restored_content(self, scope: str, revision: int | None) -> str:
        if revision is None:
            _raise_invalid("action='restore' requires revision", scope=scope)
        old = (await self._get(scope, revision))["document"]
        if old is None or old.get("content") is None:
            _raise_invalid(f"Revision {revision} of {scope} is a deletion", scope=scope)
        return str(old["content"])


def edit_text(
    action: str, scope: str, text: str, content: str, find: str | None
) -> str:
    """Apply an 'append' or 'replace_text' edit to the current document text."""
    if action == "append":
        return f"{text.rstrip()}\n\n{content}" if text else content
    if not find:
        _raise_invalid("action='replace_text' requires find", scope=scope)
    occurrences = text.count(find)
    if occurrences != 1:
        _raise_invalid(
            f"find occurs {occurrences} times in {scope}; it must occur exactly once",
            scope=scope,
            occurrences=occurrences,
        )
    return text.replace(find, content)


def register_home_context_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the home context tools and the reminder on write results."""
    from ..config import get_global_settings
    from .home_context_hint_middleware import HomeContextHintMiddleware

    if not get_global_settings().enable_home_context:
        return

    register_tool_methods(mcp, HomeContextTools(client))

    async def list_all_tools() -> Any:
        return await mcp.local_provider._list_tools()

    mcp.add_middleware(
        HomeContextHintMiddleware(get_client=lambda: client, list_tools=list_all_tools)
    )
