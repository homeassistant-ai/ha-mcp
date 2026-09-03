"""Blueprint write paths for ``ha_manage_blueprints``: ``import`` and ``save``.

Both end in core's ``blueprint/save`` WebSocket command, but they fail for
different reasons and deserve different remedies — an import has already had
its content validated by ``blueprint/import``, so a save failure there is about
the destination, while a direct save is the first time core sees the caller's
YAML. Kept out of ``tools_blueprints`` so that module stays the tool surface
(dispatch, list/get/delete/substitute) and this one owns the write plumbing.
"""

from __future__ import annotations

from typing import Any, NoReturn

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error


def error_text(response: dict[str, Any], fallback: str) -> str:
    """Render Home Assistant's WebSocket error payload as a message string.

    ``send_websocket_message`` surfaces the error either as a plain string or
    as core's ``{"code": ..., "message": ...}`` envelope.
    """
    error = response.get("error", fallback)
    if isinstance(error, dict):
        return str(error.get("message", error) or fallback)
    return str(error or fallback)


def error_code(response: dict[str, Any]) -> str:
    """Home Assistant's WebSocket error ``code``, or ``""`` when it sent none.

    Only the ``{"code": ..., "message": ...}`` envelope carries one; a plain
    string error leaves the caller to match on the message text.
    """
    error = response.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or "")
    return ""


async def _save_blueprint(
    client: Any,
    domain: str,
    path: str,
    yaml_data: str,
    overwrite: bool,
    *,
    source_url: str | None,
) -> dict[str, Any]:
    """Send ``blueprint/save`` and return Home Assistant's raw response.

    ``source_url`` is sent only when given: core marks the key
    ``vol.Optional`` and stamps it into the saved blueprint's metadata, so
    forwarding ``None`` would attribute a hand-authored blueprint to a
    source it never came from. Failure mapping belongs to the caller — an
    import and a direct save fail for different reasons and offer different
    remedies.
    """
    save_message: dict[str, Any] = {
        "type": "blueprint/save",
        "domain": domain,
        "path": path,
        "yaml": yaml_data,
    }
    if source_url is not None:
        save_message["source_url"] = source_url
    # allow_override only exists on HA >= 2023.12 and the WS schema
    # rejects unknown keys - only send it when actually overwriting
    if overwrite:
        save_message["allow_override"] = True

    response: dict[str, Any] = await client.send_websocket_message(save_message)
    return response


# --- save ---------------------------------------------------------------------


async def write_blueprint(
    client: Any,
    domain: str,
    path: str,
    yaml_text: str,
    source_url: str | None,
    overwrite: bool,
) -> dict[str, Any]:
    """Write caller-supplied YAML to a blueprint path via blueprint/save."""
    response = await _save_blueprint(
        client, domain, path, yaml_text, overwrite, source_url=source_url
    )
    if not response.get("success"):
        _raise_save_failure(domain, path, response)

    overrides_existing = bool(
        (response.get("result") or {}).get("overrides_existing", False)
    )
    return {
        "success": True,
        "domain": domain,
        "path": path,
        "overrides_existing": overrides_existing,
        "message": (
            "Blueprint saved over the existing file. Automations and "
            "scripts using it were reloaded."
            if overrides_existing
            else "Blueprint saved."
        ),
    }


def _raise_save_failure(domain: str, path: str, response: dict[str, Any]) -> NoReturn:
    """Map a failed blueprint/save onto a structured error.

    Core answers with three codes: ``already_exists`` when the path is taken
    and ``allow_override`` was not sent, ``invalid_format`` when the YAML is
    not a valid blueprint for the domain, and ``unknown_error`` for a
    filesystem failure. The message text is matched as well, so a core that
    sends a bare string still classifies.
    """
    message = error_text(response, "Failed to save blueprint")
    code = error_code(response)
    lowered = message.lower()
    if code == "already_exists" or "already exists" in lowered:
        mapped = ErrorCode.RESOURCE_ALREADY_EXISTS
        suggestions = [
            f'Pass overwrite=True to replace it: ha_manage_blueprints(action="save", '
            f'domain="{domain}", path="{path}", yaml=..., overwrite=True)',
            "Or save under a different path to keep both",
        ]
    elif code == "invalid_format" or "invalid" in lowered:
        mapped = ErrorCode.VALIDATION_FAILED
        suggestions = [
            f"The YAML must be a complete {domain} blueprint with a "
            "'blueprint:' section declaring its domain and inputs",
            'Start from an installed one: ha_manage_blueprints(action="get", '
            f'domain="{domain}", path=...)',
        ]
    else:
        mapped = ErrorCode.SERVICE_CALL_FAILED
        suggestions = [
            "Check the Home Assistant logs for the underlying error",
            "Verify the blueprints directory is writable",
        ]
    raise_tool_error(
        create_error_response(
            mapped,
            message,
            context={"domain": domain, "path": path},
            suggestions=suggestions,
        )
    )


# --- import -------------------------------------------------------------------


async def import_blueprint(client: Any, url: str, overwrite: bool) -> dict[str, Any]:
    """Validate a blueprint URL through core, then persist it to disk."""
    if not url.startswith(("http://", "https://")):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Invalid URL format. URL must start with http:// or https://",
                context={"url": url},
            )
        )

    response = await client.send_websocket_message(
        {"type": "blueprint/import", "url": url}
    )
    if not response.get("success"):
        _raise_import_failure(url, response)

    result_data = response.get("result", {}) or {}
    suggested_filename = result_data.get("suggested_filename", "")
    raw_data = result_data.get("raw_data", "")
    blueprint_meta = result_data.get("blueprint", {}).get("metadata", {})
    domain = blueprint_meta.get("domain", "automation")

    if not suggested_filename or not raw_data:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Blueprint validated but no filename or YAML data was returned",
                context={"url": url},
                suggestions=[
                    "This may indicate an incompatible blueprint format",
                    "Try a different blueprint URL",
                ],
            )
        )

    # Ensure the path has a .yaml extension — HA's blueprint/import returns
    # suggested_filename without the extension (e.g. "user/blueprint_name")
    if not suggested_filename.endswith((".yaml", ".yml")):
        suggested_filename = suggested_filename + ".yaml"

    _assert_importable(url, result_data, suggested_filename, domain, overwrite)

    # Save the blueprint to disk (blueprint/import only validates)
    save_response = await _save_blueprint(
        client, domain, suggested_filename, raw_data, overwrite, source_url=url
    )
    if not save_response.get("success"):
        _raise_import_save_failure(url, suggested_filename, save_response)
    overrides_existing = (save_response.get("result") or {}).get(
        "overrides_existing", False
    )

    return {
        "success": True,
        "url": url,
        "imported_blueprint": {
            "path": suggested_filename,
            "domain": domain,
            "name": blueprint_meta.get("name"),
            "description": blueprint_meta.get("description"),
        },
        "overrides_existing": overrides_existing,
        "message": (
            "Blueprint re-imported successfully. Automations/scripts using it were reloaded."
            if overrides_existing
            else 'Blueprint imported successfully. Use ha_manage_blueprints(action="list") '
            "to see all installed blueprints."
        ),
    }


def _raise_import_failure(url: str, response: dict[str, Any]) -> NoReturn:
    """Raise SERVICE_CALL_FAILED for a rejected blueprint/import.

    The last two suggestions are the network-side remedies the old
    ``ha_import_blueprint`` carried on its exception handler: importing is
    the one action that makes Home Assistant reach the internet, so they
    belong here rather than on the consolidated tool's generic handler,
    where they would be offered for a failed list or delete too.
    """
    error_msg = error_text(response, "Failed to import blueprint")
    suggestions = [
        "Verify the URL is accessible",
        "Ensure the URL points to a valid blueprint YAML file",
        "Check if the blueprint format is compatible with your Home Assistant version",
        "Ensure Home Assistant has internet access",
        "Try importing from a different source (GitHub, Community, direct URL)",
    ]
    if "already exists" in error_msg.lower():
        suggestions.insert(
            0,
            'Blueprint already exists - use ha_manage_blueprints(action="list") '
            "to see installed blueprints",
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            error_msg,
            context={"url": url},
            suggestions=suggestions,
        )
    )


def _raise_import_save_failure(
    url: str, path: str, response: dict[str, Any]
) -> NoReturn:
    """Map a blueprint/save failure on the import path onto a structured error.

    Import validated the blueprint through core before writing it, so the
    remaining failure is about the destination, not the content.
    """
    save_error = error_text(response, "Failed to save blueprint")

    suggestions = [
        "The blueprint was validated but could not be saved to disk",
        'Use ha_manage_blueprints(action="list") to check if it already exists',
    ]

    # Reachable despite the early exists check: a race between
    # import and save, or an installed file that failed to load
    # (core reports exists=false for those)
    already_exists = "already exists" in save_error.lower()
    if already_exists:
        suggestions.insert(
            0,
            "A blueprint with this path already exists - pass overwrite=true to re-import it",
        )

    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_ALREADY_EXISTS
            if already_exists
            else ErrorCode.SERVICE_CALL_FAILED,
            save_error,
            context={"url": url, "path": path},
            suggestions=suggestions,
        )
    )


def _assert_importable(
    url: str,
    result_data: dict[str, Any],
    suggested_filename: str,
    domain: str,
    overwrite: bool,
) -> None:
    """Enforce the two gates blueprint/save does not re-run itself."""
    # blueprint/save does not re-run these checks (currently the
    # blueprint's min Home Assistant version) - without this gate an
    # unsupported blueprint saves cleanly and reports success
    validation_errors = result_data.get("validation_errors")
    if validation_errors:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                "Blueprint failed validation: "
                + "; ".join(str(e) for e in validation_errors),
                context={"url": url, "validation_errors": validation_errors},
                suggestions=[
                    "The blueprint is not compatible with this Home Assistant installation",
                    "Update Home Assistant to satisfy the blueprint's minimum version requirement",
                ],
            )
        )

    # blueprint/import reports whether the target path is already
    # installed - fail early with a re-import hint instead of letting
    # blueprint/save reject the write
    if result_data.get("exists") and not overwrite:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_ALREADY_EXISTS,
                f"Blueprint already exists at '{suggested_filename}'. "
                "Pass overwrite=true to re-import it.",
                context={
                    "url": url,
                    "path": suggested_filename,
                    "domain": domain,
                },
                suggestions=[
                    'Call ha_manage_blueprints(action="import", overwrite=True) to update '
                    "the installed blueprint",
                    'Use ha_manage_blueprints(action="get") to inspect the currently '
                    "installed version",
                ],
            )
        )
