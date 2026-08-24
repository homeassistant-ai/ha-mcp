"""
E2E tests for integration management tools.
"""

import json
import logging

import pytest

from ...utilities.assertions import (
    MCPAssertions,
    assert_mcp_success,
    safe_call_tool,
)
from ...utilities.wait_helpers import wait_for_tool_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.integrations
class TestIntegrationManagement:
    """Test integration enable/disable/delete operations."""

    async def test_set_integration_enabled_cycle(self, mcp_client):
        """Test full enable/disable/re-enable cycle."""
        # Find suitable integration (supports_unload=True)
        list_result = await mcp_client.call_tool("ha_get_integration", {})
        data = assert_mcp_success(list_result, "List integrations")

        # Find test integration
        test_entry = None
        for entry in data.get("entries", []):
            if entry.get("supports_unload") and entry.get("state") == "loaded":
                test_entry = entry
                break

        if not test_entry:
            pytest.skip("No suitable integration found for testing")

        entry_id = test_entry["entry_id"]
        logger.info(f"Testing with integration: {test_entry['title']}")

        # DISABLE
        disable_result = await mcp_client.call_tool(
            "ha_set_integration", {"entry_id": entry_id, "enabled": False}
        )
        assert_mcp_success(disable_result, "Disable integration")

        # Verify disabled
        list_result = await mcp_client.call_tool(
            "ha_get_integration", {"query": test_entry["domain"]}
        )
        data = assert_mcp_success(list_result, "List after disable")
        entry = next(e for e in data["entries"] if e["entry_id"] == entry_id)
        assert entry["disabled_by"] == "user", "Integration should be disabled by user"

        # RE-ENABLE
        enable_result = await mcp_client.call_tool(
            "ha_set_integration", {"entry_id": entry_id, "enabled": True}
        )
        assert_mcp_success(enable_result, "Re-enable integration")

        # Verify re-enabled
        list_result = await mcp_client.call_tool(
            "ha_get_integration", {"query": test_entry["domain"]}
        )
        data = assert_mcp_success(list_result, "List after enable")
        entry = next(e for e in data["entries"] if e["entry_id"] == entry_id)
        assert entry["disabled_by"] is None, (
            "Integration should not be disabled after re-enable"
        )

    async def test_delete_config_entry_requires_confirm(self, mcp_client):
        """Test deletion safety check."""
        data = await safe_call_tool(
            mcp_client,
            "ha_remove_helpers_integrations",
            {"target": "fake_id", "confirm": False},
        )
        assert not data.get("success"), "Delete without confirm should fail"
        error = data.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        assert "not confirmed" in error_msg.lower()

    async def test_delete_config_entry_create_delete_cycle(self, mcp_client):
        """Test full create → verify → delete → verify-gone cycle.

        Regression test: the config-entry delete path previously used the WebSocket
        command ``config_entries/delete`` which HA does not support, returning
        "Unknown command".  The fix switches to the REST API endpoint.
        """
        # Create a temporary light group helper
        config = {
            "group_type": "light",
            "name": "test_delete_regression_e2e",
            "entities": [],
            "hide_members": False,
        }

        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "group",
                "name": "test_delete_regression_e2e",
                "config": config,
            },
        )
        data = assert_mcp_success(create_result, "Create light group for delete test")
        entry_id = data["entry_id"]
        logger.info(f"Created temporary group helper: {entry_id}")

        # Wait until the entry is registered
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_integration",
            arguments={"entry_id": entry_id},
            predicate=lambda d: d.get("success") is True,
            description="group helper is registered",
        )

        # Delete the entry
        delete_result = await mcp_client.call_tool(
            "ha_remove_helpers_integrations",
            {"target": entry_id, "confirm": True},
        )
        delete_data = assert_mcp_success(delete_result, "Delete config entry")
        assert delete_data.get("success") is True
        assert delete_data.get("entry_id") == entry_id
        logger.info(f"Deleted config entry: {entry_id}")

        # Verify the entry is gone
        verify_data = await safe_call_tool(
            mcp_client,
            "ha_get_integration",
            {"entry_id": entry_id},
        )
        assert not verify_data.get("success", False), (
            f"Config entry {entry_id} should not exist after deletion"
        )

    async def test_set_integration_enabled_nonexistent(self, mcp_client):
        """Test error handling for non-existent integration."""
        data = await safe_call_tool(
            mcp_client,
            "ha_set_integration",
            {"entry_id": "nonexistent_entry_id", "enabled": True},
        )
        # Should fail - either through validation or API error
        assert not data.get("success", False)

    async def test_add_integration_and_update_options_cycle(self, mcp_client):
        """Add-mode + options-mode round-trip via ha_set_integration (#1814).

        Uses the ``group`` domain because it is always present in the test
        container and its config flow exercises both a menu step
        (``group_type``) and a form step through the generic (non-helper)
        driver. The mechanics are identical for any integration domain —
        unlike ha_config_set_helper, the tool does not gate the handler on
        the helper allowlist.
        """
        # ADD: drive the config flow (menu -> form -> create_entry)
        create_result = await mcp_client.call_tool(
            "ha_set_integration",
            {
                "domain": "group",
                "config": {
                    "group_type": "light",
                    "name": "test_set_integration_add_e2e",
                    "entities": [],
                    "hide_members": False,
                },
            },
        )
        data = assert_mcp_success(create_result, "Add integration via config flow")
        entry_id = data["entry_id"]
        assert data["domain"] == "group"

        try:
            # Wait until the entry is registered
            await wait_for_tool_result(
                mcp_client,
                tool_name="ha_get_integration",
                arguments={"entry_id": entry_id},
                predicate=lambda d: d.get("success") is True,
                description="added integration entry is registered",
            )

            # UPDATE OPTIONS: drive the options flow on the same entry
            update_result = await mcp_client.call_tool(
                "ha_set_integration",
                {
                    "entry_id": entry_id,
                    "config": {"entities": [], "hide_members": True},
                },
            )
            update_data = assert_mcp_success(
                update_result, "Update integration options via options flow"
            )
            assert update_data.get("updated") is True
            assert update_data.get("entry_id") == entry_id

            # Verify the option persisted (single-entry mode probes options)
            verify_data = await wait_for_tool_result(
                mcp_client,
                tool_name="ha_get_integration",
                arguments={"entry_id": entry_id},
                predicate=lambda d: (
                    d.get("entry", {}).get("options", {}).get("hide_members") is True
                ),
                description="updated option is readable back",
            )
            assert verify_data["entry"]["options"]["hide_members"] is True
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_remove_helpers_integrations",
                {"target": entry_id, "confirm": True},
            )

    async def test_partial_options_submit_keeps_unnamed_fields(self, mcp_client):
        """A one-field options patch must not reset the fields it omits (#2254).

        The bug: the walker submitted ONLY caller-named keys, so every field
        the caller left out went back to whatever voluptuous substitutes for an
        absent key — its static schema default — while the tool still reported
        success. The sibling test above never caught it because ``group``'s
        light options are all ``vol.Required``, and required fields were
        already backfilled from the step's own suggestion.

        ``group`` with ``group_type="sensor"`` is the reachable repro: its
        options schema extends the basic one with
        ``vol.Optional(CONF_IGNORE_NON_NUMERIC, default=False)``, an OPTIONAL
        field carrying a static default. Set it true, patch a different field,
        and pre-fix it silently fell back to false. Deliberately an e2e rather
        than a unit test: the unit suite pins the payload against a
        hand-written copy of HA's schema serialization, so it would keep
        passing if HA changed how it serializes ``suggested_value``. Only a
        real options flow proves the values actually survive the round trip.
        """
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_set_integration",
                {
                    "domain": "group",
                    "config": {
                        "group_type": "sensor",
                        "name": "test_partial_options_2254_e2e",
                        "entities": [],
                        "hide_members": False,
                        "type": "max",
                        "ignore_non_numeric": True,
                    },
                },
            )
            entry_id = data["entry_id"]

            try:
                await wait_for_tool_result(
                    mcp_client,
                    tool_name="ha_get_integration",
                    arguments={"entry_id": entry_id},
                    predicate=lambda d: (
                        d.get("entry", {}).get("options", {}).get("ignore_non_numeric")
                        is True
                    ),
                    description="baseline option is set before the partial patch",
                )

                # The patch under test: name ONLY 'type'. Every other field in
                # the step is left out, which is what used to reset them.
                update_data = await mcp.call_tool_success(
                    "ha_set_integration",
                    {"entry_id": entry_id, "config": {"type": "min"}},
                )
                assert update_data.get("updated") is True

                verify_data = await wait_for_tool_result(
                    mcp_client,
                    tool_name="ha_get_integration",
                    arguments={"entry_id": entry_id},
                    predicate=lambda d: (
                        d.get("entry", {}).get("options", {}).get("type") == "min"
                    ),
                    description="the patched field took effect",
                )
                options = verify_data["entry"]["options"]

                # The regression assert: pre-#2254 this was False, silently
                # reset from the static schema default because the key was
                # omitted.
                assert options.get("ignore_non_numeric") is True, (
                    "Partial options submit reset 'ignore_non_numeric' to its "
                    "schema default — the #2254 wipe is back. Fields the "
                    "caller never named must survive the patch. Got options: "
                    f"{options}"
                )
                assert options.get("type") == "min"
            finally:
                await safe_call_tool(
                    mcp_client,
                    "ha_remove_helpers_integrations",
                    {"target": entry_id, "confirm": True},
                )

    async def test_add_integration_unknown_domain_fails(self, mcp_client):
        """Add mode surfaces a structured error for an unknown domain."""
        data = await safe_call_tool(
            mcp_client,
            "ha_set_integration",
            {"domain": "definitely_not_a_real_domain_xyz"},
        )
        assert not data.get("success", False)

    async def test_update_options_unsupported_entry_fails(self, mcp_client):
        """Options mode surfaces a structured error when the entry has no
        options flow (supports_options=false)."""
        list_result = await mcp_client.call_tool("ha_get_integration", {})
        data = assert_mcp_success(list_result, "List integrations")
        no_options_entry = next(
            (e for e in data.get("entries", []) if not e.get("supports_options")),
            None,
        )
        if no_options_entry is None:
            pytest.skip("No integration without options flow found")

        result = await safe_call_tool(
            mcp_client,
            "ha_set_integration",
            {
                "entry_id": no_options_entry["entry_id"],
                "config": {"anything": True},
            },
        )
        assert not result.get("success", False)

    async def test_delete_config_entry_nonexistent_raises(self, mcp_client):
        """
        Pin the missing-target contract for the Path 3 (direct config
        entry) branch: confirmed deletion of an entry that does not
        exist raises RESOURCE_NOT_FOUND so a typo'd entry_id surfaces
        at the caller layer instead of being silently masked as success.

        Source path: confirm_bool=True bypasses the confirm guard;
        delete_config_entry() reaches the HA REST API which returns 404
        (HomeAssistantAPIError); _delete_direct_entry catches the 404
        and raises RESOURCE_NOT_FOUND. Non-404 API errors surface as
        different structured tool errors via exception_to_structured_error.
        """
        data = await safe_call_tool(
            mcp_client,
            "ha_remove_helpers_integrations",
            {"target": "nonexistent_entry_a7_e2e_xyz", "confirm": True},
        )
        assert data.get("success") is False, (
            f"Expected raise for nonexistent entry_id, got: {data}"
        )
        assert data.get("error", {}).get("code") == "RESOURCE_NOT_FOUND", (
            f"Expected RESOURCE_NOT_FOUND, got: {data!r}"
        )
        assert "already_deleted" not in json.dumps(data), (
            f"Stale already_deleted marker leaked into error: {data!r}"
        )

    # The fixture seeds a `filesize` entry (see
    # tests/initial_test_state/.storage/core.config_entries). It is the only
    # integration in the harness that implements async_step_reconfigure, and it
    # commits through async_update_reload_and_abort — the same HA call whose
    # scheduled reload the verification retry loop has to outlast — so the
    # confirmed path gets real CI coverage instead of skipping forever.
    RECONFIGURE_ENTRY_ID = "01KRECONFIGUREE2E000000001"
    RECONFIGURE_PATH_A = "/config/www/filesize_e2e_a.txt"
    RECONFIGURE_PATH_B = "/config/www/filesize_e2e_b.txt"

    async def test_reconfigure_target_advertises_supports_reconfigure(self, mcp_client):
        """ha_get_integration exposes the discovery signal for reconfigure mode."""
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_get_integration", {"entry_id": self.RECONFIGURE_ENTRY_ID}
            )

        entry = data.get("entry", data)
        assert entry.get("domain") == "filesize", entry
        assert entry.get("supports_reconfigure") is True, entry

    async def test_reconfigure_preflight_is_read_only(self, mcp_client):
        """The preflight validates and issues a token without touching the entry."""
        async with MCPAssertions(mcp_client) as mcp:
            before = await mcp.call_tool_success(
                "ha_get_integration", {"entry_id": self.RECONFIGURE_ENTRY_ID}
            )
            # Preview only — nothing is submitted to HA, so the target path
            # does not have to differ from the current one here.
            result = await mcp.call_tool_success(
                "ha_set_integration",
                {
                    "entry_id": self.RECONFIGURE_ENTRY_ID,
                    "reconfigure": True,
                    "config": {"file_path": self.RECONFIGURE_PATH_B},
                },
            )

            assert result.get("status") == "preview", result
            assert "preview" not in result, result
            assert result.get("operation") == "reconfigure", result
            confirm_token = result.get("confirm_token")
            assert isinstance(confirm_token, str) and confirm_token.startswith(
                "sha256:"
            ), result

            after = await mcp.call_tool_success(
                "ha_get_integration", {"entry_id": self.RECONFIGURE_ENTRY_ID}
            )

        before_entry = before.get("entry", before)
        after_entry = after.get("entry", after)
        stable_fields = (
            "entry_id",
            "domain",
            "unique_id",
            "title",
            "state",
            "disabled_by",
        )
        assert {field: after_entry.get(field) for field in stable_fields} == {
            field: before_entry.get(field) for field in stable_fields
        }

    async def test_reconfigure_rejects_a_stale_token(self, mcp_client):
        """A token issued for a different target config cannot be replayed."""
        async with MCPAssertions(mcp_client) as mcp:
            preview = await mcp.call_tool_success(
                "ha_set_integration",
                {
                    "entry_id": self.RECONFIGURE_ENTRY_ID,
                    "reconfigure": True,
                    "config": {"file_path": self.RECONFIGURE_PATH_B},
                },
            )
            stale = await mcp.call_tool_failure(
                "ha_set_integration",
                {
                    "entry_id": self.RECONFIGURE_ENTRY_ID,
                    "reconfigure": True,
                    # A different target config than the token was issued for.
                    "config": {"file_path": self.RECONFIGURE_PATH_A},
                    "confirm_token": preview["confirm_token"],
                },
            )

        assert stale.get("status") == "stale_preflight", stale
        assert "confirm_token" not in stale, stale

    async def test_reconfigure_confirmed_applies_and_verifies(self, mcp_client):
        """The confirmed path drives HA's real reconfigure flow end to end.

        Targets whichever of the two paths the entry is NOT currently on, and
        restores the starting one in a `finally`. filesize's reconfigure step
        calls `_abort_if_unique_id_configured()`, which does not exclude the
        entry being reconfigured (core `config_entries.py`), so reconfiguring
        to the path it already holds aborts with `already_configured` — this
        test must not assume a starting path or an ordering.
        """
        async with MCPAssertions(mcp_client) as mcp:
            current = await mcp.call_tool_success(
                "ha_get_integration", {"entry_id": self.RECONFIGURE_ENTRY_ID}
            )
            # `title` is the observable: filesize names the entry after the
            # file's basename and retitles it on reconfigure. The entry's
            # unique_id would be the more direct signal, but Home Assistant
            # does not expose it — `ConfigEntry.as_json_fragment` omits it, and
            # every config-entry endpoint serializes through that fragment.
            start_title = (current.get("entry", current) or {}).get("title")
            target = (
                self.RECONFIGURE_PATH_A
                if start_title == self.RECONFIGURE_PATH_B.rsplit("/", 1)[-1]
                else self.RECONFIGURE_PATH_B
            )
            start_path = (
                self.RECONFIGURE_PATH_B
                if target == self.RECONFIGURE_PATH_A
                else self.RECONFIGURE_PATH_A
            )
            try:
                preview = await mcp.call_tool_success(
                    "ha_set_integration",
                    {
                        "entry_id": self.RECONFIGURE_ENTRY_ID,
                        "reconfigure": True,
                        "config": {"file_path": target},
                    },
                )
                result = await mcp.call_tool_success(
                    "ha_set_integration",
                    {
                        "entry_id": self.RECONFIGURE_ENTRY_ID,
                        "reconfigure": True,
                        "config": {"file_path": target},
                        "confirm_token": preview["confirm_token"],
                    },
                )

                assert result.get("operation") == "reconfigure", result
                # The entry reloads cleanly and keeps its identity, so this is
                # the fully verified outcome — not the degraded one the reload
                # race used to produce for a reconfigure that worked.
                assert result.get("status") == "applied_and_verified", result
                verification = result.get("verification", {})
                assert verification.get("entry_state") == "loaded", result
                assert verification.get("operational_state_verified") is True, result
                assert verification.get("identity_verification") == "complete", result
                # Against a real core the change stream must be what answered.
                # Every unit fixture builds its own frames, so a parser that
                # rejects the shape Home Assistant actually sends
                # (`as_json_fragment` emits `modified_at.timestamp()`, a float)
                # degrades every reconfigure to polling with the suite still
                # green. This is the only assertion that sees the real shape.
                assert verification.get("operational_state_source") == "observed", (
                    result
                )
                # filesize keys its entry on the file path, so a path change
                # re-keys the unique_id through
                # async_update_reload_and_abort(unique_id=...). That is a
                # legitimate re-key, reported rather than refused — and it is
                # only observable because the harness installs ha_mcp_tools
                # (conftest copies the component in), which is the sole source
                # of a config entry's unique_id.
                assert (
                    verification.get("unique_id_verification")
                    == "changed_during_change"
                ), result

                changed = await mcp.call_tool_success(
                    "ha_get_integration", {"entry_id": self.RECONFIGURE_ENTRY_ID}
                )
                changed_entry = changed.get("entry", changed)
                assert changed_entry.get("title") == target.rsplit("/", 1)[-1], (
                    changed_entry
                )
            finally:
                if start_path != target:
                    restore_preview = await mcp.call_tool_success(
                        "ha_set_integration",
                        {
                            "entry_id": self.RECONFIGURE_ENTRY_ID,
                            "reconfigure": True,
                            "config": {"file_path": start_path},
                        },
                    )
                    await mcp.call_tool_success(
                        "ha_set_integration",
                        {
                            "entry_id": self.RECONFIGURE_ENTRY_ID,
                            "reconfigure": True,
                            "config": {"file_path": start_path},
                            "confirm_token": restore_preview["confirm_token"],
                        },
                    )
