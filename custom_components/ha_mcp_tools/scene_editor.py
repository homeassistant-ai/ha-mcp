"""The scene editor's ignored domains, for ``ha_mcp_tools/scene_ignored_domains``.

The scene editor leaves entities of these domains out of a picked device's
entities and out of its entity picker. The list exists only in the frontend
(``SCENE_IGNORED_DOMAINS`` in ``src/data/scene.ts``), so it is read from the
build Core serves: the webpack module that holds the scene config API's
delete call holds exactly one string-array literal, this one. Every build
file that holds that call must hold exactly one in its module, and all the
same; anything else refuses the command rather than guess. The installed
frontend changes only with a Core update, which restarts Home Assistant, so
the read is cached.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError

from .card_definitions import _MOD_RE, _balanced_end

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

WS_SCENE_IGNORED_DOMAINS = "ha_mcp_tools/scene_ignored_domains"
# Advertised by websocket_api's info; without it, or when the command is
# refused or unanswered, the server refuses capture_devices.
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
    """``SCENE_IGNORED_DOMAINS`` of the frontend build at ``build``, or ``None``
    unless every build file that holds the anchor agrees on exactly one array."""
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

    build = hass_frontend.where() / "frontend_latest"
    domains = read_ignored_domains(build)
    if domains is None:
        _LOGGER.warning(
            "No single list of the scene editor's ignored domains in %s", build
        )
    return domains


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""

    async def prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        domains = await hass.async_add_executor_job(ignored_domains)
        if domains is None:
            # A refused read is a missing component to the server: an update
            # that reads the frontend's new layout is the fix.
            raise HomeAssistantError(
                "The installed frontend holds no single list of the scene "
                "editor's ignored domains"
            )
        return {"domains": domains}

    def do_scene_ignored_domains(
        hass: HomeAssistant, msg: dict[str, Any], *, domains: list[str]
    ) -> dict[str, Any]:
        return {"domains": domains}

    return [
        (
            {vol.Required("type"): WS_SCENE_IGNORED_DOMAINS},
            do_scene_ignored_domains,
            prep,
        )
    ]
