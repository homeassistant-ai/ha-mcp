"""Unit tests for tools_updates module."""

import pytest

from ha_mcp.tools.tools_updates import _categorize_update, _supports_release_notes


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
