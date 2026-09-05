"""Rendering a blueprint plus its inputs into a standalone config.

``blueprint/substitute`` is the single core command behind two user-facing
shapes: ``ha_manage_blueprints(action="substitute")``, which returns the
rendered config and writes nothing, and ``ha_config_set_automation(
take_control_of_blueprint=True)``, which renders an existing blueprint
automation and saves the result over itself. Both go through here so the
frame and the failure classification stay identical.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple, NoReturn

from ..errors import ErrorCode, create_error_response
from .blueprint_write import error_text
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)


class TakenControl(NamedTuple):
    """A rendered standalone config, and what it was rendered from.

    ``config_hash`` is of the config the tool READ, not the one it is about to
    write: the write locks against it so an edit landing in between surfaces
    as a conflict rather than a lost update.
    """

    config: dict[str, Any]
    blueprint_path: str
    config_hash: str


def raise_substitute_failure(
    domain: str, path: str, response: dict[str, Any]
) -> NoReturn:
    """Map a failed ``blueprint/substitute`` onto a structured error.

    Core answers every substitute failure with the same generic
    ``unknown_error`` code, so the message text is the only discriminator:
    ``MissingInput`` and ``FailedToLoad`` carry their own prefixes.
    """
    message = error_text(response, "Failed to render the blueprint")
    lowered = message.lower()
    if "missing input" in lowered:
        code = ErrorCode.VALIDATION_FAILED
        suggestions = [
            (
                f'Read the required inputs: ha_manage_blueprints(action="get", '
                f'domain="{domain}", path="{path}")'
            ),
            "Pass every required input in the input dict",
        ]
    elif "failed to load" in lowered or "not found" in lowered:
        code = ErrorCode.RESOURCE_NOT_FOUND
        suggestions = [
            (
                f'Use ha_manage_blueprints(action="list", domain="{domain}") to see '
                "installed blueprints"
            ),
            "Check the path format (e.g., 'homeassistant/motion_light.yaml')",
        ]
    else:
        code = ErrorCode.SERVICE_CALL_FAILED
        suggestions = [
            "Check the Home Assistant logs for the underlying error",
            "Verify the input values match the blueprint's selectors",
        ]
    raise_tool_error(
        create_error_response(
            code,
            message,
            context={"domain": domain, "path": path},
            suggestions=suggestions,
        )
    )


async def substitute_blueprint(
    client: Any, domain: str, path: str, blueprint_input: dict[str, Any]
) -> dict[str, Any]:
    """Render ``path`` plus ``blueprint_input`` into a standalone config."""
    response = await client.send_websocket_message(
        {
            "type": "blueprint/substitute",
            "domain": domain,
            "path": path,
            "input": blueprint_input,
        }
    )
    if not response.get("success"):
        raise_substitute_failure(domain, path, response)

    result = response.get("result") or {}
    config = result.get("substituted_config")
    if config is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Home Assistant returned no substituted config for blueprint '{path}'.",
                context={"domain": domain, "path": path},
            )
        )
    if not isinstance(config, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Home Assistant returned a substituted config that is not a "
                f"mapping for blueprint '{path}'.",
                context={"domain": domain, "path": path},
            )
        )
    return config


def _blueprint_reference(config: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Return ``(path, input)`` when ``config`` is built on a blueprint.

    ``None`` for an already-standalone config, which is what distinguishes
    "nothing to take control of" from a rendering failure.
    """
    use_blueprint = config.get("use_blueprint")
    if not isinstance(use_blueprint, dict):
        return None
    path = use_blueprint.get("path")
    if not isinstance(path, str) or not path:
        return None
    blueprint_input = use_blueprint.get("input")
    return path, blueprint_input if isinstance(blueprint_input, dict) else {}


async def take_control_config(
    client: Any, domain: str, identifier: str, current_config: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Render a blueprint-backed config into the standalone config that replaces it.

    Returns ``(config, blueprint_path)`` -- the caller reports the path as what
    the config no longer references, so it is read here rather than by each
    tool re-deriving it.

    Mirrors the frontend's "Take control" (``ha-automation-editor``'s
    ``_takeControl``): the substituted config wins every key except ``id``,
    ``alias`` and ``description``, which carry over from the original so the
    entity keeps its identity and name. ``mode`` deliberately does NOT carry
    over -- it belongs to the blueprint's own rendered output.
    """
    # The two set tools name their target differently; a suggestion naming the
    # wrong one cannot be run as written, which is exactly when the caller
    # needs it.
    id_param = "identifier" if domain == "automation" else f"{domain}_id"
    reference = _blueprint_reference(current_config)
    if reference is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"'{identifier}' is not built from a blueprint, so there is "
                "nothing to take control of.",
                context={id_param: identifier, "domain": domain},
                suggestions=[
                    (
                        f"Inspect it with ha_config_get_{domain}"
                        f'({id_param}="{identifier}") -- a blueprint-backed '
                        "config has a 'use_blueprint' key"
                    ),
                    "Edit a standalone config directly with config or python_transform",
                ],
            )
        )
    path, blueprint_input = reference

    substituted = await substitute_blueprint(client, domain, path, blueprint_input)

    # The substituted config is authoritative for the logic; identity and the
    # human-facing labels come from the config being replaced.
    taken: dict[str, Any] = dict(substituted)
    taken.pop("use_blueprint", None)
    for key in ("id", "alias", "description"):
        value = current_config.get(key)
        if value is not None:
            taken[key] = value
    logger.debug("took control of %s %s from blueprint %s", domain, identifier, path)
    return taken, path


def validate_write_modes(
    domain: str,
    id_param: str,
    id_value: str | None,
    config: dict[str, Any] | None,
    python_transform: str | None,
    take_control_of_blueprint: bool,
) -> None:
    """Reject combinations of the three mutually exclusive write modes.

    Lives here because take control is what made the automation and script
    tools' validation identical: both gained the same third mode, with the
    same reason it cannot be paired with either of the other two. ``id_param``
    is the caller-facing name of the target ("identifier" for automations,
    "script_id" for scripts) so the error names the argument the caller
    actually passed.
    """
    if config is not None and python_transform is not None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Cannot use both config and python_transform simultaneously",
                suggestions=[
                    "Use only ONE of: config or python_transform",
                    "config: Full replacement",
                    (
                        "python_transform: Python-based edits (recommended for "
                        f"existing {domain}s)"
                    ),
                ],
                context={"action": "set", id_param: id_value},
            )
        )

    if take_control_of_blueprint and (
        config is not None or python_transform is not None
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"take_control_of_blueprint replaces the {domain} with its own "
                "rendered config, so it cannot be combined with config or "
                "python_transform.",
                suggestions=[
                    (
                        "Take control first, then edit the standalone config in "
                        "a second call"
                    ),
                    (
                        "Preview the rendering with ha_manage_blueprints"
                        f'(action="substitute", domain="{domain}") if you only '
                        "want to see it"
                    ),
                ],
                context={"action": "take_control", id_param: id_value},
            )
        )
