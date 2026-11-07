"""Keep ha-mcp's own integration on the one store repository that owns it.

Two repositories carry the ``ha_mcp_tools`` component: the main repository,
which the default catalog lists, and the legacy distribution mirror earlier
installs came from. Both extract to ``custom_components/ha_mcp_tools``. The
Marketplace (Home Assistant 2026.11+) refuses to install a repository whose
integration another installed repository owns. HACS never checked, so a
download of the second repository on Home Assistant 2026.10 or older left both
records claiming the directory, and after the 2026.11 upgrade the Marketplace
refuses to update either (#2993). The server's own install guidance names the
main repository, so a download of either repository goes to the one that is
installed.
"""

from __future__ import annotations

import re
from typing import Any

from ..client.rest_client import HomeAssistantCommandError, HomeAssistantCommandTimeout
from ..errors import ErrorCode, create_error_response
from ..hacs_auto_refresh import CANDIDATE_REPO_FULL_NAMES
from .hacs_backend import DUPLICATE_RECORD_SUGGESTIONS, StoreApi
from .helpers import raise_tool_error

MAIN_REPOSITORY = "homeassistant-ai/ha-mcp"
MIRROR_REPOSITORY = "homeassistant-ai/ha-mcp-integration"

# The two repositories tag releases alike, but not development builds:
# publish-dev.yml tags the main repository ``v<next>.dev<N>`` while
# sync-integration-mirror.yml tags the mirror ``v<next>-dev.<N>``.
_MAIN_DEV_REF = re.compile(r"^(v?\d+\.\d+\.\d+)\.dev(\d+)$")
_MIRROR_DEV_REF = re.compile(r"^(v?\d+\.\d+\.\d+)-dev\.(\d+)$")


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


async def route_own_repository_download(
    ws_client: Any, api: StoreApi, repository_id: str, version: str | None
) -> tuple[str, str | None, str | None]:
    """Return the repository and version to download, and a note when rerouted.

    A repository named by ``owner/repo`` that is not ha-mcp's own passes
    through with no store call; a numeric id is looked up in the store's list,
    since ids are the store's and not written down anywhere else. For ha-mcp's
    own, the installed one wins: a request for the other repository is
    rerouted to it, so the mirror keeps updating from the mirror and the main
    repository from the catalog, and a development ref is renamed the way the
    target tags it. Two installed records are the deadlock the Marketplace
    cannot resolve, so that is refused with the way out.
    """
    wanted = repository_id.strip().lower()
    named_own = wanted in {n.lower() for n in CANDIDATE_REPO_FULL_NAMES}
    if not named_own and "/" in wanted:
        return repository_id, version, None

    try:
        response = await ws_client.send_command(api.command("list"))
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout):
        # The download itself reports the store's trouble, with its own context
        return repository_id, version, None
    repos = [dict(r) for r in response.get("result", []) if isinstance(r, dict)]
    if not named_own:
        requested = next((r for r in repos if str(r.get("id")) == wanted), None)
        if requested is None or _own_name(requested) is None:
            return repository_id, version, None
        wanted = str(_own_name(requested))

    installed = [r for r in repos if _own_name(r) and r.get("installed")]
    if len(installed) > 1:
        names = " and ".join(str(r.get("full_name")) for r in installed)
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{names} are both installed and both claim "
                "custom_components/ha_mcp_tools, so the store refuses to update "
                "either of them. One repository has to own the integration.",
                suggestions=DUPLICATE_RECORD_SUGGESTIONS,
                context={
                    "repository_id": repository_id,
                    "installed": [r.get("full_name") for r in installed],
                },
            )
        )
    if not installed or _own_name(installed[0]) == wanted:
        return repository_id, version, None
    owner = str(installed[0].get("full_name"))
    target_version = translate_version(version, owner)
    note = (
        f"{repository_id} was rerouted to {owner}, which already owns "
        "custom_components/ha_mcp_tools; the store allows one repository per "
        "integration."
    )
    if target_version != version:
        note += f" The version was renamed from {version} to {target_version}."
    return owner, target_version, note
