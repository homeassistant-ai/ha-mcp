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

from typing import Any

from ..errors import ErrorCode, create_error_response
from ..hacs_auto_refresh import CANDIDATE_REPO_FULL_NAMES
from .hacs_backend import DUPLICATE_RECORD_SUGGESTIONS, StoreApi
from .helpers import raise_tool_error

# The stores use the GitHub repository id as the repository id.
OWN_REPOSITORY_IDS: dict[str, str] = {
    "homeassistant-ai/ha-mcp": "1056618941",
    "homeassistant-ai/ha-mcp-integration": "1289599380",
}


def _names_own_repository(repository_id: str) -> bool:
    wanted = repository_id.strip().lower()
    return wanted in {name.lower() for name in CANDIDATE_REPO_FULL_NAMES} or wanted in (
        OWN_REPOSITORY_IDS.values()
    )


def _matches(repo: dict[str, Any], repository_id: str) -> bool:
    wanted = repository_id.strip().lower()
    return wanted in ((repo.get("full_name") or "").lower(), str(repo.get("id")))


async def route_own_repository_download(
    ws_client: Any, api: StoreApi, repository_id: str
) -> tuple[str, str | None]:
    """Return the repository to download and a note when it was rerouted.

    Any repository that is not ha-mcp's own passes through untouched, with no
    store call. For ha-mcp's own, the installed one wins: a request for the
    other repository is rerouted to it, so the mirror keeps updating from the
    mirror and the main repository from the catalog. Two installed records
    are the deadlock the Marketplace cannot resolve, so that is refused with
    the way out.
    """
    if not _names_own_repository(repository_id):
        return repository_id, None

    response = await ws_client.send_command(api.command("list"))
    own = [
        dict(repo)
        for repo in response.get("result", [])
        if isinstance(repo, dict)
        and (repo.get("full_name") or "").lower()
        in {name.lower() for name in CANDIDATE_REPO_FULL_NAMES}
    ]
    installed = [repo for repo in own if repo.get("installed")]
    if len(installed) > 1:
        names = " and ".join(str(repo.get("full_name")) for repo in installed)
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{names} are both installed and both claim "
                "custom_components/ha_mcp_tools, so the store refuses to update "
                "either of them. One repository has to own the integration.",
                suggestions=DUPLICATE_RECORD_SUGGESTIONS,
                context={
                    "repository_id": repository_id,
                    "installed": [repo.get("full_name") for repo in installed],
                },
            )
        )
    if not installed or _matches(installed[0], repository_id):
        return repository_id, None
    owner = str(installed[0].get("full_name"))
    return owner, (
        f"{repository_id} was rerouted to {owner}, which already owns "
        "custom_components/ha_mcp_tools; the store allows one repository per "
        "integration."
    )
