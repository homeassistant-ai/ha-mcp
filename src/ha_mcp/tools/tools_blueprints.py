"""
Blueprint management tools for Home Assistant.

This module provides a single tool for the blueprint lifecycle: listing,
reading, importing, saving, deleting, and rendering a standalone config from an
installed automation or script blueprint.
"""

import logging
from typing import Annotated, Any, Literal, NoReturn

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .blueprint_sources import resolve_blueprint_source
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)

logger = logging.getLogger(__name__)

_VALID_DOMAINS = ("automation", "script")

# The reference lookup runs only to enrich an error that is already being
# raised, so inheriting send_command's 30s default would make a rejected
# delete hang far longer than the delete itself. Same reasoning and same
# leash as ``smart_search/_graph.py``'s ``_GRAPH_TIMEOUT_S``. Popped by the
# client; never reaches Home Assistant.
_RELATED_TIMEOUT_S = 5.0


def _error_text(response: dict[str, Any], fallback: str) -> str:
    """Render Home Assistant's WebSocket error payload as a message string.

    ``send_websocket_message`` surfaces the error either as a plain string or
    as core's ``{"code": ..., "message": ...}`` envelope.
    """
    error = response.get("error", fallback)
    if isinstance(error, dict):
        return str(error.get("message", error) or fallback)
    return str(error or fallback)


def _error_code(response: dict[str, Any]) -> str:
    """Home Assistant's WebSocket error ``code``, or ``""`` when it sent none.

    Only the ``{"code": ..., "message": ...}`` envelope carries one; a plain
    string error leaves the caller to match on the message text.
    """
    error = response.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or "")
    return ""


class BlueprintTools:
    """Blueprint management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _format_blueprint_list(
        blueprints_data: dict[str, Any], domain: str
    ) -> dict[str, Any]:
        """Format blueprint data into list response structure.

        Args:
            blueprints_data: Raw blueprint data from WebSocket API
            domain: Blueprint domain (automation or script)

        Returns:
            Formatted response with blueprints list, count, and domain
        """
        blueprints = []
        for bp_path, metadata in blueprints_data.items():
            blueprint_info = {
                "path": bp_path,
                "domain": domain,
                "name": metadata.get(
                    "name", bp_path.split("/")[-1].replace(".yaml", "")
                ),
            }

            # Add optional metadata if available
            if "metadata" in metadata:
                meta = metadata["metadata"]
                blueprint_info.update(
                    {
                        "description": meta.get("description"),
                        "source_url": meta.get("source_url"),
                        "author": meta.get("author"),
                    }
                )

            blueprints.append(blueprint_info)

        return {
            "success": True,
            "domain": domain,
            "count": len(blueprints),
            "blueprints": blueprints,
        }

    @tool(
        name="ha_manage_blueprints",
        tags={"Blueprints"},
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            # import fetches arbitrary URLs; list/get return externally
            # authored blueprint content from an otherwise local read.
            "openWorldHint": True,
            "title": "Manage Blueprints",
        },
    )
    # ``delete`` and ``save`` are the two actions that can destroy an installed
    # blueprint's contents, so both capture first; a ``save`` to a brand-new path
    # finds nothing to snapshot and proceeds. ``confirm`` is part of the skip
    # test too, not just ``action``: an unconfirmed delete changes nothing, and
    # capturing for it would run a component file read and — with no component —
    # an outbound re-fetch of the blueprint's third-party ``source_url`` before
    # the tool refuses. Auto-backup is on by default, so that would fire on
    # every unconfirmed call.
    @with_auto_backup(
        domain_fn=lambda kw: f"blueprint_{kw.get('domain') or 'automation'}",
        id_param="path",
        skip_fn=lambda kw: (
            kw.get("action") not in ("delete", "save")
            or (kw.get("action") == "delete" and not kw.get("confirm"))
        ),
    )
    @log_tool_usage
    async def ha_manage_blueprints(
        self,
        action: Annotated[
            Literal["list", "get", "import", "save", "delete", "substitute"],
            Field(
                description=(
                    "'list' installed blueprints, 'get' one blueprint's "
                    "metadata/inputs/YAML, 'import' one from a URL, 'save' YAML "
                    "text to a blueprint path, 'delete' an installed one, or "
                    "'substitute' to render a standalone config"
                )
            ),
        ],
        domain: Annotated[
            str,
            Field(
                description=(
                    "Blueprint domain: 'automation' or 'script'. Ignored by "
                    "action='import' — the blueprint file declares its own domain."
                ),
                default="automation",
            ),
        ] = "automation",
        path: Annotated[
            str | None,
            Field(
                description=(
                    "Installed blueprint path, e.g. 'homeassistant/motion_light.yaml' "
                    "(action='get' / 'save' / 'delete' / 'substitute')"
                ),
                default=None,
            ),
        ] = None,
        url: Annotated[
            str | None,
            Field(
                description=(
                    "URL to import from — GitHub, Home Assistant Community, or a "
                    "direct YAML link (action='import')"
                ),
                default=None,
            ),
        ] = None,
        yaml: Annotated[
            str | None,
            Field(
                description="Blueprint YAML text to write (action='save')",
                default=None,
            ),
        ] = None,
        source_url: Annotated[
            str | None,
            Field(
                description=(
                    "Origin URL to stamp into the saved blueprint's metadata "
                    "(action='save'); omit for a hand-authored blueprint"
                ),
                default=None,
            ),
        ] = None,
        overwrite: Annotated[
            bool,
            Field(
                description=(
                    "Write over an already-installed blueprint "
                    "(action='import' / 'save'). Home Assistant reloads every "
                    "automation/script using it."
                ),
                default=False,
            ),
        ] = False,
        input: Annotated[  # noqa: A002 — mirrors core's blueprint/substitute key
            dict[str, Any] | None,
            Field(
                description=(
                    "Blueprint input values keyed by input name "
                    "(action='substitute'); defaults to {}"
                ),
                default=None,
            ),
        ] = None,
        confirm: Annotated[
            bool,
            Field(
                description="Required confirmation for action='delete'",
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Manage Home Assistant blueprints — list, read, import, save, delete, or render a standalone config.

        One interface for the whole blueprint lifecycle in the ``automation``
        and ``script`` domains.

        DO NOT use this to create an automation or script FROM a blueprint —
        that is ``ha_config_set_automation`` / ``ha_config_set_script`` with a
        ``use_blueprint`` config.

        Use ``action="list"`` to discover installed blueprints, ``action="get"``
        for one blueprint's metadata, inputs and YAML, ``action="import"`` to
        install one from a URL, ``action="save"`` to write YAML text to a
        blueprint path, ``action="delete"`` to remove an installed one, and
        ``action="substitute"`` to render a blueprint plus inputs into a
        standalone config (the UI's "Take control"). To duplicate a blueprint,
        ``get`` it and ``save`` its ``yaml`` under a new ``path``; to edit one in
        place, ``get`` it, change the text, and ``save`` it back to the same
        ``path`` with ``overwrite=True``.

        CAVEATS: ``get`` returns the on-disk YAML only when something can read
        it — an in-process server, the ha_mcp_tools component, the File & YAML
        Tools entry, or the blueprint's ``source_url``; ``yaml_source`` names
        which one answered, and ``source_url`` text is a fresh download that can
        differ from the installed file. Core's blueprint API alone exposes
        metadata only, so a locally authored blueprint on a bare install has no
        readable text. ``save`` needs ``overwrite=True`` to replace an existing
        path and reloads every automation/script using it. ``delete`` requires
        ``confirm=True``, and Home Assistant refuses it while any automation or
        script still uses the blueprint — the error lists the consumers. Both
        writes are snapshotted first when a copy can be read, so
        ``ha_manage_backup(scope="edits")`` can restore the previous file.
        ``substitute`` only renders — it writes nothing, so pass the returned
        config to ``ha_config_set_automation`` / ``ha_config_set_script`` to
        persist it.

        EXAMPLES:
        - List: ha_manage_blueprints(action="list", domain="automation")
        - Get one: ha_manage_blueprints(action="get", path="homeassistant/motion_light.yaml")
        - Import: ha_manage_blueprints(action="import", url="https://example.com/bp.yaml")
        - Duplicate: ha_manage_blueprints(action="save", path="user/my_copy.yaml", yaml=<text from get>)
        - Edit in place: ha_manage_blueprints(action="save", path="user/motion.yaml", yaml=<edited text>, overwrite=True)
        - Delete: ha_manage_blueprints(action="delete", path="user/motion.yaml", confirm=True)
        - Detach: ha_manage_blueprints(action="substitute", path="user/motion.yaml", input={"motion_sensor": "binary_sensor.hall"})

        RELATED TOOLS: ``ha_config_set_automation`` / ``ha_config_set_script``
        to build on a blueprint or persist a substituted config,
        ``ha_config_remove_automation`` / ``ha_config_remove_script`` to clear
        consumers blocking a delete, ``ha_search`` to find them, and
        ``ha_manage_backup(scope="edits")`` to restore a deleted blueprint.
        """
        try:
            self._validate_domain(domain)

            if action == "list":
                return await self._list_blueprints(domain)
            if action == "get":
                return await self._get_blueprint(
                    domain, self._require(path, "path", action)
                )
            if action == "import":
                return await self._import_blueprint(
                    self._require(url, "url", action), overwrite
                )
            if action == "save":
                return await self._write_blueprint(
                    domain,
                    self._require(path, "path", action),
                    self._require(yaml, "yaml", action),
                    source_url,
                    overwrite,
                )
            if action == "delete":
                return await self._delete_blueprint(
                    domain, self._require(path, "path", action), confirm
                )
            return await self._substitute_blueprint(
                domain, self._require(path, "path", action), input or {}
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"action": action, "path": path, "domain": domain, "url": url},
                suggestions=[
                    'Use ha_manage_blueprints(action="list") to see available blueprints',
                    "Verify the blueprint path or import URL is correct",
                    "Check Home Assistant connection",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    # --- Shared validation / lookups --------------------------------------

    @staticmethod
    def _validate_domain(domain: str) -> None:
        """Reject a domain Home Assistant has no blueprint store for."""
        if domain not in _VALID_DOMAINS:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid domain '{domain}'. Must be one of: {', '.join(_VALID_DOMAINS)}",
                    context={"domain": domain, "valid_domains": list(_VALID_DOMAINS)},
                )
            )

    @staticmethod
    def _require(value: str | None, name: str, action: str) -> str:
        """Return a required parameter, or raise VALIDATION_MISSING_PARAMETER."""
        if value:
            return value
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                f"'{name}' is required for action='{action}'.",
                context={"action": action, "missing_parameter": name},
                suggestions=[
                    f"Pass {name} to ha_manage_blueprints(action='{action}', ...)",
                    'Use ha_manage_blueprints(action="list") to see installed blueprints',
                ],
            )
        )

    async def _fetch_blueprints(self, domain: str) -> dict[str, Any]:
        """Return the installed blueprint map for ``domain`` via blueprint/list."""
        response = await self._client.send_websocket_message(
            {"type": "blueprint/list", "domain": domain}
        )
        if not response.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    _error_text(response, "Failed to query blueprints"),
                    context={"domain": domain},
                )
            )
        return response.get("result", {}) or {}

    @staticmethod
    def _raise_not_found(
        domain: str, path: str, blueprints_data: dict[str, Any]
    ) -> NoReturn:
        """Raise RESOURCE_NOT_FOUND naming a sample of the installed paths."""
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Blueprint not found: {path}",
                context={
                    "path": path,
                    "domain": domain,
                    "available_blueprints": list(blueprints_data.keys())[:10],
                },
                suggestions=[
                    f'Use ha_manage_blueprints(action="list", domain="{domain}") '
                    "to see all available blueprints",
                    "Check the path format (e.g., 'homeassistant/motion_light.yaml')",
                ],
            )
        )

    # --- Action handlers ---------------------------------------------------

    async def _list_blueprints(self, domain: str) -> dict[str, Any]:
        """List every installed blueprint in ``domain``."""
        return self._format_blueprint_list(await self._fetch_blueprints(domain), domain)

    async def _get_blueprint(self, domain: str, path: str) -> dict[str, Any]:
        """Return one blueprint's metadata, inputs, parsed body and raw YAML."""
        blueprints_data = await self._fetch_blueprints(domain)
        if path not in blueprints_data:
            self._raise_not_found(domain, path, blueprints_data)

        blueprint_data = blueprints_data[path]
        result: dict[str, Any] = {
            "success": True,
            "path": path,
            "domain": domain,
            "name": blueprint_data.get(
                "name", path.rsplit("/", maxsplit=1)[-1].replace(".yaml", "")
            ),
        }

        source_url: str | None = None
        if "metadata" in blueprint_data:
            meta = blueprint_data["metadata"]
            result["metadata"] = {
                "name": meta.get("name"),
                "description": meta.get("description"),
                "source_url": meta.get("source_url"),
                "author": meta.get("author"),
                "domain": meta.get("domain"),
                "homeassistant": meta.get("homeassistant"),
            }
            if "input" in meta:
                result["inputs"] = meta["input"]
            raw_source_url = meta.get("source_url")
            source_url = raw_source_url if isinstance(raw_source_url, str) else None

        # Core's blueprint/list returns metadata only (never a body or the file
        # text), so both come from the tier ladder in ``blueprint_sources``.
        # Merged additively; on an install where no tier can serve the file the
        # response stays metadata + inputs.
        await self._merge_blueprint_body(result, domain, path, source_url)
        return result

    async def _delete_blueprint(
        self, domain: str, path: str, confirm: bool
    ) -> dict[str, Any]:
        """Delete an installed blueprint after confirmation and existence checks."""
        if not confirm:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Deletion not confirmed. Set confirm=True to delete blueprint '{path}'.",
                    context={"domain": domain, "path": path},
                    suggestions=[
                        f'Re-run with confirm=True: ha_manage_blueprints(action="delete", '
                        f'domain="{domain}", path="{path}", confirm=True)',
                        f'Inspect it first: ha_manage_blueprints(action="get", domain="{domain}", path="{path}")',
                    ],
                )
            )

        blueprints_data = await self._fetch_blueprints(domain)
        if path not in blueprints_data:
            self._raise_not_found(domain, path, blueprints_data)

        response = await self._client.send_websocket_message(
            {"type": "blueprint/delete", "domain": domain, "path": path}
        )
        if not response.get("success"):
            await self._raise_delete_failure(domain, path, response)

        # State verification: core answers blueprint/delete with an empty
        # result, so re-read the store rather than trusting the ack.
        if path in await self._fetch_blueprints(domain):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Home Assistant reported the delete succeeded, but blueprint "
                    f"'{path}' is still installed.",
                    context={"domain": domain, "path": path},
                    suggestions=[
                        "Check the Home Assistant logs for a filesystem error",
                        "Verify the blueprints directory is writable",
                    ],
                )
            )

        return {
            "success": True,
            "domain": domain,
            "path": path,
            "message": "Blueprint deleted.",
        }

    async def _raise_delete_failure(
        self, domain: str, path: str, response: dict[str, Any]
    ) -> NoReturn:
        """Map a failed blueprint/delete onto a structured error.

        Core raises ``BlueprintInUse`` ("Blueprint in use") while any
        automation/script still references the blueprint. That is a lock, not
        a service fault, so it becomes ``RESOURCE_LOCKED`` carrying the
        consumers the caller has to clear first.
        """
        message = _error_text(response, "Failed to delete blueprint")
        if "blueprint in use" not in message.lower():
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    message,
                    context={"domain": domain, "path": path},
                    suggestions=[
                        "Check the Home Assistant logs for the underlying error",
                        f'Verify the blueprint exists: ha_manage_blueprints(action="list", domain="{domain}")',
                    ],
                )
            )

        in_use_by, resolved = await self._blueprint_consumers(domain, path)
        remove_tool = f"ha_config_remove_{domain}"
        set_tool = f"ha_config_set_{domain}"
        suggestions = [
            f"Delete the {domain}s that use it with {remove_tool}, or re-point "
            f"them at another blueprint with {set_tool}",
            f'Or detach them: ha_manage_blueprints(action="substitute", domain="{domain}", '
            f'path="{path}", input=...) and write the rendered config back with {set_tool}',
            f'Then retry: ha_manage_blueprints(action="delete", domain="{domain}", '
            f'path="{path}", confirm=True)',
        ]
        if resolved:
            detail = (
                f"in use by {', '.join(in_use_by)}"
                if in_use_by
                else "reported as in use by Home Assistant"
            )
        else:
            detail = "reported as in use by Home Assistant"
            suggestions.insert(
                0,
                f"Home Assistant did not answer the reference lookup — use "
                f'ha_search(query="{path}") to find the {domain}s using it',
            )
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_LOCKED,
                f"Blueprint '{path}' cannot be deleted: {detail}.",
                context={"domain": domain, "path": path, "in_use_by": in_use_by},
                suggestions=suggestions,
            )
        )

    async def _blueprint_consumers(
        self, domain: str, path: str
    ) -> tuple[list[str], bool]:
        """Ask HA's reference graph which entities use ``path``.

        Only the bucket named by the blueprint's own domain is read. Core's
        ``_async_search_automation_blueprint`` / ``_async_search_script_blueprint``
        fill just that one today, but the result is an open dict: were core to
        start returning related entities, devices or areas, unioning every
        bucket would list unrelated ids as blueprint consumers.

        Returns ``(entity_ids, resolved)``. ``resolved`` is False when the
        ``search/related`` lookup itself could not be consulted, so the caller
        can say "unknown consumers" instead of "no consumers".
        """
        try:
            response = await self._client.send_websocket_message(
                {
                    "type": "search/related",
                    "item_type": f"{domain}_blueprint",
                    "item_id": path,
                    "_wait_timeout": _RELATED_TIMEOUT_S,
                }
            )
        except Exception as exc:
            logger.debug("search/related failed for blueprint %s: %r", path, exc)
            return [], False
        if not isinstance(response, dict) or not response.get("success"):
            logger.debug("search/related rejected for blueprint %s: %r", path, response)
            return [], False
        result = response.get("result") or {}
        if not isinstance(result, dict):
            return [], False
        consumers = result.get(domain)
        if not isinstance(consumers, list):
            return [], True
        return sorted({item for item in consumers if isinstance(item, str)}), True

    async def _substitute_blueprint(
        self, domain: str, path: str, blueprint_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Render a blueprint plus inputs into a standalone config."""
        response = await self._client.send_websocket_message(
            {
                "type": "blueprint/substitute",
                "domain": domain,
                "path": path,
                "input": blueprint_input,
            }
        )
        if not response.get("success"):
            self._raise_substitute_failure(domain, path, response)

        result = response.get("result") or {}
        config = result.get("substituted_config")
        if config is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Home Assistant returned no substituted config for blueprint "
                    f"'{path}'.",
                    context={"domain": domain, "path": path},
                )
            )
        return {
            "success": True,
            "domain": domain,
            "path": path,
            "config": config,
        }

    @staticmethod
    def _raise_substitute_failure(
        domain: str, path: str, response: dict[str, Any]
    ) -> NoReturn:
        """Map a failed blueprint/substitute onto a structured error.

        Core answers every substitute failure with the same generic
        ``unknown_error`` code, so the message text is the only discriminator:
        ``MissingInput`` and ``FailedToLoad`` carry their own prefixes.
        """
        message = _error_text(response, "Failed to render the blueprint")
        lowered = message.lower()
        if "missing input" in lowered:
            code = ErrorCode.VALIDATION_FAILED
            suggestions = [
                f'Read the required inputs: ha_manage_blueprints(action="get", '
                f'domain="{domain}", path="{path}")',
                "Pass every required input in the input dict",
            ]
        elif "failed to load" in lowered or "not found" in lowered:
            code = ErrorCode.RESOURCE_NOT_FOUND
            suggestions = [
                f'Use ha_manage_blueprints(action="list", domain="{domain}") to see '
                "installed blueprints",
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

    async def _merge_blueprint_body(
        self,
        result: dict[str, Any],
        domain: str,
        path: str,
        source_url: str | None,
    ) -> None:
        """Merge the blueprint's body and raw YAML into ``result``.

        Adds ``config`` (the parsed body) whenever any tier produced one, and
        ``yaml`` + ``yaml_source`` whenever one produced the text — never a null
        key for a tier that found nothing. A ``source_url`` answer earns its own
        warning: that text is a fresh download from the author, not the file on
        disk, so anything changed upstream since the import is in it. The
        component's own "present but could not read the body" warning is
        preserved.
        """
        found = await resolve_blueprint_source(
            self._client, domain, path, source_url=source_url
        )
        if found.config is not None:
            result["config"] = found.config
        if found.text is not None and found.source is not None:
            result["yaml"] = found.text
            result["yaml_source"] = found.source
        warnings = [w for w in (found.warning,) if w]
        if found.source == "source_url":
            warnings.append(
                f"The YAML was re-fetched from {source_url}, not read from the "
                "installed file — it includes any upstream change made since "
                "the blueprint was imported."
            )
        if warnings:
            result.setdefault("warnings", []).extend(warnings)

    async def _write_blueprint(
        self,
        domain: str,
        path: str,
        yaml_text: str,
        source_url: str | None,
        overwrite: bool,
    ) -> dict[str, Any]:
        """Write caller-supplied YAML to a blueprint path via blueprint/save."""
        response = await self._save_blueprint(
            domain, path, yaml_text, overwrite, source_url=source_url
        )
        if not response.get("success"):
            self._raise_save_failure(domain, path, response)

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

    @staticmethod
    def _raise_save_failure(
        domain: str, path: str, response: dict[str, Any]
    ) -> NoReturn:
        """Map a failed blueprint/save onto a structured error.

        Core answers with three codes: ``already_exists`` when the path is taken
        and ``allow_override`` was not sent, ``invalid_format`` when the YAML is
        not a valid blueprint for the domain, and ``unknown_error`` for a
        filesystem failure. The message text is matched as well, so a core that
        sends a bare string still classifies.
        """
        message = _error_text(response, "Failed to save blueprint")
        code = _error_code(response)
        lowered = message.lower()
        if code == "already_exists" or "already exists" in lowered:
            error_code = ErrorCode.RESOURCE_ALREADY_EXISTS
            suggestions = [
                f'Pass overwrite=True to replace it: ha_manage_blueprints(action="save", '
                f'domain="{domain}", path="{path}", yaml=..., overwrite=True)',
                "Or save under a different path to keep both",
            ]
        elif code == "invalid_format" or "invalid" in lowered:
            error_code = ErrorCode.VALIDATION_FAILED
            suggestions = [
                f"The YAML must be a complete {domain} blueprint with a "
                "'blueprint:' section declaring its domain and inputs",
                'Start from an installed one: ha_manage_blueprints(action="get", '
                f'domain="{domain}", path=...)',
            ]
        else:
            error_code = ErrorCode.SERVICE_CALL_FAILED
            suggestions = [
                "Check the Home Assistant logs for the underlying error",
                "Verify the blueprints directory is writable",
            ]
        raise_tool_error(
            create_error_response(
                error_code,
                message,
                context={"domain": domain, "path": path},
                suggestions=suggestions,
            )
        )

    async def _save_blueprint(
        self,
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

        response: dict[str, Any] = await self._client.send_websocket_message(
            save_message
        )
        return response

    @staticmethod
    def _raise_import_save_failure(
        url: str, path: str, response: dict[str, Any]
    ) -> NoReturn:
        """Map a blueprint/save failure on the import path onto a structured error.

        Import validated the blueprint through core before writing it, so the
        remaining failure is about the destination, not the content.
        """
        save_error = _error_text(response, "Failed to save blueprint")

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

    async def _import_blueprint(self, url: str, overwrite: bool) -> dict[str, Any]:
        """Validate a blueprint URL through core, then persist it to disk."""
        if not url.startswith(("http://", "https://")):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Invalid URL format. URL must start with http:// or https://",
                    context={"url": url},
                )
            )

        response = await self._client.send_websocket_message(
            {"type": "blueprint/import", "url": url}
        )
        if not response.get("success"):
            self._raise_import_failure(url, response)

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

        self._assert_importable(url, result_data, suggested_filename, domain, overwrite)

        # Save the blueprint to disk (blueprint/import only validates)
        save_response = await self._save_blueprint(
            domain, suggested_filename, raw_data, overwrite, source_url=url
        )
        if not save_response.get("success"):
            self._raise_import_save_failure(url, suggested_filename, save_response)
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

    @staticmethod
    def _raise_import_failure(url: str, response: dict[str, Any]) -> NoReturn:
        """Raise SERVICE_CALL_FAILED for a rejected blueprint/import.

        The last two suggestions are the network-side remedies the old
        ``ha_import_blueprint`` carried on its exception handler: importing is
        the one action that makes Home Assistant reach the internet, so they
        belong here rather than on the consolidated tool's generic handler,
        where they would be offered for a failed list or delete too.
        """
        error_msg = _error_text(response, "Failed to import blueprint")
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

    @staticmethod
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


def register_blueprint_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant blueprint management tools."""
    register_tool_methods(mcp, BlueprintTools(client))
