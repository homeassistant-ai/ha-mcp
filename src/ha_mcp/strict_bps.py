"""Strict mandatory best-practices gate (#1779).

Today the six write tools attach best-practice ``skill_content`` unless
the caller passes ``MandatoryBPS=false`` — purely advisory. This module
adds a HARD GATE modeled on the Hubitat MCP server's acknowledgment gate:
when strict mode is effective, those six write tools are BLOCKED unless
the call carries an acknowledgment key that is published ONLY inside the
best-practices skill content served by ``ha_get_skill_guide``. The block
error tells the model exactly how to obtain the key but never the key
itself, forcing it to actually fetch/read the best practices before
writing.

Strict mode is *effective* only when BOTH ``enable_mandatory_bps`` (the
parent, #1182) and ``enable_strict_mandatory_bps`` (the child, #1779) are
on — see :func:`strict_bps_effective`. Both flags are read live per
request so a settings-UI toggle takes effect without a restart, mirroring
``read_only_mode``.

The six write tools declare the ``BestPracticeKey`` parameter on their
signatures purely so FastMCP's pydantic validation accepts it and
schema-validating clients will send it; the tool bodies never read it —
:class:`StrictBpsMiddleware` consumes it here at call time.
"""

from __future__ import annotations

import logging
from typing import Any, NoReturn

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from pydantic import ValidationError

from .errors import ErrorCode, create_error_response
from .tools.helpers import raise_tool_error
from .tools.util_helpers import _HA_BEST_PRACTICES_SKILL_NAME

logger = logging.getLogger(__name__)

# Single source of truth for the acknowledgment key literal. It is published
# ONLY by ``strict_bps_ack_line`` (surfaced through ha_get_skill_guide Tier 3
# when strict mode is effective) and validated ONLY by the middleware — it must
# never appear in a block error, a tool docstring, or a skill_content embed.
STRICT_BPS_ACK_KEY = "bps-ack-1779"

# The write-tool parameter that carries the acknowledgment key. Declared on
# each of the six gated tools (adjacent to ``MandatoryBPS``) but read only here.
STRICT_BPS_KEY_PARAM = "BestPracticeKey"

# The six gated write tools mapped to the first canonical skill reference file
# the block error should direct the model to read. Each value is the FIRST
# entry of that module's canonical ``_*_SKILL_FILES`` constant (kept in sync by
# tests/src/unit/test_strict_bps.py):
#   ha_config_set_automation → _AUTOMATION_SKILL_FILES[0]
#   ha_config_set_script     → _SCRIPT_SKILL_FILES[0]
#   ha_config_set_scene      → _SCENE_SKILL_FILES[0]
#   ha_config_set_helper     → _HELPER_SKILL_FILES[0]
#   ha_config_set_dashboard  → _DASHBOARD_SKILL_FILES[0]
#   ha_config_set_yaml       → _YAML_SKILL_FILES[0]
STRICT_BPS_GATED_TOOLS: dict[str, str] = {
    "ha_config_set_automation": "references/automation-patterns.md",
    "ha_config_set_script": "references/automation-patterns.md",
    "ha_config_set_scene": "SKILL.md",
    "ha_config_set_helper": "references/helper-selection.md",
    "ha_config_set_dashboard": "references/dashboard-guide.md",
    "ha_config_set_yaml": "references/template-guidelines.md",
}


def strict_bps_effective() -> bool:
    """Return True only when strict best-practices mode is in force.

    Requires BOTH ``enable_mandatory_bps`` (parent, #1182) AND
    ``enable_strict_mandatory_bps`` (child, #1779) to be on — the child is
    inert without the parent (there is no config-level cascade for these
    non-beta flags, so the AND is enforced here at the single consumption
    site).

    Fail-open in two degraded situations, each with a single warning log:

    * A ``ValidationError`` from the settings load — a corrupt settings env
      must not brick every gated write. Mirrors the narrow degrade in
      ``build_skill_content`` (util_helpers.py).
    * ``get_skills_dir()`` returns None (skills-vendor submodule absent) —
      with the vendor missing the key is unobtainable, so the gate would
      otherwise lock out every gated write with no recovery path.
    """
    from .config import get_global_settings
    from .utils.skill_loader import get_skills_dir

    try:
        settings = get_global_settings()
    except ValidationError:
        logger.warning(
            "strict-BPS settings lookup failed; gate disabled", exc_info=True
        )
        return False

    if not (settings.enable_mandatory_bps and settings.enable_strict_mandatory_bps):
        return False

    if get_skills_dir() is None:
        logger.warning(
            "strict-BPS gate disabled: skills-vendor submodule is missing, so "
            "the acknowledgment key is unobtainable — allowing gated writes "
            "through rather than locking them out. Run "
            "`git submodule update --init` on the server install."
        )
        return False

    return True


def strict_bps_ack_line() -> str:
    """Return the single line that publishes the acknowledgment key.

    Prepended to the ha_get_skill_guide Tier-3 best-practices content when
    strict mode is effective (server.py). This is the ONLY place the key
    literal is emitted to a caller.
    """
    return (
        f"Acknowledgment key: {STRICT_BPS_ACK_KEY} — strict best-practices "
        "mode is ON; pass this exact value as the BestPracticeKey argument "
        "on gated write tools."
    )


def _raise_bps_ack_required_error(name: str) -> NoReturn:
    """Raise the structured block error for a gated write missing the key.

    The message and suggestion tell the model how to obtain the key but
    never contain the key itself.
    """
    reference_file = STRICT_BPS_GATED_TOOLS[name]
    message = (
        "Strict best-practices mode is enabled for this tool. Read the "
        "best-practices skill to obtain the required acknowledgment key, then "
        "pass it as the BestPracticeKey argument on this call. The key is "
        "published only in that skill's content."
    )
    raise_tool_error(
        create_error_response(
            ErrorCode.BPS_ACKNOWLEDGMENT_REQUIRED,
            message,
            suggestions=[
                f"Call ha_get_skill_guide(skill={_HA_BEST_PRACTICES_SKILL_NAME!r}, "
                f"file={reference_file!r}), read the content, then retry with "
                f"{STRICT_BPS_KEY_PARAM} set."
            ],
            context={"tool_name": name, "strict_mandatory_bps": True},
        )
    )


class StrictBpsMiddleware(Middleware):
    """Block gated write tools that lack the acknowledgment key.

    No-op passthrough unless strict mode is effective AND the called tool
    is one of ``STRICT_BPS_GATED_TOOLS``. Consults the live flags per call
    (via :func:`strict_bps_effective`), so toggling strict mode is
    restart-free like ``read_only_mode``.

    Proxied calls (``ha_call_write_tool`` etc.) re-enter the middleware
    chain with the REAL tool name after the proxy dispatches (see
    read_only.py:433-441 and transforms/categorized_search.py:451), so no
    proxy-envelope unwrapping is needed here — the gated inner name is
    seen on the re-entry.
    """

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        name = context.message.name
        if name not in STRICT_BPS_GATED_TOOLS or not strict_bps_effective():
            return await call_next(context)

        if (context.message.arguments or {}).get(STRICT_BPS_KEY_PARAM) != (
            STRICT_BPS_ACK_KEY
        ):
            logger.info("strict-BPS mode blocked keyless write to %s", name)
            _raise_bps_ack_required_error(name)

        return await call_next(context)
