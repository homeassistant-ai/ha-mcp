"""The release notes ``ha_manage_updates(action="get")`` returns for one update."""

import logging
import re
from typing import Any

import httpx

from ..client.exceptions import HomeAssistantConnectionError
from .helpers import core_error_message

logger = logging.getLogger(__name__)

GITHUB_API_HEADERS = {
    "User-Agent": "HomeAssistant-MCP-Server",
    "Accept": "application/vnd.github+json",
}

# Core's codes for an entity without release notes and for an unknown entity.
_NO_NOTES_CODES = frozenset({"not_supported", "not_found"})
# Also Core's refusal for an unavailable entity or a failed notes read.
_ANSWERED_CODES = _NO_NOTES_CODES | {"home_assistant_error"}
_GITHUB_RELEASE = re.compile(
    r"https://github\.com/([^/]+)/([^/]+)/releases(?:/tag)?/([^/?#]+)"
)
_CHANGELOG_FILES = (
    "CHANGELOG.md",
    "RELEASES.md",
    "RELEASE_NOTES.md",
    "docs/releases/{tag}.md",
    "docs/CHANGELOG.md",
)
# Shorter than this, a changelog file is a placeholder rather than notes.
_MIN_CHANGELOG_LENGTH = 50


async def release_notes_fields(
    client: Any, entity_id: str, attributes: dict[str, Any], *, is_core: bool
) -> dict[str, Any]:
    """Core's release notes for the entity, else GitHub's, else a hint; Core's
    refusal (other than having no notes) as ``release_notes_error`` either way."""
    reply = await _core_release_notes(client, entity_id)
    if reply and reply.get("success") and reply.get("result"):
        return {"release_notes": reply["result"], "release_notes_source": "websocket"}

    fields: dict[str, Any] = {}
    if (
        reply
        and not reply.get("success")
        and reply.get("error_code") not in _NO_NOTES_CODES
    ):
        fields["release_notes_error"] = {
            "code": reply.get("error_code"),
            "message": core_error_message(reply.get("error", "")),
        }
    release_url = attributes.get("release_url")
    core_version = attributes.get("latest_version") if is_core else None
    found = await _github_release_notes(release_url, core_version)
    if found:
        fields["release_notes"], fields["release_notes_source"] = found
    else:
        fields["release_notes_hint"] = _hint(release_url)
    return fields


async def _core_release_notes(client: Any, entity_id: str) -> dict[str, Any] | None:
    try:
        reply: dict[str, Any] = await client.send_websocket_message(
            {"type": "update/release_notes", "entity_id": entity_id},
            expected_error_codes=_ANSWERED_CODES,
        )
    except HomeAssistantConnectionError as e:
        logger.debug(f"update/release_notes unanswered for {entity_id}: {e}")
        return None
    return reply


def _hint(release_url: str | None) -> str:
    hint = "Release notes could not be fetched automatically."
    if release_url:
        hint += f" View them at: {release_url}"
    return hint


async def _github_release_notes(
    release_url: str | None, core_version: str | None
) -> tuple[str, str] | None:
    """The GitHub release body or changelog file, with where it came from."""
    match = _GITHUB_RELEASE.match(release_url or "")
    if not match and not core_version:
        return None
    async with httpx.AsyncClient(
        timeout=15.0, headers={"User-Agent": GITHUB_API_HEADERS["User-Agent"]}
    ) as http:
        if match:
            owner, repo, tag = match.groups()
            body = await _release_body(http, owner, repo, tag)
            if body:
                return body, "github_api"
            changelog = await _changelog_file(http, owner, repo, tag)
            if changelog:
                return changelog, "github_raw"
        if core_version:
            body = await _release_body(http, "home-assistant", "core", core_version)
            if body:
                return body, "github_api"
    return None


async def _release_body(
    http: httpx.AsyncClient, owner: str, repo: str, tag: str
) -> str | None:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
    try:
        response = await http.get(url, headers=GITHUB_API_HEADERS)
        release = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as e:
        logger.debug(f"GitHub release {url} not read: {e}")
        return None
    if (
        response.status_code == 403
        and response.headers.get("X-RateLimit-Remaining") == "0"
    ):
        logger.warning(f"GitHub API rate limit exceeded for {url}")
    body = release.get("body") if isinstance(release, dict) else None
    return str(body) if body else None


async def _changelog_file(
    http: httpx.AsyncClient, owner: str, repo: str, tag: str
) -> str | None:
    for path in _CHANGELOG_FILES:
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{tag}/{path.format(tag=tag)}"
        try:
            response = await http.get(url)
        except httpx.HTTPError as e:
            logger.debug(f"Changelog {url} not read: {e}")
            continue
        if response.status_code == 200 and len(response.text) > _MIN_CHANGELOG_LENGTH:
            return response.text
    return None
