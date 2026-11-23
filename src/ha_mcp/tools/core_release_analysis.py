"""Breaking changes between the installed and the offered Home Assistant Core.

GitHub lists Core's releases with their bodies. A monthly release (``x.y.0``)
links its blog post, whose "Backward-incompatible changes" section holds one
``<details>`` block per change; a patch release tags each breaking pull request
line ``(breaking-change)``. A release that could not be read is reported as
unverified, never as checked.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import httpx

from ..client.exceptions import HomeAssistantConnectionError, HomeAssistantError
from .update_release_notes import GITHUB_API_HEADERS, http_problem

logger = logging.getLogger(__name__)

_RELEASES_API = "https://api.github.com/repos/home-assistant/core/releases"
_RELEASES_PAGE = "https://github.com/home-assistant/core/releases"
_PER_PAGE = 100
_MAX_PAGES = 3
_BLOG_LINK = re.compile(r"https://www\.home-assistant\.io/blog/[^\s()\[\]<>]+/")
_DOCS_LABEL = re.compile(r"\[([a-z0-9_]+) docs\]")
_INTEGRATION_LINK = re.compile(
    r"^(?:https://www\.home-assistant\.io)?/integrations/([a-z0-9_]+)/"
)
_BREAKING_TAG = "(breaking-change)"
_SECTION_ANCHOR = "backward-incompatible-changes"
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_LINE_BREAKS = frozenset({"p", "br", "div", "li", "tr", *_HEADINGS})
_HIDDEN = frozenset({"script", "style", "svg"})


def _version(tag: str) -> tuple[int, ...] | None:
    """``2026.11.2`` as a comparable tuple; None for a beta or any other form."""
    parts = tag.split(".")
    if not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


_PRERELEASE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:a|b|rc|\.dev)\d")


def _below_its_release(version: str) -> tuple[int, ...] | None:
    """The bound just below the release a prerelease such as ``2026.11.0b3``
    leads to, so that release counts as newer than it; None for any other form."""
    if not (match := _PRERELEASE.match(version)):
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch - 1)


@dataclass
class _Entry:
    summary: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    domains: set[str] = field(default_factory=set)
    depth: int = 1


class _BlogPost(HTMLParser):
    """The article text and the backward-incompatible changes of a release post."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page: list[str] = []
        self.article: list[str] | None = None
        self.entries: list[_Entry] | None = None
        self._hidden: tuple[str, int] | None = None
        self._in_article = 0
        self._heading: str | None = None
        self._heading_anchored = False
        self._section_open = False
        self._entry: _Entry | None = None
        self._in_summary = False

    def _buffers(self) -> list[list[str]]:
        buffers = [self.page]
        if self._in_article and self.article is not None:
            buffers.append(self.article)
        if self._entry:
            buffers.append(
                self._entry.summary if self._in_summary else self._entry.text
            )
        return buffers

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            for buffer in self._buffers():
                buffer.append(re.sub(r"\s+", " ", data))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self._hidden:
            if tag == self._hidden[0]:
                self._hidden = (tag, self._hidden[1] + 1)
            return
        classes = (attributes.get("class") or "").split()
        if tag in _HIDDEN or "terminology-tooltip" in classes:
            self._hidden = (tag, 1)
            return
        if tag == "article":
            self._in_article += 1
            self.article = self.article if self.article is not None else []
        self._track_section(tag, attributes)
        self._track_entry(tag, attributes)
        if tag in _LINE_BREAKS:
            self._line_break("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if self._hidden:
            if tag == self._hidden[0]:
                count = self._hidden[1] - 1
                self._hidden = (tag, count) if count else None
            return
        if tag == "article" and self._in_article:
            self._in_article -= 1
        elif tag == self._heading:
            self._end_heading()
        elif tag == "summary":
            self._in_summary = False
        elif tag == "details" and self._entry:
            self._end_details(self._entry)
        if tag in _LINE_BREAKS and tag != "br":
            self._line_break("\n")

    def _end_heading(self) -> None:
        if self._heading_anchored and self.entries is None:
            self._section_open = True
            self.entries = []
        self._heading = None

    def _end_details(self, entry: _Entry) -> None:
        entry.depth -= 1
        if not entry.depth:
            if self.entries is not None:
                self.entries.append(entry)
            self._entry = None

    def _line_break(self, text: str) -> None:
        for buffer in self._buffers():
            buffer.append(text)

    def _track_section(self, tag: str, attributes: dict[str, str | None]) -> None:
        if tag in _HEADINGS:
            if tag == "h2":
                self._section_open = False
            self._heading = tag
            self._heading_anchored = False
        if self._heading and _SECTION_ANCHOR in (
            attributes.get("id"),
            attributes.get("name"),
        ):
            self._heading_anchored = True

    def _track_entry(self, tag: str, attributes: dict[str, str | None]) -> None:
        if tag == "details" and self._entry:
            self._entry.depth += 1
        elif tag == "details" and self._section_open:
            self._entry = _Entry()
        elif tag == "summary" and self._entry:
            self._in_summary = True
        elif tag == "a" and self._entry:
            link = _INTEGRATION_LINK.match(attributes.get("href") or "")
            if link:
                self._entry.domains.add(link.group(1))


def _tidy(parts: list[str]) -> str:
    """Join parsed text, one blank line at most between blocks."""
    lines = [line.strip() for line in "".join(parts).split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _read_blog(html: str) -> tuple[str, list[dict[str, Any]] | None]:
    """The post's text, and its breaking changes (None when they cannot be read)."""
    post = _BlogPost()
    post.feed(html)
    post.close()
    text = _tidy(post.article if post.article is not None else post.page)
    if not post.entries:
        return text, None
    entries = [
        {
            "integration": _tidy(entry.summary),
            "description": _tidy(entry.text),
            "domains": sorted(entry.domains),
        }
        for entry in post.entries
    ]
    if not all(entry["integration"] for entry in entries):
        return text, None
    return text, entries


def _tagged_lines(body: str) -> list[dict[str, Any]]:
    """The breaking changes a patch release body tags, one per line."""
    entries = []
    for line in body.splitlines():
        if _BREAKING_TAG not in line.lower():
            continue
        domains = sorted(set(_DOCS_LABEL.findall(line)))
        entries.append(
            {
                "integration": ", ".join(domains) or None,
                "description": line.strip().lstrip("-* ").strip(),
                "domains": domains,
            }
        )
    return entries


@dataclass
class _Outcome:
    """What reading one release found."""

    version: str
    entries: list[dict[str, Any]] | None
    url: str
    reason: str | None = None
    notes: dict[str, str] | None = None


@dataclass
class _Releases:
    entries: list[dict[str, Any]] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    unverified: list[dict[str, str]] = field(default_factory=list)
    notes: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def unverified_release(self, version: str, reason: str, url: str) -> None:
        self.unverified.append({"version": version, "reason": reason, "url": url})
        self.warnings.append(
            f"Core {version} was not checked for breaking changes: {reason}. "
            f"No entries for it does not mean it has none; read {url}."
        )

    def add(self, outcome: _Outcome) -> None:
        if outcome.notes:
            self.notes.append(outcome.notes)
        if outcome.entries is None:
            self.unverified_release(
                outcome.version, outcome.reason or "not read", outcome.url
            )
            return
        self.checked.append(outcome.version)
        self.entries.extend(
            {"version": outcome.version, **entry} for entry in outcome.entries
        )


async def _releases_page(
    http: httpx.AsyncClient, page: int
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        response = await http.get(
            _RELEASES_API, params={"per_page": _PER_PAGE, "page": page}
        )
        rows = response.json() if response.status_code == 200 else None
    except httpx.HTTPError as e:
        return [], f"GitHub's release listing could not be reached ({e})"
    except ValueError:
        return [], "GitHub's release listing was not JSON"
    if rows is None:
        return [], http_problem("GitHub's release listing", response)
    if not isinstance(rows, list):
        return [], "GitHub's release listing was not a list"
    return [row for row in rows if isinstance(row, dict)], None


async def _list_releases(
    http: httpx.AsyncClient, installed: tuple[int, ...]
) -> tuple[list[dict[str, Any]], str | None]:
    """Releases newest first, back to ``installed``, and why the listing stops short."""
    listed: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        rows, problem = await _releases_page(http, page)
        if problem:
            return listed, problem
        listed.extend(rows)
        versions = [v for row in rows if (v := _version(str(row.get("tag_name"))))]
        if versions and versions[-1] <= installed:
            return listed, None
        if len(rows) < _PER_PAGE:
            return listed, "GitHub lists no older releases"
    return listed, f"GitHub's newest {_MAX_PAGES * _PER_PAGE} releases do not reach it"


def _stable_in_range(
    listed: list[dict[str, Any]],
    low: tuple[int, ...],
    high: tuple[int, ...],
    found: _Releases,
) -> list[tuple[tuple[int, ...], dict[str, Any]]]:
    in_range: dict[str, tuple[tuple[int, ...], dict[str, Any]]] = {}
    for release in listed:
        if release.get("prerelease") or release.get("draft"):
            continue
        tag = str(release.get("tag_name"))
        version = _version(tag)
        if version is None:
            found.unverified_release(
                tag,
                "its tag cannot be compared with the installed version",
                str(release.get("html_url") or _RELEASES_PAGE),
            )
        elif low < version <= high:
            in_range[tag] = (version, release)
    return sorted(in_range.values(), key=lambda item: item[0])


async def _read_monthly(
    http: httpx.AsyncClient, tag: str, body: str, release_url: str
) -> _Outcome:
    link = _BLOG_LINK.search(body)
    if not link:
        return _Outcome(tag, None, release_url, "its release names no blog post")
    url = link.group(0)
    try:
        response = await http.get(url)
    except httpx.HTTPError as e:
        return _Outcome(tag, None, url, f"its blog post could not be reached ({e})")
    if response.status_code != 200:
        return _Outcome(tag, None, url, http_problem("its blog post", response))
    text, entries = _read_blog(response.text)
    notes = {"version": tag, "content": text, "source_url": url} if text else None
    if entries is None:
        reason = "its blog post has no readable backward-incompatible changes section"
        return _Outcome(tag, None, url, reason, notes)
    return _Outcome(tag, entries, url, notes=notes)


async def _read_release(
    http: httpx.AsyncClient, version: tuple[int, ...], release: dict[str, Any]
) -> _Outcome:
    tag = str(release.get("tag_name"))
    body = str(release.get("body") or "")
    url = str(release.get("html_url") or _RELEASES_PAGE)
    if version[-1] == 0:
        return await _read_monthly(http, tag, body, url)
    return _Outcome(tag, _tagged_lines(body), url)


async def _check_releases(installed: str, target: str) -> _Releases:
    found = _Releases()
    low = _version(installed) or _below_its_release(installed)
    high = _version(target)
    span = f">{installed},<={target}"
    if low is None or high is None:
        unreadable = installed if low is None else target
        found.unverified_release(
            span,
            f"version {unreadable} cannot be compared with Core's release tags",
            _RELEASES_PAGE,
        )
        return found
    if high <= low:
        return found
    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, headers=GITHUB_API_HEADERS
    ) as http:
        listed, problem = await _list_releases(http, low)
        in_range = _stable_in_range(listed, low, high, found)
        outcomes = await asyncio.gather(
            *(_read_release(http, version, release) for version, release in in_range)
        )
    for outcome in outcomes:
        found.add(outcome)
    if problem:
        oldest = in_range[0][1].get("tag_name") if in_range else None
        found.unverified_release(
            f">{installed},<{oldest}" if oldest else span, problem, _RELEASES_PAGE
        )
    return found


async def _installed_integrations(
    client: Any, warnings: list[str]
) -> tuple[set[str], set[str]] | None:
    """The config-entry domains, and those plus every loaded component (both
    sides of ``a.b``), which is what a breaking change is matched against."""
    try:
        config = await client.get_config()
        entries = await client.list_config_entries()
    except HomeAssistantError as e:
        warnings.append(
            f"Installed integrations could not be read ({e}), so no breaking-change "
            "entry says whether it concerns this installation."
        )
        return None
    set_up = {str(entry.get("domain") or "") for entry in entries} - {""}
    loaded = set(set_up)
    for component in config.get("components") or []:
        loaded.update(str(component).split("."))
    return set_up, loaded - {""}


async def _repairs(client: Any, warnings: list[str]) -> list[dict[str, Any]] | None:
    """Active Repairs issues that carry Core's ``breaks_in_ha_version``."""
    try:
        reply = await client.send_websocket_message({"type": "repairs/list_issues"})
    except HomeAssistantConnectionError as e:
        reply = {"success": False, "error": str(e)}
    if not reply.get("success"):
        warnings.append(
            f"Repairs issues could not be read ({reply.get('error')}), so "
            "breaking_changes has no repairs list."
        )
        return None
    issues = (reply.get("result") or {}).get("issues") or []
    return [
        issue
        for issue in issues
        if isinstance(issue, dict) and issue.get("breaks_in_ha_version")
    ]


def _concerns(domains: list[str], installed: set[str] | None) -> bool | None:
    if not domains or installed is None:
        return None
    return any(domain in installed for domain in domains)


async def core_release_analysis(
    client: Any, installed: str, target: str
) -> dict[str, Any]:
    """Breaking changes of every stable Core release after ``installed`` up to ``target``."""
    integration_warnings: list[str] = []
    repair_warnings: list[str] = []
    integrations, repairs, releases = await asyncio.gather(
        _installed_integrations(client, integration_warnings),
        _repairs(client, repair_warnings),
        _check_releases(installed, target),
    )
    set_up, loaded = integrations or (set(), None)
    entries = [
        {**entry, "installed": _concerns(entry["domains"], loaded)}
        for entry in releases.entries
    ]
    breaking_changes: dict[str, Any] = {
        "entries": entries,
        "count": len(entries),
        "affecting_installed": sum(e["installed"] is True for e in entries),
        "versions_checked": releases.checked,
        "versions_unverified": releases.unverified,
    }
    if repairs is not None:
        breaking_changes["repairs"] = repairs
    return {
        "installed_integrations": sorted(set_up),
        "multi_version_release_notes": releases.notes,
        "breaking_changes": breaking_changes,
        "warnings": releases.warnings + integration_warnings + repair_warnings,
    }
