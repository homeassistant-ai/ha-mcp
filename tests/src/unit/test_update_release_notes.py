"""Release notes and the Core breaking-change analysis of ``ha_manage_updates``.

Updates are entities of Core's ``update`` integration (``_tools_updates_core``);
GitHub and the Home Assistant blog are served by respx, in the markup of the
real pages (``_core_release_pages``).
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components import update as update_component
from homeassistant.components.update import UpdateEntityFeature
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import update_check
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.helpers import core_error_message

from ._core_release_pages import (
    BLOG_202611,
    RELEASES,
    blog_page,
    listing,
    patch_release,
    release,
)
from ._hassio_updates_core import core_versions as core_versions
from ._hassio_updates_core import hassio_updates as hassio_updates
from ._tools_addons_core import Supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app
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


class UnreachableUpdate(SoftwareUpdate):
    """An update whose device does not answer."""

    online = False


class NotesFailUpdate(SoftwareUpdate):
    """An update whose device fails to send its release notes."""

    async def async_release_notes(self) -> str | None:
        raise HomeAssistantError("The device did not send its release notes")


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


async def core_analysis(client: HomeAssistantClient) -> dict[str, Any]:
    """``get`` with release analysis on Core's own Core update, which offers
    the update a test sets with ``core_versions``."""
    return await manage_updates(
        client, action="get", entity_ids=[CORE_UPDATE], include_release_notes=True
    )


class TestReleaseNotes:
    """``get`` returns an update's release notes from the first source that has them."""

    @pytest.mark.parametrize(
        "notes", ["Faster startup", None], ids=["notes", "no-notes"]
    )
    @pytest.mark.parametrize(
        "has_warning", [False, True], ids=["no-warning", "warning"]
    )
    async def test_update_entity_release_notes_and_messages(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        web: Web,
        notes: str | None,
        has_warning: bool,
    ) -> None:
        if has_warning and (MAJOR_VERSION, MINOR_VERSION) < (2026, 12):
            pytest.skip("Core added update messages in 2026.12")

        class FirmwareUpdate(SoftwareUpdate):
            @property
            def release_notes_messages(self) -> tuple[Any, ...]:
                if not has_warning:
                    return ()
                warning = getattr(
                    update_component, "RELEASE_NOTES_MESSAGE_DO_NOT_INTERRUPT", None
                )
                assert warning is not None
                return (warning,)

        github = "https://api.github.com/repos/owner/repo/releases/tags/v1.0.0"
        web.serve(github, json={"body": "Published changelog"})
        entity = FirmwareUpdate(
            "update.firmware",
            features=UpdateEntityFeature.RELEASE_NOTES,
            notes=notes,
            release_url=RELEASE_URL,
        )
        result = await get_update(hass, core_client, entity)
        if notes:
            assert (result["release_notes"], result["release_notes_source"]) == (
                notes,
                "websocket",
            )
            assert web.requested == []
        else:
            assert (result["release_notes"], result["release_notes_source"]) == (
                "Published changelog",
                "github_api",
            )
            assert github in web.requested
        if has_warning:
            [message] = result["release_notes_messages"]
            assert message["severity"] == "warning"
            assert message["message"]
        else:
            assert "release_notes_messages" not in result

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

    async def test_github_rate_limit_is_named_in_the_hint(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        """Otherwise the caller cannot tell a used-up limit from missing notes."""
        web.serve(
            "https://api.github.com/repos/owner/repo/releases/tags/v1.0.0",
            status=403,
            headers={"X-RateLimit-Remaining": "0"},
            json={"message": "API rate limit exceeded"},
        )

        result = await get_update(
            hass, core_client, SoftwareUpdate("update.thing", release_url=RELEASE_URL)
        )

        assert "HTTP 403" in result["release_notes_hint"]

    async def test_an_update_without_a_release_link_still_says_notes_are_missing(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        result = await get_update(hass, core_client, SoftwareUpdate("update.thing"))

        assert "release_notes" not in result
        assert "release_notes_hint" in result

    async def test_core_refusal_of_the_notes_is_shown_beside_github_notes(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        web.serve(
            "https://api.github.com/repos/owner/repo/releases/tags/v1.0.0",
            json={"body": "## v1.0.0\n- First stable release"},
        )
        update = NotesFailUpdate(
            "update.thing",
            features=UpdateEntityFeature.RELEASE_NOTES,
            release_url=RELEASE_URL,
        )

        result = await get_update(hass, core_client, update)

        refusal = await core_client.send_websocket_message(
            {"type": "update/release_notes", "entity_id": "update.thing"}
        )
        assert result["release_notes_error"] == {
            "code": refusal["error_code"],
            "message": core_error_message(refusal["error"]),
        }
        assert result["release_notes_source"] == "github_api"

    async def test_core_refusal_of_the_notes_is_not_repeated_in_the_hint(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, web: Web
    ) -> None:
        update = UnreachableUpdate(
            "update.thing", features=UpdateEntityFeature.RELEASE_NOTES
        )

        result = await get_update(hass, core_client, update)

        error = result["release_notes_error"]
        assert error["code"] not in result["release_notes_hint"]
        assert error["message"] not in result["release_notes_hint"]

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

    @pytest.mark.parametrize("core_versions", [("2026.11.1", "2026.11.2")])
    async def test_core_update_gets_the_github_release_of_the_offered_version(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
        entity_registry: er.EntityRegistry,
    ) -> None:
        """The Core update is known by what it is, whatever its entity_id."""
        web.serve(core_release("2026.11.2"), json=release("2026.11.2"))
        renamed = "update.renamed_by_the_user"
        entity_registry.async_update_entity(CORE_UPDATE, new_entity_id=renamed)
        await hass.async_block_till_done()

        result = await manage_updates(core_client, action="get", entity_ids=[renamed])

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

    @pytest.mark.parametrize("core_versions", [("2026.10.4", "2026.11.2")])
    async def test_breaking_changes_of_a_monthly_release_are_read_from_its_blog(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
    ) -> None:
        """#3170: every Core update read as no breaking changes, nothing checked."""
        web.listings[RELEASES] = listing(
            "2026.11.2", "2026.11.1", "2026.11.0", "2026.11.0b6", "2026.10.4"
        )
        web.serve(BLOG_202611, text=blog_page())

        result = await core_analysis(core_client)

        changes = result["breaking_changes"]
        assert [
            (entry["version"], entry["integration"], entry["domains"])
            for entry in changes["entries"]
        ] == [
            ("2026.11.0", "Example Hub", ["example_hub"]),
            ("2026.11.0", "Status card", []),
            ("2026.11.0", "Calendar and Tide", ["calendar", "tide"]),
        ]
        assert changes["versions_checked"] == ["2026.11.0", "2026.11.1", "2026.11.2"]
        assert changes["versions_unverified"] == []

    @pytest.mark.parametrize("core_versions", [("2026.10.4", "2026.11.0")])
    async def test_glossary_tooltips_stay_out_of_a_breaking_change_description(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
    ) -> None:
        web.listings[RELEASES] = listing("2026.11.0", "2026.10.4")
        web.serve(BLOG_202611, text=blog_page())

        result = await core_analysis(core_client)

        [status_card] = [
            e
            for e in result["breaking_changes"]["entries"]
            if e["integration"] == "Status card"
        ]
        assert status_card["description"].startswith(
            "The Status card now changes color only while the entity is in a "
            "placeholder state."
        )
        assert "glossary definition" not in status_card["description"]

    @pytest.mark.parametrize("core_versions", [("2026.10.4", "2026.11.0")])
    async def test_the_release_post_text_comes_with_the_analysis(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
    ) -> None:
        web.listings[RELEASES] = listing("2026.11.0", "2026.10.4")
        web.serve(BLOG_202611, text=blog_page())

        result = await core_analysis(core_client)

        [notes] = result["multi_version_release_notes"]
        assert (notes["version"], notes["source_url"]) == ("2026.11.0", BLOG_202611)
        assert notes["content"].startswith("2026.11: Placeholder title")
        assert "Example Hub" in notes["content"]

    @pytest.mark.parametrize("core_versions", [("2026.10.0", "2026.10.2")])
    async def test_only_stable_releases_inside_the_range_are_checked(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
    ) -> None:
        web.listings[RELEASES] = listing(
            "2026.11.0",
            "2026.11.0b6",
            "2026.10.4",
            "2026.10.3",
            "2026.10.2",
            "2026.10.1",
            "2026.10.0",
        )

        result = await core_analysis(core_client)

        assert result["breaking_changes"]["versions_checked"] == [
            "2026.10.1",
            "2026.10.2",
        ]
        assert BLOG_202611 not in web.requested

    @pytest.mark.parametrize("core_versions", [("2026.10.0", "2026.10.2")])
    async def test_a_tagged_patch_release_line_is_a_breaking_change(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
    ) -> None:
        web.listings[RELEASES] = listing("2026.10.2", "2026.10.1", "2026.10.0")

        result = await core_analysis(core_client)

        assert result["breaking_changes"]["entries"] == [
            {
                "version": "2026.10.1",
                "integration": "example_pool",
                "description": "Remove the placeholder interval from "
                "`example_pool` ([@contributor-three] - [#100020]) "
                "([example_pool docs]) (breaking-change)",
                "domains": ["example_pool"],
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
                        release("2026.11.0", "Placeholder announcement.\n"),
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
                {"text": blog_page("<h2>All changes</h2>\n<p>Placeholder</p>\n")},
                "2026.11.0",
                id="no-breaking-changes-section",
            ),
            pytest.param(
                {"json": listing("2026.11.0", "2026.10.4")},
                {
                    "text": blog_page(
                        '<h2>Backward-incompatible changes <a class="title-link"'
                        ' name="backward-incompatible-changes"'
                        ' href="#backward-incompatible-changes"></a>\n</h2>\n'
                        "<p>Placeholder introduction.</p>\n"
                        "<h2>All changes</h2>\n"
                    )
                },
                "2026.11.0",
                id="empty-breaking-changes-section",
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
    @pytest.mark.parametrize("core_versions", [("2026.10.4", "2026.11.0")])
    async def test_a_release_that_could_not_be_read_is_never_reported_as_checked(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
        github: dict[str, Any],
        blog: dict[str, Any],
        unverified: str,
    ) -> None:
        web.serve(RELEASES, **github)
        web.serve(BLOG_202611, **blog)

        result = await core_analysis(core_client)

        changes = result["breaking_changes"]
        assert [u["version"] for u in changes["versions_unverified"]] == [unverified]
        assert changes["versions_checked"] == []
        assert changes["count"] == 0
        assert any(unverified in warning for warning in result["warnings"])

    @pytest.mark.parametrize("core_versions", [("2026.11.0b3", "2026.11.1")])
    async def test_an_installed_beta_gets_the_releases_after_it_checked(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
    ) -> None:
        """Updating from a beta to the stable release read as "not checked" for
        the whole range, the release the beta led to included."""
        web.listings[RELEASES] = listing(
            "2026.11.1", "2026.11.0", "2026.11.0b6", "2026.10.4"
        )
        web.serve(BLOG_202611, text=blog_page())

        result = await core_analysis(core_client)

        changes = result["breaking_changes"]
        assert changes["versions_checked"] == ["2026.11.0", "2026.11.1"]
        assert changes["versions_unverified"] == []

    @pytest.mark.parametrize("core_versions", [("2026.11.2", "2026.12.0b1")])
    async def test_a_beta_target_is_reported_not_guessed(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
    ) -> None:
        result = await core_analysis(core_client)

        assert [
            u["version"] for u in result["breaking_changes"]["versions_unverified"]
        ] == [">2026.11.2,<=2026.12.0b1"]
        assert web.listing_pages(RELEASES) == []

    @pytest.mark.parametrize("core_versions", [("2026.9.4", "2026.11.0")])
    async def test_releases_older_than_the_listing_are_reported_unverified(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
    ) -> None:
        web.listings[RELEASES] = listing("2026.11.0", "2026.10.4")
        web.serve(BLOG_202611, text=blog_page())

        result = await core_analysis(core_client)

        changes = result["breaking_changes"]
        assert changes["versions_checked"] == ["2026.10.4", "2026.11.0"]
        assert [u["version"] for u in changes["versions_unverified"]] == [
            ">2026.9.4,<2026.10.4"
        ]

    @pytest.mark.parametrize("core_versions", [("2025.1.0", "2025.1.150")])
    async def test_a_range_longer_than_a_listing_page_is_checked_in_full(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
    ) -> None:
        newer = [f"2025.1.{n}" for n in range(150, 0, -1)]
        web.listings[RELEASES] = [*map(patch_release, newer), release("2025.1.0", "")]

        result = await core_analysis(core_client)

        changes = result["breaking_changes"]
        assert changes["versions_checked"] == newer[::-1]
        assert changes["versions_unverified"] == []

    @pytest.mark.parametrize("core_versions", [("2025.1.0", "2025.2.2000")])
    async def test_the_listing_stops_at_its_page_limit_and_says_what_it_missed(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
    ) -> None:
        """A listing that never reaches the installed version is not paged
        through to its end."""
        tags = [f"2025.2.{n}" for n in range(2000, 0, -1)]
        web.listings[RELEASES] = list(map(patch_release, tags))

        result = await core_analysis(core_client)

        changes = result["breaking_changes"]
        oldest = changes["versions_checked"][0]
        assert len(changes["versions_checked"]) < len(tags)
        assert [u["version"] for u in changes["versions_unverified"]] == [
            f">2025.1.0,<{oldest}"
        ]

    @pytest.mark.parametrize("core_versions", [("2026.10.4", "2026.11.0")])
    async def test_an_entry_says_whether_a_loaded_integration_is_concerned(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
    ) -> None:
        assert await async_setup_component(hass, "calendar", {})
        web.listings[RELEASES] = listing("2026.11.0", "2026.10.4")
        web.serve(BLOG_202611, text=blog_page())

        result = await core_analysis(core_client)

        changes = result["breaking_changes"]
        assert [(e["integration"], e["installed"]) for e in changes["entries"]] == [
            ("Example Hub", False),
            ("Status card", None),
            ("Calendar and Tide", True),
        ]
        assert changes["affecting_installed"] == 1

    @pytest.mark.parametrize("core_versions", [("2026.10.4", "2026.11.0")])
    async def test_an_integration_with_a_config_entry_counts_as_installed(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
    ) -> None:
        """Its entry may not be loaded (failed or disabled); it is still installed."""
        MockConfigEntry(domain="example_hub").add_to_hass(hass)
        web.listings[RELEASES] = listing("2026.11.0", "2026.10.4")
        web.serve(BLOG_202611, text=blog_page())

        result = await core_analysis(core_client)

        assert "example_hub" in result["installed_integrations"]
        [hub] = [
            e
            for e in result["breaking_changes"]["entries"]
            if e["integration"] == "Example Hub"
        ]
        assert hub["installed"] is True

    @pytest.mark.parametrize("core_versions", [("2026.10.4", "2026.11.0")])
    async def test_repairs_issues_core_says_will_break_are_listed(
        self,
        hass: HomeAssistant,
        hassio_updates: Supervisor,
        core_client: HomeAssistantClient,
        web: Web,
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
        web.listings[RELEASES] = listing("2026.11.0", "2026.10.4")

        result = await core_analysis(core_client)

        assert [
            (issue["domain"], issue["issue_id"], issue["breaks_in_ha_version"])
            for issue in result["breaking_changes"]["repairs"]
        ] == [("sun", "deprecated_yaml", "2026.12.0")]
