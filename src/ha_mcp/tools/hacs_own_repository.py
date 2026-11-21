"""Keep ha-mcp's own integration on the one store repository that owns it.

Two repositories carry the ``ha_mcp_tools`` component: the main repository,
which the default catalog lists, and the distribution mirror earlier installs
came from. Both extract to ``custom_components/ha_mcp_tools``, and the
Marketplace refuses to install a repository whose integration another
installed repository owns. Installs from before the Marketplace can hold both
records, and the Marketplace then refuses to update either (#2993). The
server's own install guidance names the main repository, so a download of
either repository goes to the one that is installed.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from ..hacs_auto_refresh import CANDIDATE_REPO_FULL_NAMES
from .hacs_backend import LIST, is_owned_directory_refusal
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)

MAIN_REPOSITORY = "homeassistant-ai/ha-mcp"
MIRROR_REPOSITORY = "homeassistant-ai/ha-mcp-integration"
# The Marketplace uses the GitHub repository id, fixed for a repository's lifetime.
OWN_REPOSITORY_IDS = {"1056618941": MAIN_REPOSITORY, "1289599380": MIRROR_REPOSITORY}

# The two repositories tag releases alike, but not development builds:
# publish-dev.yml tags the main repository ``v<next>.dev<N>`` while
# sync-integration-mirror.yml tags the mirror ``v<next>-dev.<N>``.
_MAIN_DEV_REF = re.compile(r"^(v?\d+\.\d+\.\d+)\.dev(\d+)$")
_MIRROR_DEV_REF = re.compile(r"^(v?\d+\.\d+\.\d+)-dev\.(\d+)$")
_RELEASE_REF = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
# From 9.0 both repositories tag a release with the server version; before
# that the mirror carried the component's own numbers (v2.2.1 for 8.6.0).
_SHARED_TAGS_SINCE = (9, 0, 0)


def _tagged_alike(version: str) -> bool:
    match = _RELEASE_REF.match(version)
    if match is None:
        return False
    return tuple(int(p) for p in match.groups()) >= _SHARED_TAGS_SINCE


def translate_version(version: str | None, target: str) -> str | None:
    """The same release named the way ``target`` tags it."""
    if not version:
        return version
    if target.lower() == MIRROR_REPOSITORY and (match := _MAIN_DEV_REF.match(version)):
        return f"{match[1]}-dev.{match[2]}"
    if target.lower() == MAIN_REPOSITORY and (match := _MIRROR_DEV_REF.match(version)):
        return f"{match[1]}.dev{match[2]}"
    return version


def _own_name(repo: dict[str, Any]) -> str | None:
    full_name = (repo.get("full_name") or "").lower()
    if full_name in {n.lower() for n in CANDIDATE_REPO_FULL_NAMES}:
        return full_name
    return None


async def installed_own_records(ws_client: Any) -> list[dict[str, Any]] | None:
    """The Marketplace's records for ha-mcp's own repositories that are
    installed, or None when it could not be asked."""
    try:
        response = await ws_client.send_command(LIST)
    except HomeAssistantCommandError, HomeAssistantCommandTimeout:
        logger.debug("Could not list the Marketplace's repositories", exc_info=True)
        return None
    repos = [dict(r) for r in response.get("result") or [] if isinstance(r, dict)]
    return [r for r in repos if _own_name(r) and r.get("installed")]


async def ownership_refusal_records(
    ws_client: Any, err: HomeAssistantCommandError
) -> list[str] | None:
    """For a Marketplace refusal, the installed own records' names, so the
    refusal can be explained; None for any other error, with no store call."""
    if not is_owned_directory_refusal(err):
        return None
    try:
        records = await installed_own_records(ws_client)
    except HomeAssistantConnectionError:
        # Core's refusal goes to the caller without the records, not this error.
        logger.debug("Lost the connection listing the records", exc_info=True)
        return None
    return [str(r.get("full_name")) for r in records or []]


async def route_own_repository_download(
    ws_client: Any, repository_id: str, version: str | None
) -> tuple[str, str | None, str | None]:
    """Return the repository and version to download, and a note when rerouted.

    A repository that is not ha-mcp's own, by name or by id, passes through
    with no store call, so a download stays one store command. For ha-mcp's
    own, the installed one wins: a request for the other repository is
    rerouted to it, so the mirror keeps updating from the mirror and the main
    repository from the catalog, and a development ref is renamed the way the
    target tags it. With two installed records the Marketplace refuses the
    download itself, and that refusal is explained where it is raised.
    """
    wanted = repository_id.strip().lower()
    wanted = OWN_REPOSITORY_IDS.get(wanted, wanted)
    if wanted not in {n.lower() for n in CANDIDATE_REPO_FULL_NAMES}:
        return repository_id, version, None

    installed = await installed_own_records(ws_client)
    # An unanswered list is the download's to report; two installed records
    # are the Marketplace's to refuse.
    if not installed or len(installed) > 1 or _own_name(installed[0]) == wanted:
        return repository_id, version, None
    owner = str(installed[0].get("full_name"))
    if version and not _tagged_alike(version):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{owner} owns custom_components/ha_mcp_tools, and releases "
                f"before {'.'.join(map(str, _SHARED_TAGS_SINCE))} are tagged "
                f"differently there, so {version} cannot be mapped to it.",
                suggestions=[
                    "Pick a version from the installed repository: "
                    'ha_get_hacs_info(action="info", '
                    f'repository_id="{owner}", include_releases=True), then '
                    f'ha_manage_hacs(action="download", repository_id="{owner}", '
                    'version="<one of its tags>").',
                    "Or omit version to install its latest release.",
                ],
                context={"repository_id": repository_id, "version": version},
            )
        )
    target_version = translate_version(version, owner)
    note = (
        f"{repository_id} was rerouted to {owner}, which already owns "
        "custom_components/ha_mcp_tools; HA-MCP stays on the one listing it "
        "was installed from."
    )
    if target_version != version:
        note += f" The version was renamed from {version} to {target_version}."
    return owner, target_version, note
