"""Unit tests for the HACS refresh nudge (#1783/#1785 follow-up).

When the component detects that a newer custom component exists — the auto-update
hold or the component-outdated repair — it asks HACS to force-refresh this
component's repository so HACS surfaces the update promptly instead of waiting
out its ~48h custom-repository cache. The interaction reaches into HACS internals
and is advisory: any failure degrades to a debug log and never faults the caller.

HACS is faked in ``hass.data["hacs"]``; HACS itself is never imported. Home
Assistant is stubbed via ``_embedded_stubs``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from ._embedded_stubs import install

install()

import custom_components.ha_mcp_tools.hacs_nudge as nudge  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DOMAIN,
    HACS_LEGACY_REPO_FULL_NAME,
    HACS_MIRROR_REPO_FULL_NAME,
)


def _make_hass() -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}
    return hass


def _fake_repo(*, category: str = "integration", installed: bool = True) -> MagicMock:
    repo = MagicMock(name="repository")
    repo.update_repository = AsyncMock()
    repo.data = SimpleNamespace(category=category, installed=installed)
    return repo


def _fake_hacs(*, repos=None, category: str = "integration"):
    """A fake HACS: ``repos`` maps full_name -> repo (any other name -> None).

    ``coordinators`` is keyed by category so the update-entity listener push can
    be asserted. Returns ``(hacs, coordinator)``.
    """
    repos = repos or {}
    hacs = MagicMock(name="hacs")
    hacs.repositories.get_by_full_name = MagicMock(
        side_effect=lambda full_name: repos.get(full_name)
    )
    coordinator = MagicMock(name="coordinator")
    hacs.coordinators = {category: coordinator}
    return hacs, coordinator


class TestNudge:
    async def test_refreshes_repository_found_by_mirror_name(self):
        hass = _make_hass()
        repo = _fake_repo(category="integration")
        hacs, coordinator = _fake_hacs(
            repos={HACS_MIRROR_REPO_FULL_NAME: repo}, category="integration"
        )
        hass.data["hacs"] = hacs

        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")

        # The mirror name is tried first and its repo is force-refreshed...
        assert (
            hacs.repositories.get_by_full_name.call_args_list[0].args[0]
            == HACS_MIRROR_REPO_FULL_NAME
        )
        repo.update_repository.assert_awaited_once_with(ignore_issues=True, force=True)
        # ...then the update entity's coordinator is nudged to re-publish.
        coordinator.async_update_listeners.assert_called_once()

    async def test_falls_back_to_legacy_repo_name(self):
        hass = _make_hass()
        repo = _fake_repo()
        hacs, _ = _fake_hacs(repos={HACS_LEGACY_REPO_FULL_NAME: repo})
        hass.data["hacs"] = hacs

        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")

        # Both names were tried, in order, and the legacy repo was refreshed.
        tried = [c.args[0] for c in hacs.repositories.get_by_full_name.call_args_list]
        assert tried == [HACS_MIRROR_REPO_FULL_NAME, HACS_LEGACY_REPO_FULL_NAME]
        repo.update_repository.assert_awaited_once()

    async def test_uninstalled_mirror_falls_through_to_installed_legacy(self):
        # Legacy->mirror migration limbo: the mirror repo has been ADDED to
        # HACS (a record exists) but not installed yet, while the running
        # component is still tracked — and downloaded — under the legacy
        # record. Only the installed repo has an update entity, so that is the
        # one that must be refreshed (review finding).
        hass = _make_hass()
        mirror = _fake_repo(installed=False)
        legacy = _fake_repo()
        hacs, _ = _fake_hacs(
            repos={
                HACS_MIRROR_REPO_FULL_NAME: mirror,
                HACS_LEGACY_REPO_FULL_NAME: legacy,
            }
        )
        hass.data["hacs"] = hacs

        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")

        mirror.update_repository.assert_not_awaited()
        legacy.update_repository.assert_awaited_once_with(
            ignore_issues=True, force=True
        )
        assert "1.2.0" in hass.data[DOMAIN][nudge._DATA_HACS_NUDGED_VERSIONS]

    async def test_no_installed_candidate_is_a_clean_no_op(self):
        # A record that is added but not installed has no update entity to
        # light up — nothing to refresh, and the throttle stays unset so a
        # later pass (after an install) still gets its one refresh.
        hass = _make_hass()
        mirror = _fake_repo(installed=False)
        hacs, _ = _fake_hacs(repos={HACS_MIRROR_REPO_FULL_NAME: mirror})
        hass.data["hacs"] = hacs

        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")

        mirror.update_repository.assert_not_awaited()
        assert "1.2.0" not in hass.data.get(DOMAIN, {}).get(
            nudge._DATA_HACS_NUDGED_VERSIONS, set()
        )

    async def test_absent_hacs_is_a_clean_no_op(self):
        hass = _make_hass()  # no hass.data["hacs"]

        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")  # must not raise

        # Nothing to throttle on: a later pass (once HACS exists) still refreshes.
        assert "1.2.0" not in hass.data.get(DOMAIN, {}).get(
            nudge._DATA_HACS_NUDGED_VERSIONS, set()
        )

    async def test_repo_not_registered_is_a_clean_no_op(self):
        hass = _make_hass()
        hacs, _ = _fake_hacs(repos={})  # neither candidate resolves
        hass.data["hacs"] = hacs

        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")

        assert "1.2.0" not in hass.data.get(DOMAIN, {}).get(
            nudge._DATA_HACS_NUDGED_VERSIONS, set()
        )

    async def test_exploding_hacs_is_swallowed_and_logged(self, caplog):
        import logging

        hass = _make_hass()
        hacs = MagicMock(name="hacs")
        hacs.repositories.get_by_full_name = MagicMock(
            side_effect=AttributeError("HACS internals changed")
        )
        hass.data["hacs"] = hacs

        with caplog.at_level(logging.DEBUG):
            await nudge.async_nudge_hacs_refresh(hass, "1.2.0")  # must not raise

        assert "could not nudge HACS" in caplog.text
        # A failure leaves the throttle unset so the next pass retries.
        assert "1.2.0" not in hass.data.get(DOMAIN, {}).get(
            nudge._DATA_HACS_NUDGED_VERSIONS, set()
        )

    async def test_update_repository_failure_is_swallowed(self):
        # A network failure inside the refresh itself must degrade, not raise.
        hass = _make_hass()
        repo = _fake_repo()
        repo.update_repository = AsyncMock(side_effect=RuntimeError("github down"))
        hacs, _ = _fake_hacs(repos={HACS_MIRROR_REPO_FULL_NAME: repo})
        hass.data["hacs"] = hacs

        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")  # must not raise

        assert "1.2.0" not in hass.data.get(DOMAIN, {}).get(
            nudge._DATA_HACS_NUDGED_VERSIONS, set()
        )

    async def test_second_call_for_same_version_is_throttled(self):
        hass = _make_hass()
        repo = _fake_repo()
        hacs, _ = _fake_hacs(repos={HACS_MIRROR_REPO_FULL_NAME: repo})
        hass.data["hacs"] = hacs

        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")
        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")

        # The successful first refresh set the throttle marker; the second call
        # short-circuits before touching HACS again.
        repo.update_repository.assert_awaited_once()
        assert "1.2.0" in hass.data[DOMAIN][nudge._DATA_HACS_NUDGED_VERSIONS]

    async def test_different_version_refreshes_again(self):
        hass = _make_hass()
        repo = _fake_repo()
        hacs, _ = _fake_hacs(repos={HACS_MIRROR_REPO_FULL_NAME: repo})
        hass.data["hacs"] = hacs

        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")
        await nudge.async_nudge_hacs_refresh(hass, "1.3.0")

        # A new pending version is a new refresh; both stay throttled.
        assert repo.update_repository.await_count == 2
        assert {"1.2.0", "1.3.0"} <= hass.data[DOMAIN][nudge._DATA_HACS_NUDGED_VERSIONS]

    async def test_two_callers_with_different_versions_do_not_thrash(self):
        # The hold nudges with the shipped version while the outdated check
        # nudges with the required one; alternating passes must not defeat the
        # throttle and re-refresh every time (review finding).
        hass = _make_hass()
        repo = _fake_repo()
        hacs, _ = _fake_hacs(repos={HACS_MIRROR_REPO_FULL_NAME: repo})
        hass.data["hacs"] = hacs

        for version in ("1.2.0", "1.3.0", "1.2.0", "1.3.0"):
            await nudge.async_nudge_hacs_refresh(hass, version)

        assert repo.update_repository.await_count == 2

    async def test_absent_repositories_attribute_is_a_clean_no_op(self):
        # The docstring's "wholly different HACS shape returns False cleanly"
        # branch: hacs exists but has no repositories/get_by_full_name.
        hass = _make_hass()
        hass.data["hacs"] = SimpleNamespace()

        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")  # must not raise

        assert "1.2.0" not in hass.data.get(DOMAIN, {}).get(
            nudge._DATA_HACS_NUDGED_VERSIONS, set()
        )

    async def test_no_op_pass_then_install_retries_same_version(self):
        # The throttle's load-bearing property end to end: an uninstalled-repo
        # no-op pass leaves the version unthrottled, and a later pass for the
        # SAME version (after the install) still gets its one refresh.
        hass = _make_hass()
        repo = _fake_repo(installed=False)
        hacs, _ = _fake_hacs(repos={HACS_MIRROR_REPO_FULL_NAME: repo})
        hass.data["hacs"] = hacs

        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")
        repo.update_repository.assert_not_awaited()

        repo.data.installed = True
        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")

        repo.update_repository.assert_awaited_once()
        assert "1.2.0" in hass.data[DOMAIN][nudge._DATA_HACS_NUDGED_VERSIONS]

    async def test_listener_push_failure_still_counts_as_refreshed(self):
        # The network refresh completed; a HACS shape change in the bonus
        # listener push must not void the throttle and re-run the fetch every
        # pass (review finding).
        hass = _make_hass()
        repo = _fake_repo(category="integration")
        hacs, coordinator = _fake_hacs(
            repos={HACS_MIRROR_REPO_FULL_NAME: repo}, category="integration"
        )
        coordinator.async_update_listeners = MagicMock(
            side_effect=RuntimeError("HACS internals changed")
        )
        hass.data["hacs"] = hacs

        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")  # must not raise

        repo.update_repository.assert_awaited_once()
        assert "1.2.0" in hass.data[DOMAIN][nudge._DATA_HACS_NUDGED_VERSIONS]

    async def test_missing_coordinator_still_refreshes(self):
        # The listener push is a bonus; a HACS with no coordinator for the
        # category must not turn the completed refresh into a failure.
        hass = _make_hass()
        repo = _fake_repo(category="integration")
        hacs = MagicMock(name="hacs")
        hacs.repositories.get_by_full_name = MagicMock(
            side_effect=lambda full_name: (
                repo if full_name == HACS_MIRROR_REPO_FULL_NAME else None
            )
        )
        hacs.coordinators = {}  # no coordinator for this category
        hass.data["hacs"] = hacs

        await nudge.async_nudge_hacs_refresh(hass, "1.2.0")

        repo.update_repository.assert_awaited_once()
        # The refresh completed, so it is throttled.
        assert "1.2.0" in hass.data[DOMAIN][nudge._DATA_HACS_NUDGED_VERSIONS]


class TestSchedule:
    def test_schedule_creates_named_background_task(self):
        hass = MagicMock(name="hass")
        captured = {}

        def _create_task(coro, name=None):
            captured["name"] = name
            # Close the real coroutine so it is not left un-awaited in this
            # scheduling-only test.
            coro.close()

        hass.async_create_task = MagicMock(side_effect=_create_task)

        nudge.async_schedule_hacs_nudge(hass, "1.2.0")

        hass.async_create_task.assert_called_once()
        assert captured["name"] == f"{DOMAIN}_hacs_nudge"
