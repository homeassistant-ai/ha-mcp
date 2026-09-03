"""
Blueprint management tools for Home Assistant.

This module provides a single tool for the blueprint lifecycle: listing,
reading, importing, saving, deleting, and rendering a standalone config from an
installed automation or script blueprint. The import and save write paths
live in ``blueprint_write``; the YAML-text tier ladder in ``blueprint_sources``.
"""

import logging
from typing import Annotated, Any, Literal, NamedTuple, NoReturn

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..client.rest_client import HomeAssistantConnectionError
from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .blueprint_sources import resolve_blueprint_source
from .blueprint_substitute import substitute_blueprint
from .blueprint_write import (
    error_text,
    import_blueprint,
    normalize_blueprint_path,
    write_blueprint,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .util_helpers import JSON_STRING_COERCION

logger = logging.getLogger(__name__)


def blueprint_snapshot_target(kw: dict[str, Any]) -> str:
    """The blueprint path a ``save`` or ``delete`` call will act on.

    ``blueprint/save`` appends ``.yaml`` to a path without it, so the pre-write
    snapshot of a ``save`` keys on that normalised path — the file the write
    replaces. ``blueprint/delete`` takes the store key verbatim and the tool
    refuses any path that is not one, so a delete keys on the argument as given.
    """
    path = kw.get("path") or ""
    if kw.get("action") == "save" and path:
        return normalize_blueprint_path(path)
    return path


class BlueprintConsumers(NamedTuple):
    """Who uses a blueprint, and whether Home Assistant actually said.

    ``answered`` false means the lookup could not be consulted, NOT that
    nothing uses the blueprint -- the whole point of returning it is that
    an empty ``entity_ids`` must never be reported as "nothing uses this".
    """

    entity_ids: list[str]
    answered: bool


_VALID_DOMAINS = ("automation", "script")

# The reference lookup is always secondary to the action the caller asked
# for -- it enriches a refused delete, or adds ``used_by`` to a get -- so
# inheriting send_command's 30s default would make either hang far longer
# than the operation itself. Same reasoning and same leash as
# ``smart_search/_graph.py``'s ``_GRAPH_TIMEOUT_S``. Popped by the client;
# never reaches Home Assistant.
_RELATED_TIMEOUT_S = 5.0


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
            The canonical ``{"success": True, "data": {...}}`` envelope
            carrying the blueprints list, count, and domain.
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
            "data": {
                "domain": domain,
                "count": len(blueprints),
                "blueprints": blueprints,
            },
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
    # blueprint's contents, so both capture first. The snapshot is keyed on the
    # path the write lands on: ``save`` follows core in appending ``.yaml``
    # (``blueprint_snapshot_target``), so an overwriting save of ``user/motion``
    # captures the ``user/motion.yaml`` it replaces instead of a path no tier
    # can read; ``delete`` takes the store key verbatim, as core does. The skip
    # test reads more than ``action``, because two shapes provably cannot
    # destroy anything: an unconfirmed delete changes nothing, and a ``save``
    # without ``overwrite`` either lands on a free path (nothing to snapshot)
    # or is refused by Home Assistant for already existing. Capturing for
    # either would still run an installed-file read (the component command, or
    # the File & YAML Tools service) first, and auto-backup is on by default,
    # so that would fire on every such call. The snapshot never re-fetches
    # ``source_url``: ``_fetch_blueprint`` passes ``source_url=None`` precisely
    # so a restore cannot write different YAML than the write destroyed.
    @with_auto_backup(
        domain_fn=lambda kw: f"blueprint_{kw.get('domain') or 'automation'}",
        id_fn=blueprint_snapshot_target,
        skip_fn=lambda kw: (
            kw.get("action") not in ("delete", "save")
            or (kw.get("action") == "delete" and not kw.get("confirm"))
            or (kw.get("action") == "save" and not kw.get("overwrite"))
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
                    "(action='get' / 'save' / 'delete' / 'substitute'). 'save' "
                    "appends '.yaml' when it is missing, as Home Assistant does."
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
            JSON_STRING_COERCION,
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

        ``get`` also reports ``used_by``: the automations or scripts built on
        the blueprint, which is the UI's "Show automations using this
        blueprint". Check it before deleting — Home Assistant refuses to delete a
        blueprint anything still uses, and it goes on counting a consumer that
        has since taken control of its own config until that consumer is
        removed.

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
        persist it. To convert an automation or script that ALREADY exists,
        prefer ``ha_config_set_automation`` / ``ha_config_set_script`` with
        ``take_control_of_blueprint=True``: it renders with that item's own
        current inputs and saves the result over itself in one call, where
        ``substitute`` would need those inputs restated and the config written
        back by hand. Taking control does NOT free the blueprint — Home
        Assistant goes on counting a converted automation or script as a user
        of it, so ``delete`` stays refused until the consumers are removed.

        EXAMPLES:
        - List: ha_manage_blueprints(action="list", domain="automation")
        - Get one (with its consumers in ``used_by``): ha_manage_blueprints(action="get", path="homeassistant/motion_light.yaml")
        - Import: ha_manage_blueprints(action="import", url="https://example.com/bp.yaml")
        - Duplicate: ha_manage_blueprints(action="save", path="user/my_copy.yaml", yaml=<text from get>)
        - Edit in place: ha_manage_blueprints(action="save", path="user/motion.yaml", yaml=<edited text>, overwrite=True)
        - Delete: ha_manage_blueprints(action="delete", path="user/motion.yaml", confirm=True)
        - Detach: ha_manage_blueprints(action="substitute", path="user/motion.yaml", input={"motion_sensor": "binary_sensor.hall"})
        - Convert an existing consumer to a standalone config: ha_config_set_automation(identifier="automation.hall", take_control_of_blueprint=True)

        RELATED TOOLS: ``ha_config_set_automation`` / ``ha_config_set_script``
        to build on a blueprint or persist a substituted config,
        ``ha_config_remove_automation`` / ``ha_config_remove_script`` to clear
        consumers blocking a delete, ``ha_search`` to find them, and
        ``ha_manage_backup(scope="edits")`` to restore a deleted blueprint.
        """
        try:
            # import takes its domain from the blueprint file, so a stray
            # value must not refuse an otherwise valid call.
            if action != "import":
                self._validate_domain(domain)

            if action == "list":
                return await self._list_blueprints(domain)
            if action == "get":
                return await self._get_blueprint(
                    domain, self._require(path, "path", action)
                )
            if action == "import":
                return await import_blueprint(
                    self._client, self._require(url, "url", action), overwrite
                )
            if action == "save":
                return await write_blueprint(
                    self._client,
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
                    error_text(response, "Failed to query blueprints"),
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
                    (
                        f'Use ha_manage_blueprints(action="list", domain="{domain}") '
                        "to see all available blueprints"
                    ),
                    "Check the path format (e.g., 'homeassistant/motion_light.yaml')",
                ],
            )
        )

    # --- Action handlers ---------------------------------------------------

    async def _list_blueprints(self, domain: str) -> dict[str, Any]:
        """List every installed blueprint in ``domain``."""
        return self._format_blueprint_list(await self._fetch_blueprints(domain), domain)

    async def _require_installed(self, domain: str, path: str) -> dict[str, Any]:
        """Return the store, having established that ``path`` is in it.

        ``get`` and ``delete`` both have to answer "no such blueprint" before
        doing anything else; the listing is returned for ``get``, which goes on
        to read the entry out of it.
        """
        blueprints_data = await self._fetch_blueprints(domain)
        if path not in blueprints_data:
            self._raise_not_found(domain, path, blueprints_data)
        return blueprints_data

    async def _get_blueprint(self, domain: str, path: str) -> dict[str, Any]:
        """Return one blueprint's metadata, inputs, parsed body, YAML and consumers.

        ``used_by`` answers the UI's "Show automations using this blueprint"
        without having to attempt a delete and read the refusal. It comes from
        the same ``search/related`` lookup that refusal uses, so the two cannot
        disagree; when Home Assistant does not answer it, the key is omitted
        and a warning says so rather than letting an absent key read as "no
        automation uses this".
        """
        blueprints_data = await self._require_installed(domain, path)

        blueprint_data = blueprints_data[path]
        data: dict[str, Any] = {
            "path": path,
            "domain": domain,
            "name": blueprint_data.get(
                "name", path.rsplit("/", maxsplit=1)[-1].replace(".yaml", "")
            ),
        }
        result: dict[str, Any] = {"success": True, "data": data}

        source_url: str | None = None
        if "metadata" in blueprint_data:
            meta = blueprint_data["metadata"]
            data["metadata"] = {
                "name": meta.get("name"),
                "description": meta.get("description"),
                "source_url": meta.get("source_url"),
                "author": meta.get("author"),
                "domain": meta.get("domain"),
                "homeassistant": meta.get("homeassistant"),
            }
            if "input" in meta:
                data["inputs"] = meta["input"]
            raw_source_url = meta.get("source_url")
            source_url = raw_source_url if isinstance(raw_source_url, str) else None

        # Core's blueprint/list returns metadata only (never a body or the file
        # text), so both come from the tier ladder in ``blueprint_sources``.
        # Merged additively; on an install where no tier can serve the file the
        # response stays metadata + inputs.
        await self._merge_blueprint_body(result, data, domain, path, source_url)

        consumers = await self._blueprint_consumers(domain, path)
        if consumers.answered:
            data["used_by"] = consumers.entity_ids
        else:
            result.setdefault("warnings", []).append(
                "Home Assistant did not answer the reference lookup, so the "
                f"{domain}s using this blueprint are unknown — 'used_by' is "
                "omitted rather than reported as empty."
            )
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
                        (
                            f'Re-run with confirm=True: ha_manage_blueprints(action="delete", '
                            f'domain="{domain}", path="{path}", confirm=True)'
                        ),
                        f'Inspect it first: ha_manage_blueprints(action="get", domain="{domain}", path="{path}")',
                    ],
                )
            )

        await self._require_installed(domain, path)

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
            "data": {"domain": domain, "path": path, "message": "Blueprint deleted."},
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
        message = error_text(response, "Failed to delete blueprint")
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

        consumers = await self._blueprint_consumers(domain, path)
        remove_tool = f"ha_config_remove_{domain}"
        set_tool = f"ha_config_set_{domain}"
        suggestions = [
            (
                f"Delete the {domain}s that use it with {remove_tool}, or re-point "
                f"them at another blueprint with {set_tool}"
            ),
            (
                f"Removing the {domain}s is what frees the blueprint. Taking "
                f"control of them ({set_tool}(take_control_of_blueprint=True)) "
                "converts them to standalone configs but does NOT release the "
                f"blueprint: Home Assistant keeps counting a converted {domain} "
                "as a user until it is removed"
            ),
            (
                f'Then retry: ha_manage_blueprints(action="delete", domain="{domain}", '
                f'path="{path}", confirm=True)'
            ),
        ]
        if consumers.answered:
            detail = (
                f"in use by {', '.join(consumers.entity_ids)}"
                if consumers.entity_ids
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
                context={
                    "domain": domain,
                    "path": path,
                    "in_use_by": consumers.entity_ids,
                },
                suggestions=suggestions,
            )
        )

    async def _blueprint_consumers(self, domain: str, path: str) -> BlueprintConsumers:
        """Ask HA's reference graph which entities use ``path``.

        Only the bucket named by the blueprint's own domain is read. Core's
        ``_async_search_automation_blueprint`` / ``_async_search_script_blueprint``
        fill just that one today, but the result is an open dict: were core to
        start returning related entities, devices or areas, unioning every
        bucket would list unrelated ids as blueprint consumers.

        ``answered`` is False when the ``search/related`` lookup itself could
        not be consulted, so the caller can say "unknown consumers" instead of
        "no consumers".
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
        except HomeAssistantConnectionError as exc:
            # The only class that escapes ``send_websocket_message``: it turns
            # every no-answer failure into this one and returns HA's own
            # rejections as a ``success: False`` envelope, handled below. A
            # wider catch here could only swallow a bug in this module and
            # report it as "consumers unknown".
            logger.debug("search/related failed for blueprint %s: %r", path, exc)
            return BlueprintConsumers([], False)
        if not isinstance(response, dict) or not response.get("success"):
            logger.debug("search/related rejected for blueprint %s: %r", path, response)
            return BlueprintConsumers([], False)
        result = response.get("result") or {}
        if not isinstance(result, dict):
            return BlueprintConsumers([], False)
        consumers = result.get(domain)
        if not isinstance(consumers, list):
            return BlueprintConsumers([], True)
        return BlueprintConsumers(
            sorted({item for item in consumers if isinstance(item, str)}), True
        )

    async def _substitute_blueprint(
        self, domain: str, path: str, blueprint_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Render a blueprint plus inputs into a standalone config.

        The same rendering ``ha_config_set_automation(
        take_control_of_blueprint=True)`` performs, minus the write: this
        returns the config for inspection and leaves the automation alone.
        """
        config = await substitute_blueprint(self._client, domain, path, blueprint_input)
        return {
            "success": True,
            "data": {"domain": domain, "path": path, "config": config},
        }

    async def _merge_blueprint_body(
        self,
        result: dict[str, Any],
        data: dict[str, Any],
        domain: str,
        path: str,
        source_url: str | None,
    ) -> None:
        """Merge the blueprint's body and raw YAML into the response.

        Adds ``config`` (the parsed body) to ``data`` whenever any tier produced
        one, and ``yaml`` + ``yaml_source`` whenever one produced the text —
        never a null key for a tier that found nothing. Warnings go on
        ``result`` instead: the style guide keeps ``warnings`` a top-level
        ``list[str]``, never nested in ``data``. A ``source_url`` answer earns
        its own: that text is a fresh download from the author, not the file on
        disk, so anything changed upstream since the import is in it. The
        component's own "present but could not read the body" warning is
        preserved.
        """
        found = await resolve_blueprint_source(
            self._client, domain, path, source_url=source_url
        )
        if found.config is not None:
            data["config"] = found.config
        if found.text is not None and found.source is not None:
            data["yaml"] = found.text
            data["yaml_source"] = found.source
        warnings = [w for w in (found.warning,) if w]
        if found.source == "source_url":
            warnings.append(
                f"The YAML was re-fetched from {source_url}, not read from the "
                "installed file — it includes any upstream change made since "
                "the blueprint was imported."
            )
        if warnings:
            result.setdefault("warnings", []).extend(warnings)


def register_blueprint_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant blueprint management tools."""
    register_tool_methods(mcp, BlueprintTools(client))
