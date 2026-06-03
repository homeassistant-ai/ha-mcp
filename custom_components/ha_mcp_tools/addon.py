"""Bootstrap the Home Assistant MCP Server add-on from the integration.

On Supervisor-based installs (Home Assistant OS / Supervised) this module adds
the ha-mcp add-on repository to the Supervisor store, installs the stable
"Home Assistant MCP Server" add-on, and starts it. A user can therefore install
everything from HACS without visiting the Add-on Store or pasting a repository
URL by hand.

It is intentionally inert on Container / Core installs (no Supervisor, no
add-ons); callers gate on ``homeassistant.helpers.hassio.is_hassio`` before
invoking it.

This flow is independent of the integration's privileged file/YAML services:
installing the add-on never enables those, and they remain gated by the ha-mcp
server's own beta feature flags.

The Home Assistant / aiohasupervisor imports are deferred into the async
function so the pure resolution helpers below stay importable (and unit
testable) without a Home Assistant environment.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Same repository users add manually today (the README "Add Repository" badge
# points here). Adding it surfaces every add-on in the repo — stable, dev, and
# the webhook proxy — exactly like the manual flow; we only ever install the
# stable one.
ADDON_REPOSITORY_URL = "https://github.com/homeassistant-ai/ha-mcp"

# The stable add-on's config slug is ``ha_mcp``. Supervisor namespaces
# third-party add-ons as ``<repo_hash>_<config_slug>``, so the installed slug is
# ``<hash>_ha_mcp``. The dev add-on (config slug ``ha_mcp_dev``) becomes
# ``<hash>_ha_mcp_dev``, which does NOT end with ``_ha_mcp`` — so the suffix
# test below selects the stable add-on only. We always pull stable.
STABLE_ADDON_SLUG_SUFFIX = "_ha_mcp"
ADDON_NAME = "Home Assistant MCP Server"

# aiohasupervisor's AddonState reports a running add-on as "started" (the
# enum has no "running" member: startup/started/stopped/unknown/error).
_RUNNING_STATES = ("started",)


class AddonBootstrapError(Exception):
    """Raised when the add-on could not be installed or started automatically."""


def _normalize_repo_url(url: str) -> str:
    """Normalize a repository URL for comparison (case, trailing slash, .git)."""
    normalized = url.strip().lower().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[: -len(".git")]
    return normalized


def _find_repository(repositories: list[Any], url: str) -> Any | None:
    """Return the store repository whose ``source`` matches ``url``, if present."""
    target = _normalize_repo_url(url)
    for repo in repositories:
        source = getattr(repo, "source", None)
        if isinstance(source, str) and _normalize_repo_url(source) == target:
            return repo
    return None


def _select_stable_addon(addons: list[Any], repo_slug: str | None) -> Any | None:
    """Pick the stable ha-mcp add-on from a list of store add-ons.

    When the repository slug is known, only an add-on that belongs to that
    repository (``repository == repo_slug``) and whose slug ends in ``_ha_mcp``
    (stable only — the dev add-on ends in ``_ha_mcp_dev``) is selected; a match
    in a different repository is never used, and None is returned if our
    repository has no matching stable add-on. Only when the repository slug is
    unknown does it fall back to the slug suffix across all add-ons.
    """
    if repo_slug is not None:
        for addon in addons:
            if getattr(addon, "repository", None) == repo_slug and getattr(
                addon, "slug", ""
            ).endswith(STABLE_ADDON_SLUG_SUFFIX):
                return addon
        return None
    for addon in addons:
        if getattr(addon, "slug", "").endswith(STABLE_ADDON_SLUG_SUFFIX):
            return addon
    return None


def _is_running(addon_info: Any) -> bool:
    """Return True if an installed add-on's info reports a running state."""
    state = getattr(addon_info, "state", None)
    state_value = getattr(state, "value", state)
    return state_value in _RUNNING_STATES


async def async_install_and_start_addon(hass: Any) -> None:
    """Add the repository (if needed), install the stable add-on, and start it.

    Idempotent: skips the repository-add when already present, skips install
    when already installed, and skips start when already running. Raises
    :class:`AddonBootstrapError` on any failure, or when the Supervisor client
    is unavailable.
    """
    # get_supervisor_client landed in HA 2024.x; guard for very old cores.
    try:
        from homeassistant.components.hassio import get_supervisor_client
        from homeassistant.helpers.hassio import is_hassio
    except ImportError as err:  # pragma: no cover
        raise AddonBootstrapError(
            "This Home Assistant version does not expose the Supervisor client "
            "needed to install the add-on automatically."
        ) from err

    # Defence in depth: the config flow only offers the add-on on Supervisor
    # installs, but never attempt the bootstrap without a Supervisor — Container
    # and Core installs have no add-on system at all.
    if not is_hassio(hass):
        raise AddonBootstrapError(
            "Home Assistant Supervisor is not available; the add-on can only be "
            "installed on Home Assistant OS or Supervised. Run the ha-mcp server "
            "via Docker or pip instead."
        )

    # aiohasupervisor ships with every Supervisor install; guarded for safety.
    try:
        from aiohasupervisor import SupervisorError
        from aiohasupervisor.models import StoreAddonInstall, StoreAddRepository
    except ImportError as err:  # pragma: no cover
        raise AddonBootstrapError(
            "The Supervisor client library is unavailable; install the "
            "Home Assistant MCP Server add-on manually from the Add-on Store."
        ) from err

    client = get_supervisor_client(hass)

    try:
        repositories = await client.store.repositories_list()
        repo = _find_repository(repositories, ADDON_REPOSITORY_URL)
        if repo is None:
            _LOGGER.info("Adding ha-mcp add-on repository to the Supervisor store")
            await client.store.add_repository(
                StoreAddRepository(repository=ADDON_REPOSITORY_URL)
            )
            # Refresh the store index so the newly added add-ons appear.
            await client.store.reload()
            repositories = await client.store.repositories_list()
            repo = _find_repository(repositories, ADDON_REPOSITORY_URL)

        addons = await client.store.addons_list()
        addon = _select_stable_addon(addons, getattr(repo, "slug", None))
        if addon is None:
            raise AddonBootstrapError(
                f"The '{ADDON_NAME}' add-on was not found in the store after "
                "adding the repository."
            )

        slug = addon.slug
        if not getattr(addon, "installed", False):
            _LOGGER.info("Installing the %s add-on (%s)", ADDON_NAME, slug)
            # Pass background=False explicitly (the library default also blocks)
            # so the blocking behaviour stays pinned if that default changes —
            # the subsequent start must not race an in-progress install.
            await client.store.install_addon(
                slug, options=StoreAddonInstall(background=False)
            )

        info = await client.addons.addon_info(slug)
        if not _is_running(info):
            _LOGGER.info("Starting the %s add-on (%s)", ADDON_NAME, slug)
            await client.addons.start_addon(slug)
    except AddonBootstrapError:
        raise
    except SupervisorError as err:
        raise AddonBootstrapError(
            f"Supervisor could not install or start the add-on: {err}"
        ) from err
    except Exception as err:
        # Orchestration boundary: surface any unexpected failure (a malformed
        # store entry, a non-SupervisorError client/transport error, etc.) as
        # AddonBootstrapError so the config flow's install_failed path runs and
        # still creates the integration entry, instead of crashing the flow.
        raise AddonBootstrapError(
            f"Unexpected error while installing the add-on: {err}"
        ) from err
