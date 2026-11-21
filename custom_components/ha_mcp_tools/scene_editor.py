"""The scene editor's ignored domains, for ``ha_mcp_tools/scene_ignored_domains``.

The scene editor leaves entities of these domains out of a picked device's
entities and out of its entity picker. The list exists only in the frontend
(``SCENE_IGNORED_DOMAINS`` in ``src/data/scene.ts``), so it is read from the
build Core serves: the webpack module that holds the scene config API's
delete call also holds this one string array. Every copy of that module in
the build must hold exactly one, and all copies the same; anything else
answers ``None`` rather than a guess. The installed frontend changes only
with a Core update, which restarts Home Assistant, so the read is cached.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .card_definitions import _MOD_RE, _balanced_end

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

WS_SCENE_IGNORED_DOMAINS = "ha_mcp_tools/scene_ignored_domains"
# Advertised by websocket_api's info; without it the server refuses capture_devices.
CAPABILITY = "scene_ignored_domains"
# scene.ts's deleteScene; the module that holds it is scene.ts.
_ANCHOR = 'callApi("DELETE",`config/scene/config/'
_STRING_ARRAY = re.compile(r'\[\s*"[^"\]]*"(?:\s*,\s*"[^"\]]*")*\s*\]')


def _module_arrays(text: str, at: int) -> list[str] | None:
    """The string-array literals of the module whose body holds ``text[at]``."""
    starts = list(_MOD_RE.finditer(text, 0, at))
    if not starts:
        return None
    start = starts[-1]
    end = _balanced_end(text, start.end() - 1)
    if end <= at:
        return None
    return _STRING_ARRAY.findall(text, start.start(), end)


def read_ignored_domains(build: Path) -> list[str] | None:
    """``SCENE_IGNORED_DOMAINS`` of the frontend build at ``build``."""
    found: set[str] = set()
    for path in build.glob("*.js"):
        text = path.read_text(encoding="utf-8")
        at = text.find(_ANCHOR)
        if at < 0:
            continue
        arrays = _module_arrays(text, at)
        if arrays is None or len(arrays) != 1:
            return None
        found.add(arrays[0])
    if len(found) != 1:
        return None
    domains: list[str] = json.loads(found.pop())
    return domains


@functools.cache
def ignored_domains() -> list[str] | None:
    """The installed frontend's list, or ``None`` when it was not found."""
    import hass_frontend

    domains = read_ignored_domains(hass_frontend.where() / "frontend_latest")
    if domains is None:
        _LOGGER.warning("The scene editor's ignored domains were not found")
    return domains


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""

    async def prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        return {"domains": await hass.async_add_executor_job(ignored_domains)}

    def do_scene_ignored_domains(
        hass: HomeAssistant, msg: dict[str, Any], *, domains: list[str] | None
    ) -> dict[str, Any]:
        return {"domains": domains}

    return [
        (
            {vol.Required("type"): WS_SCENE_IGNORED_DOMAINS},
            do_scene_ignored_domains,
            prep,
        )
    ]
