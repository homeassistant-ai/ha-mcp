"""Unit tests for tools_updates module."""


from ha_mcp.tools.tools_updates import (
    _categorize_update,
    _extract_blog_content,
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
        assert _parse_version("2025.11.3") == (2025, 11, 3)

    def test_major_minor_only(self):
        assert _parse_version("2025.11") == (2025, 11)

    def test_empty_and_invalid(self):
        assert _parse_version("") is None
        assert _parse_version("beta") is None
        assert _parse_version("2025.11.beta") is None

    def test_version_comparison(self):
        assert _parse_version("2025.10.0") < _parse_version("2025.11.0")  # type: ignore[operator]
        assert _parse_version("2025.11.1") < _parse_version("2025.11.3")  # type: ignore[operator]


class TestGetMonthlyVersionsBetween:
    """Test _get_monthly_versions_between function."""

    def test_single_month_gap(self):
        assert _get_monthly_versions_between("2025.11.3", "2025.12.0") == ["2025.12.0"]

    def test_multi_month_gap(self):
        assert _get_monthly_versions_between("2025.10.3", "2026.2.1") == [
            "2025.11.0", "2025.12.0", "2026.1.0", "2026.2.0",
        ]

    def test_year_boundary(self):
        assert _get_monthly_versions_between("2025.11.0", "2026.1.0") == ["2025.12.0", "2026.1.0"]

    def test_same_month_returns_empty(self):
        assert _get_monthly_versions_between("2025.11.0", "2025.11.3") == []
        assert _get_monthly_versions_between("2025.11.0", "2025.11.0") == []

    def test_invalid_versions_fallback(self):
        assert _get_monthly_versions_between("bad", "2025.11.0") == ["2025.11.0"]


class TestStripHtml:
    """Test _strip_html function."""

    def test_removes_tags(self):
        assert _strip_html("<b>bold</b> text") == "bold text"

    def test_preserves_structure(self):
        result = _strip_html("<p>First</p><p>Second</p>")
        assert "First" in result and "Second" in result and "\n" in result
        result = _strip_html("<ul><li>One</li><li>Two</li></ul>")
        assert "- One" in result and "- Two" in result

    def test_edge_cases(self):
        assert _strip_html("") == ""
        assert _strip_html("just plain text") == "just plain text"


class TestExtractBlogContent:
    """Test _extract_blog_content function."""

    def test_extracts_article_content(self):
        html = "<nav>Nav</nav><article><h2>Release</h2><p>Great stuff</p></article><footer>F</footer>"
        result = _extract_blog_content(html)
        assert "Great stuff" in result and "Nav" not in result

    def test_fallback_to_heading(self):
        html = '<div>Nav</div><h1>Release</h1><p>Content</p><footer class="f">F</footer>'
        assert "Content" in _extract_blog_content(html)

    def test_strips_html_tags(self):
        result = _extract_blog_content("<article><p><b>Bold</b> text</p></article>")
        assert "<" not in result and "Bold" in result


class TestParseBreakingChangesHtml:
    """Test _parse_breaking_changes_html function."""

    SAMPLE_HTML = (
        '<h2 id="backward-incompatible-changes">Backward-incompatible changes</h2>'
        "<h3>Tuya</h3><p>HVACMode converted to presets.</p>"
        "<h3>Group</h3><p>Sensor group behavior changed.</p>"
        '<h2 id="all-changes">All changes</h2>'
    )

    def test_parses_entries(self):
        result = _parse_breaking_changes_html(self.SAMPLE_HTML, "https://example.com")
        assert result is not None
        assert result["count"] == 2
        names = [e["integration"] for e in result["entries"]]
        assert "Tuya" in names and "Group" in names

    def test_entry_descriptions(self):
        result = _parse_breaking_changes_html(self.SAMPLE_HTML, "https://example.com")
        assert result is not None
        tuya = next(e for e in result["entries"] if e["integration"] == "Tuya")
        assert "HVACMode" in tuya["description"]

    def test_no_section_returns_none(self):
        assert _parse_breaking_changes_html("<h2>Other</h2>", "url") is None

    def test_empty_section_returns_none(self):
        html = '<h2 id="backward-incompatible-changes">BC</h2><h2 id="next">N</h2>'
        assert _parse_breaking_changes_html(html, "url") is None


class TestParsePatchBreakingChanges:
    """Test _parse_patch_breaking_changes function."""

    def test_parses_tagged_items(self):
        body = "- Normal fix\n- Tuya fix ([tuya docs]) (breaking-change)\n- Another fix\n"
        result = _parse_patch_breaking_changes(body, "2025.11.1")
        assert result is not None
        assert result["count"] == 1
        assert result["entries"][0]["integration"] == "tuya"
        assert "2025.11.1" in result["source_url"]

    def test_no_breaking_changes(self):
        assert _parse_patch_breaking_changes("- Normal fix\n", "2025.11.1") is None

    def test_integration_extraction(self):
        body = "- Fix ([vesync documentation]) (breaking-change)\n"
        result = _parse_patch_breaking_changes(body, "2025.11.2")
        assert result is not None and result["entries"][0]["integration"] == "vesync"

    def test_unknown_when_no_docs_link(self):
        body = "- Some change (breaking-change)\n"
        result = _parse_patch_breaking_changes(body, "2025.11.2")
        assert result is not None and result["entries"][0]["integration"] == "unknown"

    def test_case_insensitive(self):
        body = "- Fix ([hue docs]) (Breaking-Change)\n"
        result = _parse_patch_breaking_changes(body, "2025.11.1")
        assert result is not None and result["count"] == 1


