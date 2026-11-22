"""Release notes and the Core breaking-change analysis of ``ha_manage_updates``.

Updates are entities of Core's ``update`` integration (``_tools_updates_core``);
GitHub and the Home Assistant blog are served by respx from the real pages cut
into ``_core_release_pages``.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.update import UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import update_check
from ha_mcp.client.rest_client import HomeAssistantClient

from ._core_release_pages import (
    BLOG_202611,
    RELEASES,
    blog_page,
    listing,
    release,
)
from ._tools_updates_core import (
    CORE_UPDATE,
    SoftwareUpdate,
    Web,
    core_release,
    manage_updates,
    set_up_updates,
)
from ._tools_updates_core import web as web

RELEASE_URL = "https://github.com/owner/repo/releases/tag/v1.0.0"
RAW = "https://raw.githubusercontent.com/owner/repo/v1.0.0/"
CORE_TITLE = "Home Assistant Core"


class UnreachableUpdate(SoftwareUpdate):
    """An update whose device does not answer."""

    _attr_available = False


@pytest.fixture(autouse=True)
def no_self_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server's own PyPI check stays off."""
    monkeypatch.setenv(update_check.DISABLE_ENV, "1")


async def get_update(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    update: SoftwareUpdate,
    integration: str = "test",
) -> dict[str, Any]:
    await set_up_updates(hass, update, integration=integration)
    return await manage_updates(client, action="get", entity_ids=[update.entity_id])


async def core_analysis(
    hass: HomeAssistant, client: HomeAssistantClient, installed: str, latest: str
) -> dict[str, Any]:
    """``get`` with release analysis on a Core update from ``installed`` to ``latest``."""
    assert await async_setup_component(hass, "repairs", {})
    await set_up_updates(
        hass,
        SoftwareUpdate(
            CORE_UPDATE, title=CORE_TITLE, installed=installed, latest=latest
        ),
        integration="hassio",
    )
    return await manage_updates(
        client, action="get", entity_ids=[CORE_UPDATE], include_release_notes=True
    )


class TestReleaseNotes:
    """``get`` returns an update's release notes from the first source that has them."""

    async def test_notes_come_from_the_update_entity_first(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        notes = "## 1.1.0\n- Faster startup"
        update = SoftwareUpdate(
            "update.thing",
            features=UpdateEntityFeature.RELEASE_NOTES,
            release_url=RELEASE_URL,
            notes=notes,
        )

        result = await get_update(hass, core_client, update)

        assert (result["release_notes"], result["release_notes_source"]) == (
            notes,
            "websocket",
        )
        assert web.requested == []

    async def test_an_update_core_has_no_notes_for_gets_its_github_release(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        """Core answers not_supported for an entity without release notes."""
        body = "## v1.0.0\n- First stable release"
        web.serve(
            "https://api.github.com/repos/owner/repo/releases/tags/v1.0.0",
            json={"body": body},
        )

        result = await get_update(
            hass, core_client, SoftwareUpdate("update.thing", release_url=RELEASE_URL)
        )

        assert (result["release_notes"], result["release_notes_source"]) == (
            body,
            "github_api",
        )

    async def test_hint_links_the_release_when_no_changelog_file_exists(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        result = await get_update(
            hass, core_client, SoftwareUpdate("update.thing", release_url=RELEASE_URL)
        )

        assert "release_notes" not in result
        assert RELEASE_URL in result["release_notes_hint"]

    async def test_an_update_without_a_release_link_still_says_notes_are_missing(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        result = await get_update(hass, core_client, SoftwareUpdate("update.thing"))

        assert "release_notes" not in result
        assert "release_notes_hint" in result

    async def test_hint_carries_the_code_core_refused_the_notes_with(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        update = UnreachableUpdate(
            "update.thing", features=UpdateEntityFeature.RELEASE_NOTES
        )

        result = await get_update(hass, core_client, update)

        assert "home_assistant_error" in result["release_notes_hint"]

    async def test_a_changelog_too_short_to_be_notes_is_not_returned(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        web.hosts["raw.githubusercontent.com"] = {"text": "too short"}

        result = await get_update(
            hass, core_client, SoftwareUpdate("update.thing", release_url=RELEASE_URL)
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

        result = await get_update(
            hass, core_client, SoftwareUpdate("update.thing", release_url=RELEASE_URL)
        )

        assert (result["release_notes"], result["release_notes_source"]) == (
            changelog,
            "github_raw",
        )
        assert [u for u in web.requested if u.startswith(RAW)] == [
            RAW + "CHANGELOG.md",
            RAW + "RELEASES.md",
        ]

    async def test_core_update_gets_the_github_release_of_the_offered_version(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        """The Core update is known by what it is, whatever its entity_id."""
        web.serve(core_release("2026.11.2"), json=release("2026.11.2"))

        result = await get_update(
            hass,
            core_client,
            SoftwareUpdate(
                "update.renamed_by_the_user",
                title=CORE_TITLE,
                installed="2026.11.1",
                latest="2026.11.2",
            ),
            integration="hassio",
        )

        assert result["release_notes"] == release("2026.11.2")["body"]

    async def test_firmware_named_core_does_not_get_home_assistant_core_notes(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        """A Zigbee coordinator called "core" is not Home Assistant Core."""
        web.serve(core_release("2026.11.2"), json=release("2026.11.2"))

        result = await get_update(
            hass,
            core_client,
            SoftwareUpdate(
                "update.slzb_06m_core",
                title="SLZB-06M Core firmware",
                latest="2026.11.2",
            ),
        )

        assert "release_notes" not in result


class TestCoreReleaseAnalysis:
    """``get`` with ``include_release_notes`` reads every stable Core release
    after the installed version up to the offered one."""

    async def test_breaking_changes_of_a_monthly_release_are_read_from_its_blog(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        """#3170: every Core update read as no breaking changes, nothing checked."""
        web.serve(
            RELEASES,
            json=listing(
                "2026.11.2", "2026.11.1", "2026.11.0", "2026.11.0b6", "2026.10.4"
            ),
        )
        web.serve(BLOG_202611, text=blog_page())

        result = await core_analysis(hass, core_client, "2026.10.4", "2026.11.2")

        changes = result["breaking_changes"]
        assert [
            (entry["version"], entry["integration"], entry["domains"])
            for entry in changes["entries"]
        ] == [
            ("2026.11.0", "AdGuard Home", ["adguard"]),
            ("2026.11.0", "Alert card", []),
            ("2026.11.0", "Calendar and Sun", ["calendar", "sun"]),
        ]
        assert changes["versions_checked"] == ["2026.11.0", "2026.11.1", "2026.11.2"]
        assert changes["versions_unverified"] == []

    async def test_glossary_tooltips_stay_out_of_a_breaking_change_description(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        web.serve(RELEASES, json=listing("2026.11.0", "2026.10.4"))
        web.serve(BLOG_202611, text=blog_page())

        result = await core_analysis(hass, core_client, "2026.10.4", "2026.11.0")

        [alert_card] = [
            e
            for e in result["breaking_changes"]["entries"]
            if e["integration"] == "Alert card"
        ]
        assert alert_card["description"].startswith(
            "The Alert card now shows its color and its pulse only while the entity "
            "is in an alert state"
        )
        assert "An entity represents" not in alert_card["description"]

    async def test_the_release_post_text_comes_with_the_analysis(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        web.serve(RELEASES, json=listing("2026.11.0", "2026.10.4"))
        web.serve(BLOG_202611, text=blog_page())

        result = await core_analysis(hass, core_client, "2026.10.4", "2026.11.0")

        [notes] = result["multi_version_release_notes"]
        assert (notes["version"], notes["source_url"]) == ("2026.11.0", BLOG_202611)
        assert notes["content"].startswith("2026.11: A lot in store!")
        assert "AdGuard Home" in notes["content"]

    async def test_only_stable_releases_inside_the_range_are_checked(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        web.serve(
            RELEASES,
            json=listing(
                "2026.11.0",
                "2026.11.0b6",
                "2026.10.4",
                "2026.10.3",
                "2026.10.2",
                "2026.10.1",
                "2026.10.0",
            ),
        )

        result = await core_analysis(hass, core_client, "2026.10.0", "2026.10.2")

        assert result["breaking_changes"]["versions_checked"] == [
            "2026.10.1",
            "2026.10.2",
        ]
        assert BLOG_202611 not in web.requested

    async def test_a_tagged_patch_release_line_is_a_breaking_change(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        web.serve(RELEASES, json=listing("2026.10.2", "2026.10.1", "2026.10.0"))

        result = await core_analysis(hass, core_client, "2026.10.0", "2026.10.2")

        assert result["breaking_changes"]["entries"] == [
            {
                "version": "2026.10.1",
                "integration": "omnilogic",
                "description": "Remove user-configurable polling interval from "
                "`omnilogic` ([@tr4nt0r] - [#184380]) ([omnilogic docs]) "
                "(breaking-change)",
                "domains": ["omnilogic"],
                "installed": False,
            }
        ]
        assert "2026.10.2" in result["breaking_changes"]["versions_checked"]

    @pytest.mark.parametrize(
        ("github", "blog", "unverified"),
        [
            pytest.param(
                {
                    "json": [
                        release("2026.11.0", "Home Assistant 2026.11 is here!\n"),
                        release("2026.10.4"),
                    ]
                },
                {"text": blog_page()},
                "2026.11.0",
                id="release-without-blog-link",
            ),
            pytest.param(
                {"json": listing("2026.11.0", "2026.10.4")},
                {"status": 500, "text": "Internal Server Error"},
                "2026.11.0",
                id="blog-error",
            ),
            pytest.param(
                {"json": listing("2026.11.0", "2026.10.4")},
                {"text": blog_page("<h2>All changes</h2>\n<p>Full changelog</p>\n")},
                "2026.11.0",
                id="no-breaking-changes-section",
            ),
            pytest.param(
                {
                    "status": 403,
                    "headers": {"X-RateLimit-Remaining": "0"},
                    "json": {"message": "API rate limit exceeded"},
                },
                {"text": blog_page()},
                ">2026.10.4,<=2026.11.0",
                id="listing-refused",
            ),
        ],
    )
    async def test_a_release_that_could_not_be_read_is_never_reported_as_checked(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        web: Web,
        github: dict[str, Any],
        blog: dict[str, Any],
        unverified: str,
    ) -> None:
        web.serve(RELEASES, **github)
        web.serve(BLOG_202611, **blog)

        result = await core_analysis(hass, core_client, "2026.10.4", "2026.11.0")

        changes = result["breaking_changes"]
        assert [u["version"] for u in changes["versions_unverified"]] == [unverified]
        assert changes["versions_checked"] == []
        assert changes["count"] == 0
        assert any(unverified in warning for warning in result["warnings"])

    async def test_a_beta_target_is_reported_not_guessed(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        result = await core_analysis(hass, core_client, "2026.11.2", "2026.12.0b1")

        assert [
            u["version"] for u in result["breaking_changes"]["versions_unverified"]
        ] == [">2026.11.2,<=2026.12.0b1"]
        assert RELEASES not in web.requested

    async def test_releases_older_than_the_listing_are_reported_unverified(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        web.serve(RELEASES, json=listing("2026.11.0", "2026.10.4"))
        web.serve(BLOG_202611, text=blog_page())

        result = await core_analysis(hass, core_client, "2026.9.4", "2026.11.0")

        changes = result["breaking_changes"]
        assert changes["versions_checked"] == ["2026.10.4", "2026.11.0"]
        assert [u["version"] for u in changes["versions_unverified"]] == [
            ">2026.9.4,<2026.10.4"
        ]

    async def test_an_entry_says_whether_a_loaded_integration_is_concerned(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        assert await async_setup_component(hass, "calendar", {})
        web.serve(RELEASES, json=listing("2026.11.0", "2026.10.4"))
        web.serve(BLOG_202611, text=blog_page())

        result = await core_analysis(hass, core_client, "2026.10.4", "2026.11.0")

        changes = result["breaking_changes"]
        assert [(e["integration"], e["installed"]) for e in changes["entries"]] == [
            ("AdGuard Home", False),
            ("Alert card", None),
            ("Calendar and Sun", True),
        ]
        assert changes["affecting_installed"] == 1

    async def test_an_integration_with_a_config_entry_counts_as_installed(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        """Its entry may not be loaded (failed or disabled); it is still installed."""
        MockConfigEntry(domain="adguard").add_to_hass(hass)
        web.serve(RELEASES, json=listing("2026.11.0", "2026.10.4"))
        web.serve(BLOG_202611, text=blog_page())

        result = await core_analysis(hass, core_client, "2026.10.4", "2026.11.0")

        assert "adguard" in result["installed_integrations"]
        [adguard] = [
            e
            for e in result["breaking_changes"]["entries"]
            if e["integration"] == "AdGuard Home"
        ]
        assert adguard["installed"] is True

    async def test_repairs_issues_core_says_will_break_are_listed(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        assert await async_setup_component(hass, "repairs", {})
        for issue_id, breaks in (("deprecated_yaml", "2026.12.0"), ("other", None)):
            ir.async_create_issue(
                hass,
                "sun",
                issue_id,
                breaks_in_ha_version=breaks,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=issue_id,
            )
        web.serve(RELEASES, json=listing("2026.11.0", "2026.10.4"))

        result = await core_analysis(hass, core_client, "2026.10.4", "2026.11.0")

        assert [
            (issue["domain"], issue["issue_id"], issue["breaks_in_ha_version"])
            for issue in result["breaking_changes"]["repairs"]
        ] == [("sun", "deprecated_yaml", "2026.12.0")]
