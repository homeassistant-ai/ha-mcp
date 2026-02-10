"""Unit tests for tools_updates module."""


from ha_mcp.tools.tools_updates import (
    _categorize_update,
    _filter_alerts,
    _get_monthly_versions_between,
    _parse_breaking_changes_html,
    _parse_patch_breaking_changes,
    _parse_version,
    _strip_html,
    _supports_release_notes,
)


class TestCategorizeUpdate:
    """Test _categorize_update function."""

    def test_core_update_by_entity_id(self):
        """Core updates are identified by entity_id."""
        result = _categorize_update("update.home_assistant_core_update", {})
        assert result == "core"

    def test_core_without_home_assistant_in_title(self):
        """Entity with 'core' but without 'home_assistant' in title is not categorized as core."""
        # The logic requires BOTH 'core' in entity_id AND 'home_assistant' in title
        # Note: 'home_assistant' (with underscore) must be present, not 'Home Assistant' (with space)
        result = _categorize_update(
            "update.some_core_entity", {"title": "Home Assistant Core Update"}
        )
        # This is 'other' because 'home_assistant' (underscore) is not in 'home assistant core update'
        assert result == "other"

    def test_os_update(self):
        """OS updates are identified correctly."""
        result = _categorize_update("update.home_assistant_operating_system", {})
        assert result == "os"

    def test_supervisor_update(self):
        """Supervisor updates are identified correctly."""
        result = _categorize_update("update.home_assistant_supervisor_update", {})
        assert result == "supervisor"

    def test_hacs_update(self):
        """HACS updates are identified correctly."""
        result = _categorize_update("update.hacs_some_integration", {})
        assert result == "hacs"

    def test_addon_update_by_title(self):
        """Add-on updates are identified by title."""
        result = _categorize_update(
            "update.some_addon_update", {"title": "Some Add-on"}
        )
        assert result == "addons"

    def test_device_firmware_esphome(self):
        """ESPHome device updates are categorized as devices."""
        result = _categorize_update("update.esphome_device_firmware", {})
        assert result == "devices"

    def test_device_firmware_by_title(self):
        """Device firmware updates are identified by title containing firmware."""
        result = _categorize_update(
            "update.slzb_06m_core", {"title": "SLZB-06M Core firmware"}
        )
        assert result == "devices"

    def test_other_update(self):
        """Unknown updates are categorized as other."""
        result = _categorize_update("update.unknown_thing", {"title": "Unknown"})
        assert result == "other"

    def test_none_title_does_not_raise(self):
        """Title attribute being None should not raise an error.

        This test verifies the fix for issue #185 where update entities
        with None values for title would cause:
        'NoneType' object has no attribute 'lower'
        """
        # This should not raise AttributeError
        result = _categorize_update("update.some_entity", {"title": None})
        # Without a title, it should fall through to "other"
        assert result == "other"

    def test_missing_title_does_not_raise(self):
        """Missing title attribute should not raise an error."""
        result = _categorize_update("update.some_entity", {})
        assert result == "other"

    def test_none_title_with_entity_match(self):
        """Entity ID matching should still work even with None title."""
        result = _categorize_update(
            "update.home_assistant_core_update", {"title": None}
        )
        assert result == "core"


class TestSupportsReleaseNotes:
    """Test _supports_release_notes function."""

    def test_feature_flag_set(self):
        """Returns True when release notes feature flag (16) is set."""
        # Feature flag 16 = 0x10 = release notes support
        result = _supports_release_notes(
            "update.test", {"supported_features": 16}
        )
        assert result is True

    def test_release_url_present(self):
        """Returns True when release_url is present."""
        result = _supports_release_notes(
            "update.test",
            {"release_url": "https://github.com/test/repo/releases/tag/v1.0"},
        )
        assert result is True

    def test_both_present(self):
        """Returns True when both feature flag and release_url are present."""
        result = _supports_release_notes(
            "update.test",
            {
                "supported_features": 16,
                "release_url": "https://github.com/test/repo/releases/tag/v1.0",
            },
        )
        assert result is True

    def test_neither_present(self):
        """Returns False when neither feature flag nor release_url is present."""
        result = _supports_release_notes("update.test", {})
        assert result is False

    def test_other_features_only(self):
        """Returns False when only other feature flags are set (not 16)."""
        # Features 1=install, 2=specific_version, 4=progress, 8=backup
        result = _supports_release_notes(
            "update.test", {"supported_features": 15}  # 1+2+4+8
        )
        assert result is False


class TestParseVersion:
    """Test _parse_version function."""

    def test_standard_ha_version(self):
        """Parses standard HA version strings."""
        assert _parse_version("2025.11.3") == (2025, 11, 3)

    def test_major_minor_only(self):
        """Parses version with only major.minor."""
        assert _parse_version("2025.11") == (2025, 11)

    def test_single_number(self):
        """Parses single number version."""
        assert _parse_version("5") == (5,)

    def test_empty_string(self):
        """Returns None for empty string."""
        assert _parse_version("") is None

    def test_non_numeric(self):
        """Returns None for non-numeric version."""
        assert _parse_version("beta") is None

    def test_mixed_non_numeric(self):
        """Returns None for partially non-numeric version."""
        assert _parse_version("2025.11.beta") is None

    def test_version_comparison(self):
        """Parsed versions compare correctly."""
        v1 = _parse_version("2025.10.0")
        v2 = _parse_version("2025.11.0")
        assert v1 is not None
        assert v2 is not None
        assert v1 < v2

    def test_patch_comparison(self):
        """Patch versions compare correctly."""
        v1 = _parse_version("2025.11.1")
        v2 = _parse_version("2025.11.3")
        assert v1 is not None
        assert v2 is not None
        assert v1 < v2


class TestGetMonthlyVersionsBetween:
    """Test _get_monthly_versions_between function."""

    def test_single_month_gap(self):
        """Single month upgrade produces one version."""
        result = _get_monthly_versions_between("2025.11.3", "2025.12.0")
        assert result == ["2025.12.0"]

    def test_multi_month_gap(self):
        """Multi-month gap produces all intermediate .0 versions."""
        result = _get_monthly_versions_between("2025.10.3", "2026.2.1")
        assert result == [
            "2025.11.0",
            "2025.12.0",
            "2026.1.0",
            "2026.2.0",
        ]

    def test_year_boundary(self):
        """Correctly handles year boundary crossing."""
        result = _get_monthly_versions_between("2025.11.0", "2026.1.0")
        assert result == ["2025.12.0", "2026.1.0"]

    def test_same_month_patch_update(self):
        """Patch update within same month returns empty list."""
        result = _get_monthly_versions_between("2025.11.0", "2025.11.3")
        assert result == []

    def test_same_version(self):
        """Same version returns empty list."""
        result = _get_monthly_versions_between("2025.11.0", "2025.11.0")
        assert result == []

    def test_invalid_versions(self):
        """Invalid versions still return a reasonable result."""
        result = _get_monthly_versions_between("bad", "2025.11.0")
        assert result == ["2025.11.0"]

    def test_single_part_versions(self):
        """Versions with less than 2 parts use target as fallback."""
        result = _get_monthly_versions_between("5", "2025.11.0")
        assert result == ["2025.11.0"]


class TestStripHtml:
    """Test _strip_html function."""

    def test_removes_tags(self):
        """Strips HTML tags."""
        assert _strip_html("<b>bold</b> text") == "bold text"

    def test_preserves_paragraphs(self):
        """Converts paragraph tags to newlines."""
        result = _strip_html("<p>First</p><p>Second</p>")
        assert "First" in result
        assert "Second" in result
        assert "\n" in result

    def test_converts_list_items(self):
        """Converts li tags to bullet points."""
        result = _strip_html("<ul><li>One</li><li>Two</li></ul>")
        assert "- One" in result
        assert "- Two" in result

    def test_empty_string(self):
        """Empty string returns empty."""
        assert _strip_html("") == ""

    def test_no_html(self):
        """Plain text passes through unchanged."""
        assert _strip_html("just plain text") == "just plain text"


class TestParseBreakingChangesHtml:
    """Test _parse_breaking_changes_html function."""

    SAMPLE_HTML = """
    <h2 id="backward-incompatible-changes">Backward-incompatible changes</h2>
    <h3>Tuya</h3>
    <p>Duplicate HVACMode have been converted to presets.</p>
    <p>(<a href="#">@contributor</a> - <a href="#">#12345</a>)
    (<a href="/integrations/tuya/">tuya documentation</a>)</p>
    <h3>Group</h3>
    <p>The behavior of sensor groups has changed.</p>
    <p>(<a href="#">@contributor</a> - <a href="#">#67890</a>)
    (<a href="/integrations/group/">group documentation</a>)</p>
    <h2 id="all-changes">All changes</h2>
    """

    def test_parses_entries(self):
        """Extracts breaking change entries from blog HTML."""
        result = _parse_breaking_changes_html(self.SAMPLE_HTML, "https://example.com")
        assert result is not None
        assert result["count"] == 2
        assert result["source_url"] == "https://example.com"

    def test_entry_integration_names(self):
        """Extracts correct integration names."""
        result = _parse_breaking_changes_html(self.SAMPLE_HTML, "https://example.com")
        assert result is not None
        names = [e["integration"] for e in result["entries"]]
        assert "Tuya" in names
        assert "Group" in names

    def test_entry_descriptions(self):
        """Extracts entry descriptions."""
        result = _parse_breaking_changes_html(self.SAMPLE_HTML, "https://example.com")
        assert result is not None
        tuya_entry = next(e for e in result["entries"] if e["integration"] == "Tuya")
        assert "HVACMode" in tuya_entry["description"]

    def test_no_breaking_changes_section(self):
        """Returns None when section is not found."""
        html = "<h2 id='other'>Other stuff</h2><p>Nothing here</p>"
        result = _parse_breaking_changes_html(html, "https://example.com")
        assert result is None

    def test_empty_breaking_changes_section(self):
        """Returns None when section exists but is empty."""
        html = (
            '<h2 id="backward-incompatible-changes">Backward-incompatible changes</h2>'
            '<h2 id="next">Next</h2>'
        )
        result = _parse_breaking_changes_html(html, "https://example.com")
        assert result is None

    def test_section_with_details_elements_fallback(self):
        """Falls back to raw text when h3 entries aren't found."""
        html = (
            '<h2 id="backward-incompatible-changes">Backward-incompatible changes</h2>'
            "<p>Some breaking change without h3 structure.</p>"
            '<h2 id="next">Next</h2>'
        )
        result = _parse_breaking_changes_html(html, "https://example.com")
        assert result is not None
        assert result["count"] == 0
        assert "breaking change" in result["raw_text"]


class TestParsePatchBreakingChanges:
    """Test _parse_patch_breaking_changes function."""

    def test_parses_breaking_change_items(self):
        """Extracts items tagged with (breaking-change)."""
        body = (
            "## Changelog\n"
            "- Fix something normal (@user) ([hue docs])\n"
            "- Fix redundant off preset in Tuya climate (@user) ([tuya docs]) (breaking-change)\n"
            "- Another normal fix\n"
        )
        result = _parse_patch_breaking_changes(body, "2025.11.1")
        assert result is not None
        assert result["count"] == 1
        assert result["entries"][0]["integration"] == "tuya"

    def test_no_breaking_changes(self):
        """Returns None when no (breaking-change) items found."""
        body = "## Changelog\n- Normal fix (@user)\n- Another fix\n"
        result = _parse_patch_breaking_changes(body, "2025.11.1")
        assert result is None

    def test_extracts_integration_from_docs_link(self):
        """Extracts integration name from [name docs] pattern."""
        body = "- Fix stuff in VeSync (@user) ([vesync documentation]) (breaking-change)\n"
        result = _parse_patch_breaking_changes(body, "2025.11.2")
        assert result is not None
        assert result["entries"][0]["integration"] == "vesync"

    def test_unknown_integration_when_no_docs_link(self):
        """Uses 'unknown' when no docs link found."""
        body = "- Some breaking change (@user) (breaking-change)\n"
        result = _parse_patch_breaking_changes(body, "2025.11.2")
        assert result is not None
        assert result["entries"][0]["integration"] == "unknown"

    def test_source_url(self):
        """Sets source_url to GitHub release page."""
        body = "- Fix (@user) ([hue docs]) (breaking-change)\n"
        result = _parse_patch_breaking_changes(body, "2025.11.1")
        assert result is not None
        assert "2025.11.1" in result["source_url"]

    def test_case_insensitive(self):
        """Handles case variations in (breaking-change) tag."""
        body = "- Fix stuff (@user) ([hue docs]) (Breaking-Change)\n"
        result = _parse_patch_breaking_changes(body, "2025.11.1")
        assert result is not None
        assert result["count"] == 1


class TestFilterAlerts:
    """Test _filter_alerts function."""

    def _make_alert(
        self,
        alert_id: str = "test_alert",
        title: str = "Test Alert",
        integrations: list[str] | None = None,
        affected_from: str | None = None,
        resolved_in: str | None = None,
    ) -> dict:
        """Create a test alert dict."""
        alert: dict = {
            "id": alert_id,
            "title": title,
            "created": "2025-01-01T00:00:00.000Z",
            "integrations": [{"package": d} for d in (integrations or [])],
            "alert_url": f"https://alerts.home-assistant.io/alerts/{alert_id}/",
        }
        ha_info: dict = {}
        if affected_from:
            ha_info["affected_from_version"] = affected_from
        if resolved_in:
            ha_info["resolved_in_version"] = resolved_in
        if ha_info:
            alert["homeassistant"] = ha_info
        return alert

    def test_relevant_alert_matching_integration(self):
        """Alert matching an installed integration is relevant."""
        alerts = [self._make_alert(integrations=["zwave_js"])]
        result = _filter_alerts(alerts, None, None, {"zwave_js", "hue"})

        assert len(result["relevant"]) == 1
        assert len(result["other"]) == 0
        assert result["relevant"][0]["matched_integrations"] == ["zwave_js"]

    def test_other_alert_no_matching_integration(self):
        """Alert not matching any installed integration goes to other."""
        alerts = [self._make_alert(integrations=["devolo_home_control"])]
        result = _filter_alerts(alerts, None, None, {"zwave_js", "hue"})

        assert len(result["relevant"]) == 0
        assert len(result["other"]) == 1

    def test_version_range_filter_future_alert(self):
        """Alert affecting only future versions is excluded."""
        alerts = [
            self._make_alert(
                integrations=["hue"],
                affected_from="2026.1.0",
            )
        ]
        # Target is 2025.12.0, alert starts at 2026.1.0 → excluded
        result = _filter_alerts(
            alerts,
            (2025, 11, 0),
            (2025, 12, 0),
            {"hue"},
        )
        assert len(result["relevant"]) == 0
        assert len(result["other"]) == 0

    def test_version_range_filter_resolved_alert(self):
        """Alert resolved before current version is excluded."""
        alerts = [
            self._make_alert(
                integrations=["hue"],
                affected_from="2025.1.0",
                resolved_in="2025.10.0",
            )
        ]
        # Current is 2025.11.0, alert resolved at 2025.10.0 → excluded
        result = _filter_alerts(
            alerts,
            (2025, 11, 0),
            (2025, 12, 0),
            {"hue"},
        )
        assert len(result["relevant"]) == 0
        assert len(result["other"]) == 0

    def test_alert_within_version_range(self):
        """Alert within update version range is included."""
        alerts = [
            self._make_alert(
                integrations=["mqtt"],
                affected_from="2025.10.0",
            )
        ]
        result = _filter_alerts(
            alerts,
            (2025, 9, 0),
            (2025, 11, 0),
            {"mqtt"},
        )
        assert len(result["relevant"]) == 1

    def test_empty_alerts_list(self):
        """Empty alerts list returns empty results."""
        result = _filter_alerts([], (2025, 11, 0), (2025, 12, 0), {"hue"})
        assert result["relevant"] == []
        assert result["other"] == []

    def test_empty_installed_domains(self):
        """All alerts go to other when no integrations installed."""
        alerts = [self._make_alert(integrations=["hue"])]
        result = _filter_alerts(alerts, None, None, set())

        assert len(result["relevant"]) == 0
        assert len(result["other"]) == 1

    def test_multiple_integrations_partial_match(self):
        """Alert with multiple integrations matches if any is installed."""
        alerts = [self._make_alert(integrations=["hue", "zwave_js", "mqtt"])]
        result = _filter_alerts(alerts, None, None, {"mqtt"})

        assert len(result["relevant"]) == 1
        assert result["relevant"][0]["matched_integrations"] == ["mqtt"]

    def test_no_version_info_alert_included(self):
        """Alerts without version info are always included."""
        alerts = [self._make_alert(integrations=["hue"])]
        result = _filter_alerts(
            alerts,
            (2025, 11, 0),
            (2025, 12, 0),
            {"hue"},
        )
        assert len(result["relevant"]) == 1

    def test_uses_min_field_as_fallback(self):
        """Falls back to 'min' field when 'affected_from_version' is absent."""
        alert = self._make_alert(integrations=["hue"])
        alert["homeassistant"] = {"min": "2026.1.0"}  # future version

        result = _filter_alerts(
            [alert],
            (2025, 11, 0),
            (2025, 12, 0),
            {"hue"},
        )
        assert len(result["relevant"]) == 0  # excluded: future alert
