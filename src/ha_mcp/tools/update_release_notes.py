"""The release notes ``ha_manage_updates(action="get")`` and the Marketplace's
repository info return for one update."""

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


def _rate_limited(response: httpx.Response) -> bool:
    return bool(response.headers.get("X-RateLimit-Remaining") == "0")


def http_problem(what: str, response: httpx.Response) -> str:
    problem = f"{what} answered HTTP {response.status_code}"
    if _rate_limited(response):
        problem += " (GitHub's API rate limit is used up)"
    return problem


def entity_release_notes(result: Any) -> dict[str, Any]:
    """``update/release_notes``'s answer as ``release_notes`` plus its other
    non-empty fields under a ``release_notes_`` prefix. Core 2026.12+ answers an
    object (translated ``messages`` beside the notes), earlier Core the notes alone."""
    if not isinstance(result, dict):
        return {"release_notes": result}
    return {
        "release_notes": result.get("release_notes"),
        **{
            f"release_notes_{key}": value
            for key, value in result.items()
            if key != "release_notes" and value not in (None, [], {}, "")
        },
    }


async def release_notes_fields(
    client: Any,
    entity_id: str,
    attributes: dict[str, Any],
    *,
    is_core: bool,
    language: str | None = None,
) -> dict[str, Any]:
    """Core's release notes for the entity, else GitHub's, else a hint, with
    the other fields of Core's answer (``release_notes_messages``) either way;
    Core's refusal (other than having no notes) as ``release_notes_error``.
    ``warnings`` says when this Core takes no ``language``."""
    reply, warnings = await _core_release_notes(client, entity_id, language)
    fields: dict[str, Any] = {"warnings": warnings} if warnings else {}
    if reply and reply.get("success"):
        fields.update(entity_release_notes(reply.get("result")))
        notes = fields.pop("release_notes")
        if notes:
            return {
                **fields,
                "release_notes": notes,
                "release_notes_source": "websocket",
            }

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
    problems: list[str] = []
    found = await _github_release_notes(release_url, core_version, problems)
    if found:
        fields["release_notes"], fields["release_notes_source"] = found
    else:
        fields["release_notes_hint"] = _hint(release_url, problems)
    return fields


async def _core_release_notes(
    client: Any, entity_id: str, language: str | None
) -> tuple[dict[str, Any] | None, list[str]]:
    """Core's reply, and a warning when Core refused ``language`` as a field
    it does not know (before 2026.12) and answered again without it."""
    message = {"type": "update/release_notes", "entity_id": entity_id}
    try:
        reply: dict[str, Any] = await client.send_websocket_message(
            message if language is None else {**message, "language": language},
            expected_error_codes=_ANSWERED_CODES | {"invalid_format"},
        )
        if language is None or reply.get("error_code") != "invalid_format":
            return reply, []
        reply = await client.send_websocket_message(
            message, expected_error_codes=_ANSWERED_CODES
        )
    except HomeAssistantConnectionError as e:
        logger.debug(f"update/release_notes unanswered for {entity_id}: {e}")
        return None, []
    return reply, [
        "This Home Assistant takes no language for release notes (2026.12+), "
        "so they come in its configured language"
    ]


def _hint(release_url: str | None, problems: list[str]) -> str:
    hint = "Release notes could not be fetched automatically"
    hint += f" ({'; '.join(problems)})." if problems else "."
    if release_url:
        hint += f" View them at: {release_url}"
    return hint


async def _github_release_notes(
    release_url: str | None, core_version: str | None, problems: list[str]
) -> tuple[str, str] | None:
    """The GitHub release body or changelog file, with where it came from;
    a used-up rate limit goes into ``problems``."""
    match = _GITHUB_RELEASE.match(release_url or "")
    if not match and not core_version:
        return None
    async with httpx.AsyncClient(
        timeout=15.0, headers={"User-Agent": GITHUB_API_HEADERS["User-Agent"]}
    ) as http:
        if match:
            owner, repo, tag = match.groups()
            body = await _release_body(http, owner, repo, tag, problems)
            if body:
                return body, "github_api"
            changelog = await _changelog_file(http, owner, repo, tag)
            if changelog:
                return changelog, "github_raw"
        if core_version:
            body = await _release_body(
                http, "home-assistant", "core", core_version, problems
            )
            if body:
                return body, "github_api"
    return None


async def _release_body(
    http: httpx.AsyncClient, owner: str, repo: str, tag: str, problems: list[str]
) -> str | None:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
    try:
        response = await http.get(url, headers=GITHUB_API_HEADERS)
        release = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as e:
        logger.debug(f"GitHub release {url} not read: {e}")
        return None
    if response.status_code == 403 and _rate_limited(response):
        problems.append(
            http_problem(f"GitHub's release {owner}/{repo} {tag}", response)
        )
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
