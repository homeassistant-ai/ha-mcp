"""``ha_manage_updates`` on a real ``hass``.

Updates are entities of Core's ``update`` integration (``_tools_updates_core``);
what the tool reads from GitHub, the Home Assistant blog and PyPI is served by
respx.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.update import UpdateEntityFeature
from homeassistant.core import HomeAssistant

from ha_mcp import update_check
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError

from ._tool_errors import error_of
from ._tools_updates_core import (
    CORE_UPDATE,
    SoftwareUpdate,
    Web,
    core_release,
    manage_updates,
    set_up_updates,
)
from ._tools_updates_core import web as web

INSTALLABLE = UpdateEntityFeature.INSTALL | UpdateEntityFeature.BACKUP
BLOG = "https://www.home-assistant.io/blog/2025/12/03/release-202512/"
RELEASE_URL = "https://github.com/owner/repo/releases/tag/v1.0.0"
RAW = "https://raw.githubusercontent.com/owner/repo/v1.0.0/"


@pytest.fixture(autouse=True)
def no_self_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server's own PyPI check stays off unless a test turns it on."""
    monkeypatch.setenv(update_check.DISABLE_ENV, "1")


async def listed(
    hass: HomeAssistant, client: HomeAssistantClient, update: SoftwareUpdate
) -> dict[str, Any]:
    """The one listed update, as ``list`` reports it."""
    await set_up_updates(hass, update)
    result = await manage_updates(client)
    [entry] = result["updates"]
    return dict(entry)


async def core_analysis(
    hass: HomeAssistant, client: HomeAssistantClient, installed: str, latest: str
) -> dict[str, Any]:
    """``get`` with release analysis on a Core update from ``installed`` to ``latest``."""
    await set_up_updates(
        hass,
        SoftwareUpdate(
            CORE_UPDATE, title="Home Assistant Core", installed=installed, latest=latest
        ),
    )
    return await manage_updates(
        client, action="get", entity_ids=[CORE_UPDATE], include_release_notes=True
    )


class TestServerOwnUpdate:
    """``list`` also tells whether a newer ha-mcp is out."""

    async def test_newer_server_release_is_reported(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        web: Web,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for name in (update_check.DISABLE_ENV, "SUPERVISOR_TOKEN", "HA_MCP_EMBEDDED"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HA_MCP_BUILD_VERSION", "7.8.0")
        web.serve(
            "https://pypi.org/pypi/ha-mcp/json", json={"info": {"version": "7.9.0"}}
        )
        await set_up_updates(hass)

        result = await manage_updates(core_client)

        assert result["ha_mcp_update"] == {
            "current": "7.8.0",
            "latest": "7.9.0",
            "update_available": True,
        }

    async def test_no_server_update_field_when_the_check_is_off(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(hass)

        result = await manage_updates(core_client)

        assert "ha_mcp_update" not in result


class TestCategories:
    """Each pending update is filed under the category the Updates page uses."""

    @pytest.mark.parametrize(
        ("entity_id", "title", "category"),
        [
            pytest.param(CORE_UPDATE, None, "core", id="core"),
            pytest.param(
                "update.some_core_entity",
                "Home Assistant Core Update",
                "other",
                id="core-in-id-but-not-home-assistant-core",
            ),
            pytest.param("update.home_assistant_operating_system", None, "os", id="os"),
            pytest.param(
                "update.home_assistant_supervisor_update", None, "supervisor", id="sv"
            ),
            pytest.param("update.some_addon_update", "Some Add-on", "addons", id="app"),
            pytest.param("update.esphome_device_firmware", None, "devices", id="esp"),
            pytest.param(
                "update.slzb_06m_core",
                "SLZB-06M Core firmware",
                "devices",
                id="firmware-title",
            ),
            pytest.param("update.unknown_thing", "Unknown", "other", id="other"),
        ],
    )
    async def test_update_lands_in_its_category(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        entity_id: str,
        title: str | None,
        category: str,
    ) -> None:
        entry = await listed(hass, core_client, SoftwareUpdate(entity_id, title=title))

        assert entry["category"] == category

    @pytest.mark.parametrize("integration", ["hacs", "marketplace"])
    async def test_store_repository_update_is_hacs_whatever_its_entity_id(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, integration: str
    ) -> None:
        """The Marketplace (Core 2026.11) names its entities after the repository
        alone, so update.ha_mcp_custom_component carries no "hacs"."""
        await set_up_updates(
            hass,
            SoftwareUpdate("update.ha_mcp_custom_component"),
            integration=integration,
        )

        result = await manage_updates(core_client)

        assert [u["entity_id"] for u in result["categories"]["hacs"]] == [
            "update.ha_mcp_custom_component"
        ]


@pytest.mark.parametrize(
    ("features", "release_url", "supported"),
    [
        pytest.param(UpdateEntityFeature.RELEASE_NOTES, None, True, id="feature"),
        pytest.param(UpdateEntityFeature(0), RELEASE_URL, True, id="release-url"),
        pytest.param(UpdateEntityFeature(0), None, False, id="neither"),
        pytest.param(
            UpdateEntityFeature.INSTALL
            | UpdateEntityFeature.SPECIFIC_VERSION
            | UpdateEntityFeature.PROGRESS
            | UpdateEntityFeature.BACKUP,
            None,
            False,
            id="only-other-features",
        ),
    ],
)
async def test_list_says_which_updates_have_release_notes(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    features: UpdateEntityFeature,
    release_url: str | None,
    supported: bool,
) -> None:
    update = SoftwareUpdate("update.thing", features=features, release_url=release_url)

    entry = await listed(hass, core_client, update)

    assert entry["supports_release_notes"] is supported


class TestReleaseNotes:
    """``get`` returns an update's release notes from the first source that has them."""

    async def test_notes_come_from_the_update_entity_first(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        notes = "## 1.1.0\n- Faster startup"
        await set_up_updates(
            hass,
            SoftwareUpdate(
                "update.thing",
                features=UpdateEntityFeature.RELEASE_NOTES,
                release_url=RELEASE_URL,
                notes=notes,
            ),
        )

        result = await manage_updates(
            core_client, action="get", entity_ids=["update.thing"]
        )

        assert (result["release_notes"], result["release_notes_source"]) == (
            notes,
            "websocket",
        )
        assert web.requested == []

    async def test_hint_links_the_release_when_no_changelog_file_exists(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        await set_up_updates(
            hass, SoftwareUpdate("update.thing", release_url=RELEASE_URL)
        )

        result = await manage_updates(
            core_client, action="get", entity_ids=["update.thing"]
        )

        assert "release_notes" not in result
        assert result["release_notes_hint"] == (
            "Release notes could not be fetched automatically. "
            f"View them at: {RELEASE_URL}"
        )
        assert len([u for u in web.requested if u.startswith(RAW)]) == 5

    async def test_a_changelog_too_short_to_be_notes_is_not_returned(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        web.hosts["raw.githubusercontent.com"] = {"text": "too short"}
        await set_up_updates(
            hass, SoftwareUpdate("update.thing", release_url=RELEASE_URL)
        )

        result = await manage_updates(
            core_client, action="get", entity_ids=["update.thing"]
        )

        assert "release_notes" not in result
        assert "release_notes_hint" in result

    @pytest.mark.parametrize("first_file", ["unreachable", "missing"])
    async def test_changelog_is_read_from_the_next_file_when_one_fails(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        web: Web,
        first_file: str,
    ) -> None:
        if first_file == "unreachable":
            web.unreachable.add(RAW + "CHANGELOG.md")
        changelog = "# Releases\n\n## v1.0.0\n- First stable release of the thing"
        web.serve(RAW + "RELEASES.md", text=changelog)
        await set_up_updates(
            hass, SoftwareUpdate("update.thing", release_url=RELEASE_URL)
        )

        result = await manage_updates(
            core_client, action="get", entity_ids=["update.thing"]
        )

        assert (result["release_notes"], result["release_notes_source"]) == (
            changelog,
            "github_raw",
        )
        assert [u for u in web.requested if u.startswith(RAW)] == [
            RAW + "CHANGELOG.md",
            RAW + "RELEASES.md",
        ]


class TestCoreReleaseAnalysis:
    """``get`` with ``include_release_notes`` reads every monthly Core release
    between the installed and the offered version."""

    @pytest.mark.parametrize(
        ("installed", "latest", "checked"),
        [
            pytest.param("2025.11.3", "2025.12.0", ["2025.12.0"], id="next-month"),
            pytest.param(
                "2025.10.3",
                "2026.2.1",
                ["2025.11.0", "2025.12.0", "2026.1.0", "2026.2.0"],
                id="across-new-year",
            ),
            pytest.param("2025.11.0", "2025.11.3", [], id="same-month"),
            pytest.param("2025.11", "2025.12.0", ["2025.12.0"], id="no-patch-part"),
            pytest.param(
                "2025.12.0b1", "2026.1.0", ["2026.1.0"], id="unreadable-installed"
            ),
        ],
    )
    async def test_every_monthly_release_in_between_is_checked(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        web: Web,
        installed: str,
        latest: str,
        checked: list[str],
    ) -> None:
        web.hosts["api.github.com"] = {
            "json": {"body": "- Fix ([hue docs]) (breaking-change)"}
        }

        result = await core_analysis(hass, core_client, installed, latest)

        assert result["breaking_changes"]["versions_checked"] == checked

    @pytest.mark.parametrize(
        ("page", "content"),
        [
            pytest.param(
                "<h1>Blog</h1><nav>Nav</nav><article><h2>Release</h2>"
                "<p><b>Great</b> stuff</p><ul><li>One</li><li>Two</li></ul>"
                "</article><aside>Related posts</aside><footer>F</footer>",
                "Release\nGreat stuff\n\n- One\n- Two",
                id="article",
            ),
            pytest.param(
                '<div>Nav</div><h1>Release</h1><p>Content</p><footer class="f">F</footer>',
                "Release\nContent",
                id="from-the-heading",
            ),
            pytest.param("just plain text", "just plain text", id="plain-page"),
        ],
    )
    async def test_blog_post_body_is_returned_as_plain_text(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        web: Web,
        page: str,
        content: str,
    ) -> None:
        web.serve(core_release("2025.12.0"), json={"body": BLOG})
        web.serve(BLOG, text=page)

        result = await core_analysis(hass, core_client, "2025.11.3", "2025.12.0")

        assert result["multi_version_release_notes"] == [
            {"version": "2025.12.0", "content": content, "source_url": BLOG}
        ]

    async def test_an_empty_blog_post_adds_no_release_notes(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        web.serve(core_release("2025.12.0"), json={"body": BLOG})
        web.serve(BLOG, text="")

        result = await core_analysis(hass, core_client, "2025.11.3", "2025.12.0")

        assert result["multi_version_release_notes"] == []
        assert result["breaking_changes"]["versions_checked"] == ["2025.12.0"]

    async def test_blog_breaking_changes_are_listed_per_integration(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        web.serve(core_release("2025.12.0"), json={"body": BLOG})
        web.serve(
            BLOG,
            text="<article>"
            '<h2 id="backward-incompatible-changes">Backward-incompatible changes</h2>'
            "<h3>Tuya</h3><p>HVACMode converted to presets.</p>"
            "<h3>Group</h3><p>Sensor group behavior changed.</p>"
            '<h2 id="all-changes">All changes</h2></article>',
        )

        result = await core_analysis(hass, core_client, "2025.11.3", "2025.12.0")

        assert result["breaking_changes"]["entries"] == [
            {
                "integration": "Tuya",
                "description": "HVACMode converted to presets.",
                "version": "2025.12.0",
            },
            {
                "integration": "Group",
                "description": "Sensor group behavior changed.",
                "version": "2025.12.0",
            },
        ]
        assert result["breaking_changes"]["count"] == 2

    @pytest.mark.parametrize(
        "page",
        [
            pytest.param("<article><h2>Other</h2><p>x</p></article>", id="no-section"),
            pytest.param(
                '<article><h2 id="backward-incompatible-changes">BC</h2>'
                '<h2 id="next">N</h2></article>',
                id="empty-section",
            ),
        ],
    )
    async def test_blog_without_breaking_changes_lists_none(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        web: Web,
        page: str,
    ) -> None:
        web.serve(core_release("2025.12.0"), json={"body": BLOG})
        web.serve(BLOG, text=page)

        result = await core_analysis(hass, core_client, "2025.11.3", "2025.12.0")

        assert result["breaking_changes"] == {
            "entries": [],
            "count": 0,
            "versions_checked": ["2025.12.0"],
        }

    @pytest.mark.parametrize(
        ("line", "integration", "description"),
        [
            pytest.param(
                "- Tuya fix ([tuya docs]) (breaking-change)",
                "tuya",
                "Tuya fix ([tuya docs])",
                id="docs-link",
            ),
            pytest.param(
                "- Fix ([vesync documentation]) (breaking-change)",
                "vesync",
                "Fix ([vesync documentation])",
                id="documentation-link",
            ),
            pytest.param(
                "- Some change (breaking-change)",
                "unknown",
                "Some change",
                id="no-link",
            ),
            pytest.param(
                "- Fix ([hue docs]) (Breaking-Change)",
                "hue",
                "Fix ([hue docs])",
                id="any-case",
            ),
        ],
    )
    async def test_tagged_release_line_is_a_breaking_change(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        web: Web,
        line: str,
        integration: str,
        description: str,
    ) -> None:
        body = f"- Normal fix\n{line}\n- Another fix\n"
        web.serve(core_release("2025.12.0"), json={"body": body})

        result = await core_analysis(hass, core_client, "2025.11.3", "2025.12.0")

        assert result["breaking_changes"]["entries"] == [
            {
                "integration": integration,
                "description": description,
                "version": "2025.12.0",
            }
        ]

    async def test_release_without_tagged_lines_has_no_breaking_changes(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        web.serve(core_release("2025.12.0"), json={"body": "- Normal fix\n"})

        result = await core_analysis(hass, core_client, "2025.11.3", "2025.12.0")

        assert result["breaking_changes"] == {
            "entries": [],
            "count": 0,
            "versions_checked": [],
        }


class TestBatchActions:
    """install / skip act on each target and report every outcome."""

    async def test_category_install_skips_updates_already_in_progress(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        idle = SoftwareUpdate("update.addon_a", title="Add-on A", features=INSTALLABLE)
        busy = SoftwareUpdate(
            "update.addon_b",
            title="Add-on B",
            features=INSTALLABLE | UpdateEntityFeature.PROGRESS,
            in_progress=True,
        )
        await set_up_updates(hass, idle, busy)

        result = await manage_updates(
            core_client, action="install", categories=["addons"], backup=True
        )

        assert (idle.installs, busy.installs) == ([(None, True)], [])
        assert (result["requested"], result["succeeded"], result["failed"]) == (1, 1, 0)
        assert result["results"] == [{"success": True, "entity_id": "update.addon_a"}]

    async def test_skip_reports_a_missing_entity_and_skips_the_rest(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(hass, SoftwareUpdate("update.exists"))

        result = await manage_updates(
            core_client, action="skip", entity_ids=["update.exists", "update.missing"]
        )

        state = hass.states.get("update.exists")
        assert state is not None
        assert state.attributes["skipped_version"] == "1.1.0"
        assert (result["requested"], result["succeeded"], result["failed"]) == (2, 1, 1)
        assert result["success"] is False
        [failure] = [r for r in result["results"] if not r.get("success")]
        assert failure["entity_id"] == "update.missing"
        assert failure["error"]["code"] == "ENTITY_NOT_FOUND"

    async def test_an_update_core_refuses_does_not_stop_the_batch(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(
            hass,
            SoftwareUpdate("update.auto", auto_update=True),
            SoftwareUpdate("update.manual"),
        )

        result = await manage_updates(
            core_client, action="skip", entity_ids=["update.auto", "update.manual"]
        )

        state = hass.states.get("update.manual")
        assert state is not None
        assert state.attributes["skipped_version"] == "1.1.0"
        assert (result["requested"], result["succeeded"], result["failed"]) == (2, 1, 1)
        [failure] = [r for r in result["results"] if not r.get("success")]
        assert failure["entity_id"] == "update.auto"
        assert failure["error"]["code"] == "SERVICE_CALL_FAILED"

    async def test_lost_connection_ends_the_batch_with_a_connection_error(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await set_up_updates(
            hass, SoftwareUpdate("update.first"), SoftwareUpdate("update.second")
        )
        with (
            patch.object(
                core_client,
                "call_service",
                AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
            ) as call_service,
            pytest.raises(ToolError) as exc_info,
        ):
            await manage_updates(
                core_client, action="skip", entity_ids=["update.first", "update.second"]
            )

        assert error_of(exc_info)["code"] == "CONNECTION_FAILED"
        assert call_service.await_count == 1
