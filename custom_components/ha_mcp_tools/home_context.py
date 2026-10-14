"""Versioned descriptions of the home itself, kept in Home Assistant's storage.

Home Assistant knows the entities, devices and areas, but not what a person
knows about the house: that the bathroom floor heating is slow and must never
drop below 19 °C, or how the PV automations depend on each other. A home
context document holds that knowledge as Markdown for one scope (``home``, an
area, floor, device, entity or free-form topic), so an agent can read it
before it changes that part of the house and update it afterwards.

Every write appends a revision. A write names the revision it was based on
(``expected_revision``); when another writer got there first, the write is
refused with ``conflict`` and the caller has to read the current text again.
A delete is a revision without content, so the history survives it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

WS_HOME_CONTEXT_GET = "ha_mcp_tools/home_context_get"
WS_HOME_CONTEXT_WRITE = "ha_mcp_tools/home_context_write"
WS_HOME_CONTEXT_MATCH = "ha_mcp_tools/home_context_match"
COMMANDS = (WS_HOME_CONTEXT_GET, WS_HOME_CONTEXT_WRITE, WS_HOME_CONTEXT_MATCH)
CAPABILITIES = ("home_context",)

STORAGE_KEY = "ha_mcp_tools_home_context"
STORAGE_VERSION = 1
_DATA_KEY = "ha_mcp_tools_home_context_book"

HOME_SCOPE = "home"
SCOPE_KINDS = ("area", "floor", "device", "entity", "topic")
SCOPE_PATTERN = re.compile(
    r"^(home|(area|floor|device|entity|topic):[a-z0-9_.\-]{1,200})$"
)

# A single frame stays well under the WS body cap with every revision kept.
MAX_CONTENT_CHARS = 20_000
MAX_NOTE_CHARS = 500
MAX_HISTORY = 50
MAX_SCOPES = 1_000
MAX_MATCH_IDS = 200
_SUMMARY_CHARS = 120


def _failure(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": {"code": code, "message": message, **extra}}


def _summary(content: str) -> str:
    for line in content.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:_SUMMARY_CHARS]
    return ""


def _public(scope: str, rev: dict[str, Any]) -> dict[str, Any]:
    return {"scope": scope, **rev}


def _index_row(scope: str, rev: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": scope,
        "revision": rev["revision"],
        "updated_at": rev["updated_at"],
        "author": rev.get("author"),
        "summary": _summary(rev["content"]),
        "chars": len(rev["content"]),
    }


class HomeContextBook:
    """The stored scopes, each a list of revisions with the newest last."""

    def __init__(self, store: Any, now: Callable[[], str]) -> None:
        self._store = store
        self._now = now
        self._scopes: dict[str, list[dict[str, Any]]] | None = None
        self._lock = asyncio.Lock()

    async def _load(self) -> dict[str, list[dict[str, Any]]]:
        if self._scopes is None:
            data = await self._store.async_load()
            scopes = data.get("scopes") if isinstance(data, dict) else None
            self._scopes = {
                scope: list(revisions)
                for scope, revisions in (scopes or {}).items()
                if isinstance(revisions, list) and revisions
            }
        return self._scopes

    def current(self, scope: str) -> dict[str, Any] | None:
        revisions = (self._scopes or {}).get(scope)
        if not revisions or revisions[-1].get("content") is None:
            return None
        return revisions[-1]

    def latest_revision(self, scope: str) -> int:
        revisions = (self._scopes or {}).get(scope)
        return int(revisions[-1]["revision"]) if revisions else 0

    async def index(self) -> list[dict[str, Any]]:
        await self._load()
        rows = []
        for scope in sorted(self._scopes or {}):
            current = self.current(scope)
            if current is not None:
                rows.append(_index_row(scope, current))
        return rows

    async def existing(self, scopes: list[str]) -> list[dict[str, Any]]:
        """Index rows for the scopes in ``scopes`` that have content, in order."""
        await self._load()
        rows = []
        for scope in dict.fromkeys(scopes):
            current = self.current(scope)
            if current is not None:
                rows.append(_index_row(scope, current))
        return rows

    async def get(
        self, scope: str, revision: int | None, include_history: bool
    ) -> dict[str, Any]:
        scopes = await self._load()
        revisions = scopes.get(scope) or []
        if revision is None:
            doc = self.current(scope)
        else:
            doc = next((r for r in revisions if r["revision"] == revision), None)
        result: dict[str, Any] = {
            "success": True,
            "scope": scope,
            "exists": self.current(scope) is not None,
            "latest_revision": self.latest_revision(scope),
            "document": _public(scope, doc) if doc is not None else None,
        }
        if revision is not None and doc is None:
            return _failure(
                "not_found",
                f"Revision {revision} of {scope} is not stored",
                latest_revision=result["latest_revision"],
                kept_revisions=[r["revision"] for r in revisions],
            )
        if include_history:
            result["history"] = [
                {
                    "revision": r["revision"],
                    "updated_at": r["updated_at"],
                    "author": r.get("author"),
                    "note": r.get("note"),
                    "deleted": r.get("content") is None,
                }
                for r in reversed(revisions)
            ]
        return result

    async def write(
        self,
        scope: str,
        content: str | None,
        expected_revision: int,
        note: str | None,
        author: str | None,
        user_id: str | None,
    ) -> dict[str, Any]:
        async with self._lock:
            scopes = await self._load()
            latest = self.latest_revision(scope)
            if expected_revision != latest:
                return _failure(
                    "conflict",
                    f"{scope} is at revision {latest}, not {expected_revision}; "
                    "read the current text and apply the change to it",
                    latest_revision=latest,
                )
            if content is None and self.current(scope) is None:
                return _failure("not_found", f"{scope} has no content to delete")
            if scope not in scopes and len(scopes) >= MAX_SCOPES:
                return _failure("limit", f"At most {MAX_SCOPES} scopes are stored")
            rev = {
                "revision": latest + 1,
                "content": content,
                "updated_at": self._now(),
                "author": author,
                "user_id": user_id,
                "note": note,
            }
            revisions = [*scopes.get(scope, []), rev][-MAX_HISTORY:]
            updated = {**scopes, scope: revisions}
            await self._store.async_save({"scopes": updated})
            self._scopes = updated
        return {
            "success": True,
            "scope": scope,
            "revision": rev["revision"],
            "previous_revision": latest,
            "deleted": content is None,
        }


def _now_iso() -> str:
    from homeassistant.util import dt as dt_util

    return str(dt_util.utcnow().isoformat())


def get_book(hass: HomeAssistant, store_factory: Callable[[], Any]) -> HomeContextBook:
    """The one book per Home Assistant instance, shared by both entry types."""
    book = hass.data.get(_DATA_KEY)
    if not isinstance(book, HomeContextBook):
        book = HomeContextBook(store_factory(), _now_iso)
        hass.data[_DATA_KEY] = book
    return book


def _attr(entry: Any, name: str) -> str | None:
    value = getattr(entry, name, None) if entry is not None else None
    return value if isinstance(value, str) and value else None


def ancestors(registries: Any, scope: str) -> list[str]:
    """The broader scopes ``scope`` belongs to, narrowest first, ending in home.

    An entity without its own area inherits its device's area, as Home
    Assistant's own area resolution does.
    """
    kind, _, ident = scope.partition(":")
    chain: list[str] = []
    device_id = area_id = floor_id = None
    if kind == "entity":
        entry = registries.entity.async_get(ident) if registries.entity else None
        device_id = _attr(entry, "device_id")
        area_id = _attr(entry, "area_id")
    if kind == "device":
        device_id = ident
    if device_id:
        if kind != "device":
            chain.append(f"device:{device_id}")
        device = registries.device.async_get(device_id) if registries.device else None
        area_id = area_id or _attr(device, "area_id")
    if kind == "area":
        area_id = ident
    if area_id:
        if kind != "area":
            chain.append(f"area:{area_id}")
        area = registries.area.async_get_area(area_id) if registries.area else None
        floor_id = _attr(area, "floor_id")
    if floor_id and kind != "floor":
        chain.append(f"floor:{floor_id}")
    if scope != HOME_SCOPE:
        chain.append(HOME_SCOPE)
    return chain


async def read(
    book: HomeContextBook, registries: Callable[[], Any], msg: dict[str, Any]
) -> dict[str, Any]:
    """The index without a scope; else one document and its broader documents."""
    scope = msg.get("scope")
    if scope is None:
        return {"success": True, "scopes": await book.index()}
    result = await book.get(
        scope, msg.get("revision"), msg.get("include_history", False)
    )
    if result["success"] and msg.get("include_related", True):
        related = await book.existing(ancestors(registries(), scope))
        result["related"] = [
            _public(row["scope"], book.current(row["scope"]) or {}) for row in related
        ]
    return result


async def match(
    book: HomeContextBook, registries: Any, msg: dict[str, Any]
) -> dict[str, Any]:
    """The stored documents covering the given ids or their broader scopes.

    Without any id the 'home' document still covers the change.
    """
    candidates: list[str] = []
    for kind in ("entity", "device", "area", "floor"):
        for ident in msg.get(f"{kind}_ids") or []:
            scope = f"{kind}:{ident}"
            if SCOPE_PATTERN.match(scope):
                candidates.extend([scope, *ancestors(registries, scope)])
    return {
        "success": True,
        "scopes": await book.existing(candidates or [HOME_SCOPE]),
    }


def command_specs(
    vol: Any,
    resolve_registries: Callable[[Any], Any],
    store_factory: Callable[[Any], Any],
) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) rows websocket_api registers."""

    def book_for(hass: Any) -> HomeContextBook:
        return get_book(hass, lambda: store_factory(hass))

    async def get_prep(hass: Any, msg: dict[str, Any]) -> dict[str, Any]:
        return {
            "result": await read(book_for(hass), lambda: resolve_registries(hass), msg)
        }

    async def write_prep(hass: Any, msg: dict[str, Any]) -> dict[str, Any]:
        context = msg.get("ha_mcp_context")
        return {
            "result": await book_for(hass).write(
                msg["scope"],
                msg.get("content"),
                msg["expected_revision"],
                msg.get("note"),
                msg.get("author"),
                getattr(context, "user_id", None),
            )
        }

    async def match_prep(hass: Any, msg: dict[str, Any]) -> dict[str, Any]:
        return {"result": await match(book_for(hass), resolve_registries(hass), msg)}

    def passthrough(
        hass: Any, msg: dict[str, Any], *, result: dict[str, Any]
    ) -> dict[str, Any]:
        return result

    scope_schema = vol.All(str, vol.Match(SCOPE_PATTERN))
    ids = vol.All([str], vol.Length(max=MAX_MATCH_IDS))
    text = vol.Any(None, vol.All(str, vol.Length(max=MAX_NOTE_CHARS)))
    return [
        (
            {
                vol.Required("type"): WS_HOME_CONTEXT_GET,
                vol.Optional("scope"): scope_schema,
                vol.Optional("revision"): vol.All(int, vol.Range(min=1)),
                vol.Optional("include_history", default=False): bool,
                vol.Optional("include_related", default=True): bool,
            },
            passthrough,
            get_prep,
        ),
        (
            {
                vol.Required("type"): WS_HOME_CONTEXT_WRITE,
                vol.Required("scope"): scope_schema,
                vol.Required("content"): vol.Any(
                    None, vol.All(str, vol.Length(min=1, max=MAX_CONTENT_CHARS))
                ),
                vol.Required("expected_revision"): vol.All(int, vol.Range(min=0)),
                vol.Optional("note"): text,
                vol.Optional("author"): text,
            },
            passthrough,
            write_prep,
        ),
        (
            {
                vol.Required("type"): WS_HOME_CONTEXT_MATCH,
                vol.Optional("entity_ids"): ids,
                vol.Optional("device_ids"): ids,
                vol.Optional("area_ids"): ids,
                vol.Optional("floor_ids"): ids,
            },
            passthrough,
            match_prep,
        ),
    ]
