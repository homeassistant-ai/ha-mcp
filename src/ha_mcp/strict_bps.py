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
request, mirroring ``read_only_mode``: a settings-UI toggle applies live
when the UI shares the server process (standalone HTTP / embedded);
other modes pick the change up on restart.

The six write tools declare the ``BestPracticeKey`` parameter on their
signatures purely so FastMCP's pydantic validation accepts it and
schema-validating clients will send it; the tool bodies never read it —
:class:`StrictBpsMiddleware` consumes it here at call time.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any, NoReturn

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from pydantic import Field, ValidationError

from .errors import ErrorCode, create_error_response
from .tools.helpers import raise_tool_error
from .tools.util_helpers import _HA_BEST_PRACTICES_SKILL_NAME

logger = logging.getLogger(__name__)

# Degrade warnings fire once per process per branch (mirrors config.py's
# _BETA_GATE_LOGGED): strict_bps_effective() runs on every gated write and
# every best-practices skill read, so an unguarded warning would flood the
# logs for the whole life of a misconfigured install.
_DEGRADE_WARNED: set[str] = set()


def _warn_degraded_once(branch: str, message: str, *, exc_info: bool = False) -> None:
    if branch in _DEGRADE_WARNED:
        return
    _DEGRADE_WARNED.add(branch)
    logger.warning(message, exc_info=exc_info)


# Single source of truth for the acknowledgment key literal. It is published
# ONLY by ``strict_bps_ack_line`` (surfaced through ha_get_skill_guide Tier 3
# when strict mode is effective) and validated ONLY by the middleware — it must
# never appear in a block error, a tool docstring, or a skill_content embed.
STRICT_BPS_ACK_KEY = "bps-ack-1779"

# The write-tool parameter that carries the acknowledgment key. Declared on
# each of the six gated tools (adjacent to ``MandatoryBPS``) but read only here.
STRICT_BPS_KEY_PARAM = "BestPracticeKey"

# Shared annotation for that parameter — the single source of the schema
# description the six signatures publish. The parameter NAME must still be
# the literal ``BestPracticeKey`` in each ``def`` (identifiers can't be
# aliased); the wiring test pins name and schema against live registration.
BestPracticeKeyParam = Annotated[
    str | None,
    Field(
        default=None,
        description=(
            "Acknowledgment key published in the home-assistant-best-practices "
            "skill content; required when strict best-practices mode is enabled."
        ),
    ),
]

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
        _warn_degraded_once(
            "settings",
            "strict-BPS settings lookup failed; gate disabled",
            exc_info=True,
        )
        return False

    if not (settings.enable_mandatory_bps and settings.enable_strict_mandatory_bps):
        return False

    if get_skills_dir() is None:
        _warn_degraded_once(
            "skills-vendor",
            "strict-BPS gate disabled: skills-vendor submodule is missing, so "
            "the acknowledgment key is unobtainable — allowing gated writes "
            "through rather than locking them out. Run "
            "`git submodule update --init` on the server install.",
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

    The suggestions tell the model how to obtain the key but never contain
    the key itself. The second suggestion pre-arms the model for clients
    that validate tool arguments against a stale cached tool schema and
    reject the ``BestPracticeKey`` retry client-side (#1901) — that
    rejection never reaches the server, so this error is the only server
    surface that can carry the recovery path.
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
                f"{STRICT_BPS_KEY_PARAM} set.",
                f"If your client then rejects the retry with a schema-validation "
                f"error such as 'must NOT have additional properties', it is "
                f"validating against a stale cached tool schema from an older "
                f"server version that lacks {STRICT_BPS_KEY_PARAM}. Ask the user "
                f"to fully reload the client application (VS Code: run "
                f"'Developer: Reload Window' — restarting the MCP server or "
                f"resetting cached tools is not enough), then retry.",
            ],
            context={"tool_name": name, "strict_mandatory_bps": True},
        )
    )


class StrictBpsMiddleware(Middleware):
    """Block gated write tools that lack the acknowledgment key.

    Passthrough for every tool outside ``STRICT_BPS_GATED_TOOLS``. For the
    gated tools it always consumes (strips) the ``BestPracticeKey`` argument,
    and blocks the call when strict mode is effective and the supplied key
    was missing or wrong. Consults the live flags per call (via
    :func:`strict_bps_effective`), so a toggle applies live in
    standalone-HTTP/embedded mode like ``read_only_mode`` (other modes
    pick it up on restart).

    Proxied calls (``ha_call_write_tool`` etc.) re-enter the middleware
    chain with the REAL tool name after the proxy dispatches (see the
    proxy-handling comment in ReadOnlyMiddleware.on_call_tool and the
    re-dispatch in transforms/categorized_search.py), so no
    proxy-envelope unwrapping is needed here — the gated inner name is
    seen on the re-entry.
    """

    def __init__(
        self, *, list_tools: Callable[[], Awaitable[Sequence[Any]]] | None = None
    ) -> None:
        self._list_tools = list_tools
        self._registered_cache: set[str] | None = None

    async def _is_registered(self, name: str) -> bool:
        """True when ``name`` is in the live tool catalog.

        A gate-map tool may not actually be registered — ``ha_config_set_yaml``
        only registers when yaml editing is enabled. Gating an unregistered
        tool would misdirect the caller into fetching the key only to then
        learn the tool doesn't exist; passing through lets FastMCP's
        unknown-tool error surface first (#1820 review). The cache rebuilds
        on a miss so a late-registered tool still gates. Failure direction
        is the OPPOSITE of pass-through: with no injected catalog, an empty
        catalog, or a raising lookup, treat the name as registered and gate —
        wrongly gating a nonexistent tool costs one confusing error, while
        wrongly passing a registered tool through would bypass the gate.
        """
        if self._list_tools is None:
            return True
        if self._registered_cache is None or name not in self._registered_cache:
            try:
                tools = await self._list_tools()
            except Exception:
                logger.exception(
                    "strict-BPS: tool catalog lookup failed while checking "
                    "%s — gating conservatively",
                    name,
                )
                return True
            self._registered_cache = {t.name for t in tools}
        if not self._registered_cache:
            return True
        return name in self._registered_cache

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        name = context.message.name
        if name not in STRICT_BPS_GATED_TOOLS:
            return await call_next(context)

        # The gate is the ONLY reader of BestPracticeKey: strip it before
        # dispatch (whether or not strict mode is on) so the constant never
        # reaches the tool body, the policy middleware's approval args-hash
        # (where it would churn remembered approvals across strict toggles),
        # or downstream logging.
        args = context.message.arguments or {}
        supplied = args.get(STRICT_BPS_KEY_PARAM)
        if STRICT_BPS_KEY_PARAM in args:
            stripped = {k: v for k, v in args.items() if k != STRICT_BPS_KEY_PARAM}
            context = context.copy(
                message=context.message.model_copy(update={"arguments": stripped})
            )

        if (
            strict_bps_effective()
            and supplied != STRICT_BPS_ACK_KEY
            and await self._is_registered(name)
        ):
            logger.info("strict-BPS mode blocked keyless write to %s", name)
            _raise_bps_ack_required_error(name)

        return await call_next(context)
