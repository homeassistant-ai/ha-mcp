"""Unit tests for tools_updates module."""


from ha_mcp.tools.tools_updates import (
    _categorize_update,
    _filter_alerts,
    _parse_version,
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
