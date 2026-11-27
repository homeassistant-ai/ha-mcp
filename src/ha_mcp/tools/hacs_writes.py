"""The write actions of ``ha_manage_hacs`` on Core's Marketplace."""

from functools import partial
from typing import Any

from ha_mcp._vendor.fastmcp import Context

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .coercion import parse_string_list_param
from .hacs_backend import (
    GITHUB_COMMAND_TIMEOUT,
    LIST,
    missing_category_warning,
    native_category,
    resolve_repository,
    store_command,
)
from .hacs_install_progress import install_progress
from .hacs_own_repository import (
    ownership_refusal_records,
    route_own_repository_download,
)
from .helpers import raise_tool_error, validate_identifier_not_empty

_RETRY_IS_SAFE = ["GitHub may be slow; this is safe to retry"]


def _outcome_unknown(what: str, repository_id: str) -> list[str]:
    """A timed-out install or uninstall still finishes in Core."""
    return [
        f"The {what} may still have completed in the Marketplace — read it back "
        f"with ha_get_hacs_info(action='info', repository_id='{repository_id}') "
        "before retrying"
    ]


def _require(value: str | None, name: str, action: str) -> str:
    return validate_identifier_not_empty(
        value,
        name,
        message=f"{name} is required for action='{action}'",
        suggestions=[
            "Find the repository with ha_get_hacs_info(action='search'); "
            "repository_id takes its numeric id or 'owner/repo'",
        ],
    )


async def _target(
    ws_client: Any, action: str, repository_id: str | None
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """The id Core takes, the listed row for a name, and the error context."""
    repository_id = _require(repository_id, "repository_id", action)
    context = {"action": action, "repository_id": repository_id}
    actual_id, row = await resolve_repository(ws_client, repository_id, context)
    return actual_id, row, context


# A read after a write Core already accepted that fails this way is a warning:
# the write stands.
_UNREAD = (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)


async def _held(
    ws_client: Any, actual_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    """The repository as the Marketplace holds it after a write (Core answers
    a version or beta change with no body), or None and a warning. Core may
    read it from GitHub first."""
    try:
        reply = await ws_client.send_command(
            "marketplace/repository/info",
            _wait_timeout=GITHUB_COMMAND_TIMEOUT,
            repository_id=actual_id,
        )
    except _UNREAD as err:
        return None, (
            f"The Marketplace accepted the change, but reading the repository "
            f"back failed ({err}); read it with ha_get_hacs_info(action='info', "
            f"repository_id='{actual_id}')."
        )
    return reply.get("result") or {}, None


def _done(
    action: str, actual_id: str, row: dict[str, Any] | None, message: str
) -> dict[str, Any]:
    full_name = row.get("full_name") if row else None
    return {
        "action": action,
        "repository_id": actual_id,
        "repository": full_name,
        "message": message.replace("{name}", full_name or f"repository {actual_id}"),
    }


async def download(
    ws_client: Any,
    repository_id: str | None,
    version: str | None,
    confirm_replace_built_in: bool | None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    repository_id = _require(repository_id, "repository_id", "download")
    repository_id, version, reroute_note = await route_own_repository_download(
        ws_client, repository_id, version
    )
    actual_id, row, context = await _target(ws_client, "download", repository_id)
    if reroute_note:
        context["routing_note"] = reroute_note
    fields: dict[str, Any] = {"repository": actual_id}
    if version:
        fields["version"] = version
    if confirm_replace_built_in:
        fields["confirm_replace_built_in"] = True
    full_name = row.get("full_name") if row else None
    async with install_progress(ws_client, ctx, actual_id, full_name):
        await store_command(
            ws_client,
            "marketplace/repository/install",
            {**context, "version": version},
            timeout=GITHUB_COMMAND_TIMEOUT,
            on_timeout=_outcome_unknown("install", actual_id),
            refusal_records=partial(ownership_refusal_records, ws_client),
            **fields,
        )
    return {
        **_done(
            "download",
            actual_id,
            row,
            "Installed {name}" + (f" version {version}" if version else ""),
        ),
        "version": version or "latest",
        "note": "Restart Home Assistant to load an integration. The Marketplace "
        "registers a dashboard card as a resource itself (storage mode); clear "
        "the browser cache to load it.",
        "warnings": [reroute_note] if reroute_note else [],
    }


async def remove(ws_client: Any, repository_id: str | None) -> dict[str, Any]:
    actual_id, row, context = await _target(ws_client, "remove", repository_id)
    await store_command(
        ws_client,
        "marketplace/repository/uninstall",
        context,
        timeout=GITHUB_COMMAND_TIMEOUT,
        on_timeout=_outcome_unknown("removal", actual_id),
        repository=actual_id,
    )
    return {
        **_done("remove", actual_id, row, "Uninstalled {name}"),
        "note": "Files are deleted, but an already-loaded integration module "
        "persists until the next Home Assistant restart.",
    }


async def update_information(
    ws_client: Any, repository_id: str | None
) -> dict[str, Any]:
    actual_id, row, context = await _target(
        ws_client, "update_information", repository_id
    )
    await store_command(
        ws_client,
        "marketplace/repository/refresh",
        context,
        timeout=GITHUB_COMMAND_TIMEOUT,
        on_timeout=_RETRY_IS_SAFE,
        repository=actual_id,
    )
    return {
        **_done(
            "update_information",
            actual_id,
            row,
            "Refreshed the repository information of {name}",
        ),
        "note": "The Marketplace re-read this repository's releases from GitHub; "
        "a newer release now shows there and, once installed, on its update entity.",
    }


async def set_version(
    ws_client: Any, repository_id: str | None, version: str | None
) -> dict[str, Any]:
    version = _require(version, "version", "set_version")
    actual_id, row, context = await _target(ws_client, "set_version", repository_id)
    await store_command(
        ws_client,
        "marketplace/repository/version",
        {**context, "version": version},
        timeout=GITHUB_COMMAND_TIMEOUT,
        on_timeout=_RETRY_IS_SAFE,
        repository=actual_id,
        version=version,
    )
    # Core selects the ref the Marketplace reads the repository at and installs
    # nothing; it answers a ref the repository does not have with success and
    # reads another, so the result is what it holds.
    held, unread = await _held(ws_client, actual_id)
    selected, ref = (held or {}).get("selected_tag"), (held or {}).get("ref")
    kept = ref == version or (
        selected is None and version == (held or {}).get("default_branch")
    )
    selection = (
        f"The Marketplace now reads {{name}} at {ref}."
        if held is not None
        else f"The Marketplace accepted {version} for {{name}}."
    )
    # A ref Core did not select is not one to send the caller to install.
    install_hint = (
        f"ha_manage_hacs(action='download', version='{version}') installs it, and "
        if held is None or kept
        else ""
    )
    message = (
        f"{selection} Nothing was installed: {install_hint}a download without "
        "version resets this selection."
    )
    warnings = [unread] if unread else []
    if held is not None and not kept:
        warnings.append(
            f"The Marketplace answered without selecting {version} (selected_tag "
            f"is {selected}, ref {ref}). Check the tag or branch name: "
            "ha_get_hacs_info(action='info', include_releases=True) lists the "
            "releases."
        )
    return {
        **_done("set_version", actual_id, row, message),
        "selected_tag": selected,
        "ref": ref,
        "warnings": warnings,
    }


async def set_beta(
    ws_client: Any, repository_id: str | None, show_beta: bool | None
) -> dict[str, Any]:
    if show_beta is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "show_beta is required for action='set_beta'",
                context={"action": "set_beta", "parameter": "show_beta"},
            )
        )
    actual_id, row, context = await _target(ws_client, "set_beta", repository_id)
    await store_command(
        ws_client,
        "marketplace/repository/beta",
        context,
        timeout=GITHUB_COMMAND_TIMEOUT,
        on_timeout=_RETRY_IS_SAFE,
        repository=actual_id,
        show_beta=show_beta,
    )
    held, unread = await _held(ws_client, actual_id)
    beta = bool(held.get("beta")) if held is not None else None
    if beta is None:
        message = "The Marketplace accepted the beta setting of {name}"
    else:
        message = (
            f"The Marketplace {'shows' if beta else 'hides'} beta versions of {{name}}"
        )
    return {
        **_done("set_beta", actual_id, row, message),
        "beta": beta,
        "warnings": [unread] if unread else [],
    }


async def ignore(ws_client: Any, repository_id: str | None) -> dict[str, Any]:
    actual_id, row, context = await _target(ws_client, "ignore", repository_id)
    await store_command(
        ws_client, "marketplace/repository/ignore", context, repository=actual_id
    )
    return _done("ignore", actual_id, row, "The Marketplace ignores {name}")


async def remove_repository(
    ws_client: Any, repository_id: str | None
) -> dict[str, Any]:
    actual_id, row, context = await _target(
        ws_client, "remove_repository", repository_id
    )
    await store_command(
        ws_client, "marketplace/repositories/remove", context, repository=actual_id
    )
    return _done(
        "remove_repository", actual_id, row, "Removed {name} from the Marketplace"
    )


async def clear_new(
    ws_client: Any,
    repository_id: str | None,
    categories: list[str] | str | None,
) -> dict[str, Any]:
    names = parse_string_list_param(categories, "categories", allow_csv=True) or None
    if (repository_id is None) == (names is None):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "action='clear_new' takes repository_id or categories, one of them.",
                context={"action": "clear_new"},
            )
        )
    if names is not None:
        context: dict[str, Any] = {"action": "clear_new", "categories": names}
        store = await store_command(ws_client, "marketplace/info", context) or {}
        warnings = [w for name in names if (w := missing_category_warning(name, store))]
        native = [native_category(name) for name in names]
        await store_command(
            ws_client,
            "marketplace/repositories/clear_new",
            context,
            categories=native,
        )
        cleared = [c for c in native if c in (store.get("categories") or [])]
        return {
            "action": "clear_new",
            "categories": native,
            "message": "Cleared the new flag of every repository in "
            + ", ".join(cleared)
            if cleared
            else "Cleared no new flag: the Marketplace has none of these categories",
            "warnings": warnings,
        }
    actual_id, row, context = await _target(ws_client, "clear_new", repository_id)
    await store_command(
        ws_client,
        "marketplace/repositories/clear_new",
        context,
        repository=actual_id,
    )
    return _done("clear_new", actual_id, row, "Cleared the new flag of {name}")


async def add_repository(
    ws_client: Any, repository: str | None, category: str | None
) -> dict[str, Any]:
    repository = _require(repository, "repository", "add_repository")
    context: dict[str, Any] = {"action": "add_repository", "repository": repository}
    if category is not None:
        category = native_category(_require(category, "category", "add_repository"))
    else:
        category = await _detected_category(ws_client, repository, context)
    await store_command(
        ws_client,
        "marketplace/repositories/add",
        {**context, "category": category},
        timeout=GITHUB_COMMAND_TIMEOUT,
        on_timeout=[
            "GitHub may be slow; retrying is safe, a second add answers that "
            "the repository is listed already"
        ],
        repository=repository,
        category=category,
    )
    found = "find its id with ha_get_hacs_info(action='search')"
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        reply = await ws_client.send_command(LIST, categories=[category])
    except _UNREAD as err:
        warnings.append(
            f"The Marketplace added {repository}, but reading its list back "
            f"failed ({err}); {found}."
        )
    else:
        rows = reply.get("result") or []
    # The repository may be given as a GitHub URL; its row is under owner/repo.
    given = repository.strip().lower().rstrip("/").removesuffix(".git")
    row = next(
        (
            r
            for r in rows
            if given == (name := str(r.get("full_name") or "").lower())
            or given.endswith(f"/{name}")
        ),
        None,
    )
    if row is None and not warnings:
        warnings.append(
            f"The Marketplace added {repository}, but lists it under another "
            f"name; {found}."
        )
    return {
        "action": "add_repository",
        "repository": repository,
        "category": category,
        "repository_id": str(row["id"]) if row else None,
        "message": f"Added {repository} to the Marketplace as {category}",
        "data": row,
        "warnings": warnings,
    }


async def _detected_category(
    ws_client: Any, repository: str, context: dict[str, Any]
) -> str:
    """The one category Core detects, as the panel's add dialog asks it."""
    result = await store_command(
        ws_client,
        "marketplace/repositories/detect",
        context,
        timeout=GITHUB_COMMAND_TIMEOUT,
        on_timeout=_RETRY_IS_SAFE,
        repository=repository,
    )
    detected = list((result or {}).get("categories") or [])
    if len(detected) == 1:
        return str(detected[0])
    found = (
        f"holds content of several categories ({', '.join(detected)})"
        if detected
        else "holds no content the Marketplace recognises in any category"
    )
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"{repository} {found}; pass category to choose.",
            suggestions=[
                "Ask the user which category to add it as, then retry with "
                "category set (search's store.categories lists them)",
            ],
            context={**context, "parameter": "category", "detected": detected},
        )
    )


async def acknowledge_critical(
    ws_client: Any, repository: str | None
) -> dict[str, Any]:
    repository = _require(repository, "repository", "acknowledge_critical")
    context = {"action": "acknowledge_critical", "repository": repository}
    critical = (
        await store_command(
            ws_client,
            "marketplace/critical/acknowledge",
            context,
            repository=repository,
        )
        or []
    )
    # Core answers its whole list whether or not a row has that exact name.
    if not any(
        row.get("repository") == repository and row.get("acknowledged")
        for row in critical
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"The Marketplace holds no critical removal named '{repository}'; "
                "nothing was acknowledged.",
                suggestions=[
                    "ha_get_hacs_info(action='critical') lists the critical "
                    "removals under the exact names this action takes",
                ],
                context=context,
            )
        )
    return {
        "action": "acknowledge_critical",
        "repository": repository,
        "message": f"Acknowledged the critical removal of {repository}",
        "critical": critical,
    }


async def accept_warning(ws_client: Any, confirm: bool | None) -> dict[str, Any]:
    if confirm is not True:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "action='accept_warning' needs confirm=True, given once the "
                "user has said they read the Marketplace's first-run warning "
                "and accept it.",
                suggestions=[
                    "Ask the user to read the warning in Settings > "
                    "Marketplace and say whether they accept it"
                ],
                context={"action": "accept_warning"},
            )
        )
    await store_command(
        ws_client, "marketplace/warning/accept", {"action": "accept_warning"}
    )
    return {
        "action": "accept_warning",
        "warning_accepted": True,
        "message": "Accepted the Marketplace's first-run warning for the user "
        "this server connects as; installs and updates now pass its gate.",
    }
