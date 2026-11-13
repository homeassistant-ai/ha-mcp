"""File writes keep stored bytes, and report what Home Assistant needs after them.

Kept apart from ``test_custom_component_file_edits.py``, which covers the edit
matching itself.
"""

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceNotFound
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, async_mock_service

from custom_components.ha_mcp_tools import (
    CALLER_TOKEN_FIELD,
    SERVICE_GET_CALLER_TOKEN,
    SERVICE_READ_FILE,
    SERVICE_WRITE_FILE,
    file_ops,
    file_post_action,
)
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN, TOOLS_ENTRY_TITLE
from custom_components.ha_mcp_tools.file_ops import (
    edit_file_sync,
    refusal_text,
    write_text_atomic,
)

needs_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="creating symlinks needs privileges on Windows"
)


def _edit(old: str, new: str) -> dict[str, str]:
    return {"old": old, "new": new}


async def _file_service(hass: HomeAssistant) -> Any:
    """Set up the component and return a caller that presents its token."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TOOLS_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    async def call(service: str, data: dict[str, Any]) -> dict[str, Any]:
        response = await hass.services.async_call(
            DOMAIN, service, data, blocking=True, return_response=True
        )
        assert response is not None
        return dict(response)

    bootstrap = await call(SERVICE_GET_CALLER_TOKEN, {})

    async def call_with_token(service: str, data: dict[str, Any]) -> dict[str, Any]:
        return await call(service, {**data, CALLER_TOKEN_FIELD: bootstrap["token"]})

    return call_with_token


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Core's config directory is this test's ``tmp_path``."""
    return hass_tmp_config_dir


class TestWholeFileReplace:
    def test_a_sibling_tmp_file_survives(self, tmp_path: Path) -> None:
        """The swap used a fixed foo.tmp, which deleted a file of that name."""
        target = tmp_path / "foo.yaml"
        target.write_text("a: 1\n")
        sibling = tmp_path / "foo.tmp"
        sibling.write_text("keep me")

        write_text_atomic(target, "a: 2\n")

        assert target.read_text() == "a: 2\n"
        assert sibling.read_text() == "keep me"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["foo.tmp", "foo.yaml"]

    async def test_a_missing_file_is_created_with_its_folder(
        self, hass: HomeAssistant
    ) -> None:
        call = await _file_service(hass)
        target = Path(hass.config.config_dir) / "themes" / "new.yaml"

        result = await call(
            SERVICE_WRITE_FILE, {"path": "themes/new.yaml", "content": "a: 1\r\n"}
        )

        assert result["success"] is True
        assert result["created"] is True
        assert target.read_bytes() == b"a: 1\r\n"
        assert result["size"] == 6

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_the_file_mode_survives(self, tmp_path: Path) -> None:
        target = tmp_path / "foo.yaml"
        target.write_text("a: 1\n")
        os.chmod(target, 0o640)

        write_text_atomic(target, "a: 2\n")

        assert target.stat().st_mode & 0o777 == 0o640

    @needs_symlinks
    def test_a_symlink_stays_a_link(self, tmp_path: Path) -> None:
        real = tmp_path / "real.yaml"
        real.write_text("a: 1\n")
        link = tmp_path / "link.yaml"
        link.symlink_to(real)

        write_text_atomic(link, "a: 2\n")

        assert link.is_symlink()
        assert real.read_text() == "a: 2\n"

    def test_a_failed_new_write_leaves_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "new.yaml"
        real_open = open

        class _FailingWrite:
            def __init__(self, handle: Any) -> None:
                self.handle = handle

            def __enter__(self) -> _FailingWrite:
                return self

            def __exit__(self, *_exc: object) -> None:
                self.handle.close()

            def close(self) -> None:
                self.handle.close()

            def write(self, _data: bytes) -> int:
                raise OSError("disk full")

        monkeypatch.setattr(
            file_ops,
            "open",
            lambda *args, **kwargs: _FailingWrite(real_open(*args, **kwargs)),
            raising=False,
        )

        with pytest.raises(OSError, match="disk full"):
            write_text_atomic(target, "a: 1")

        assert not target.exists()


class TestLineEndingsAsStored:
    async def test_read_can_keep_crlf(self, hass: HomeAssistant) -> None:
        call = await _file_service(hass)
        target = Path(hass.config.config_dir) / "www" / "card.js"
        target.parent.mkdir()
        target.write_bytes(b"one\r\ntwo\r\n")

        kept = await call(
            SERVICE_READ_FILE, {"path": "www/card.js", "keep_line_endings": True}
        )
        shown = await call(SERVICE_READ_FILE, {"path": "www/card.js"})

        assert kept["success"] is True
        assert shown["success"] is True
        assert kept["content"] == "one\r\ntwo\r\n"
        assert shown["content"] == "one\ntwo\n"

    async def test_write_stores_line_endings_as_sent(self, hass: HomeAssistant) -> None:
        """A restore of a CRLF snapshot must write CRLF back, on any OS."""
        call = await _file_service(hass)
        target = Path(hass.config.config_dir) / "www" / "card.js"

        result = await call(
            SERVICE_WRITE_FILE,
            {"path": "www/card.js", "content": "one\r\ntwo\n", "overwrite": True},
        )

        assert result["success"] is True
        assert target.read_bytes() == b"one\r\ntwo\n"


class TestLineEndingRefusals:
    def test_an_edit_of_line_endings_only_is_refused(self, tmp_path: Path) -> None:
        """Compared after translation, this edit would save nothing; compared
        before, it was refused as identical to 'new'."""
        target = tmp_path / "a.txt"
        target.write_bytes(b"alpha\r\nbeta\r\n")

        result = edit_file_sync(target, [_edit("beta\n", "beta\r\n")])

        assert result["_error"] == "line_endings_only"
        assert target.read_bytes() == b"alpha\r\nbeta\r\n"

    def test_a_miss_in_a_mixed_file_says_the_file_is_mixed(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "a.txt"
        target.write_bytes(b"one\r\ntwo\r\nthree\n")

        result = edit_file_sync(target, [_edit("one\ntwo", "x")])

        assert result == {
            "_error": "old_not_found",
            "edit_index": 0,
            "line_endings": "mixed",
        }
        reason = result.pop("_error")
        assert "mixes CRLF and LF" in refusal_text(reason, "a.txt", result)

    def test_a_single_line_miss_in_a_mixed_file_stays_generic(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "a.txt"
        target.write_bytes(b"one\r\ntwo\n")

        result = edit_file_sync(target, [_edit("missing", "x")])

        assert "line_endings" not in result

    def test_a_multi_line_old_matches_lines_stored_with_its_ending(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "a.txt"
        target.write_bytes(b"one\r\ntwo\nthree\n")

        edit_file_sync(target, [_edit("two\nthree", "TWO\nTHREE")])

        assert target.read_bytes() == b"one\r\nTWO\nTHREE\n"


# Every field a refusal can carry, so each template is rendered once.
_REFUSAL_FIELDS = {
    "edit_index": 1,
    "invalid_edit_indexes": [1],
    "matches": 2,
    "parent": "/share",
}


_REFUSAL_REASONS = (
    "invalid_edit",
    "line_endings_only",
    "not_a_file",
    "not_found",
    "not_mounted",
    "not_text",
    "old_not_found",
    "old_not_unique",
    "too_large",
)


@pytest.mark.parametrize("reason", _REFUSAL_REASONS)
def test_every_refusal_renders(reason: str) -> None:
    """A missing field or template would raise KeyError inside the service."""
    text = refusal_text(reason, "www/a.css", dict(_REFUSAL_FIELDS))

    assert "{" not in text


def test_every_refusal_reason_has_a_template() -> None:
    source = Path(file_ops.__file__).read_text(encoding="utf-8")
    reasons = {
        line.split('"_error": "')[1].split('"')[0]
        for line in source.splitlines()
        if '"_error": "' in line
    }

    assert reasons <= set(_REFUSAL_REASONS)


class TestAfterFileWrite:
    @pytest.mark.parametrize("path", ["themes/rev.yaml", "themes/sub/rev.yml"])
    async def test_a_theme_is_reloaded(
        self, hass: HomeAssistant, hass_config_dir: str, path: str
    ) -> None:
        reload_calls = async_mock_service(hass, "frontend", "reload_themes")

        info = await file_post_action.after_file_write(hass, path, 10)

        assert len(reload_calls) == 1
        assert reload_calls[0].data == {}
        assert info == {
            "post_action": "reload_performed",
            "reload_service": "frontend.reload_themes",
        }

    async def test_a_failed_reload_is_reported(
        self, hass: HomeAssistant, hass_config_dir: str
    ) -> None:
        """Without the frontend loaded, Core has no frontend.reload_themes."""

        info = await file_post_action.after_file_write(hass, "themes/x.yaml", 1)

        assert info["post_action"] == "reload_available"
        assert info["reload_error"] == str(ServiceNotFound("frontend", "reload_themes"))

    async def test_a_custom_template_is_reloaded(
        self,
        hass: HomeAssistant,
        hass_config_dir: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(file_post_action, "_max_custom_template_size", lambda: 100)
        reload_calls = async_mock_service(
            hass, "homeassistant", "reload_custom_templates"
        )

        info = await file_post_action.after_file_write(
            hass,
            "custom_templates/sub/rev.jinja",
            100,
        )

        assert len(reload_calls) == 1
        assert reload_calls[0].data == {}
        assert info["post_action"] == "reload_performed"

    @pytest.mark.parametrize(
        ("path", "size", "says"),
        [
            ("custom_templates/rev.txt", 10, "*.jinja"),
            ("custom_templates/rev.jinja", 101, "over 100 bytes"),
        ],
        ids=["not-jinja", "too-large"],
    )
    async def test_a_template_core_skips_is_flagged(
        self,
        hass: HomeAssistant,
        hass_config_dir: str,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
        size: int,
        says: str,
    ) -> None:
        monkeypatch.setattr(file_post_action, "_max_custom_template_size", lambda: 100)
        reload_calls = async_mock_service(
            hass, "homeassistant", "reload_custom_templates"
        )

        info = await file_post_action.after_file_write(hass, path, size)

        assert reload_calls == []
        assert "post_action" not in info
        assert says in info["warnings"][0]

    async def test_a_dashboard_file_names_its_dashboard(
        self, hass: HomeAssistant, hass_config_dir: str
    ) -> None:
        assert await async_setup_component(
            hass,
            "lovelace",
            {
                "lovelace": {
                    "dashboards": {
                        "rev-2976": {
                            "mode": "yaml",
                            "filename": "dashboards/rev.yaml",
                            "title": "Rev",
                        },
                        "other-dash": {
                            "mode": "yaml",
                            "filename": "dashboards/other.yaml",
                            "title": "Other",
                        },
                    }
                }
            },
        )

        info = await file_post_action.after_file_write(
            hass,
            "dashboards/rev.yaml",
            10,
        )

        assert [row["url_path"] for row in info["dashboards"]] == ["rev-2976"]
        assert info["dashboards"][0]["mode"] == "yaml"
        assert info["dashboards"][0]["title"] == "Rev"

    async def test_an_unused_dashboard_file_is_flagged(
        self, hass: HomeAssistant, hass_config_dir: str
    ) -> None:
        assert await async_setup_component(hass, "lovelace", {"lovelace": {}})

        info = await file_post_action.after_file_write(
            hass,
            "dashboards/rev.yaml",
            10,
        )

        assert "No YAML dashboard names dashboards/rev.yaml" in info["warnings"][0]

    async def test_an_absolute_path_inside_the_config_dir_counts(
        self,
        hass: HomeAssistant,
        hass_config_dir: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A custom directory can name the config dir by its absolute path."""
        monkeypatch.setattr(file_post_action, "_max_custom_template_size", lambda: 100)
        async_mock_service(hass, "homeassistant", "reload_custom_templates")
        path = str(tmp_path / "custom_templates" / "rev.jinja")

        info = await file_post_action.after_file_write(hass, path, 10)

        assert info["reload_service"] == "homeassistant.reload_custom_templates"

    async def test_unreadable_dashboards_claim_nothing(
        self, hass: HomeAssistant, hass_config_dir: str
    ) -> None:
        """Without Core's dashboards nothing was checked, so nothing is said."""

        info = await file_post_action.after_file_write(
            hass,
            "dashboards/rev.yaml",
            10,
        )

        assert info == {}

    @pytest.mark.parametrize(
        "path", ["www/card.js", "/share/themes/x.yaml", "themes/readme.md"]
    )
    async def test_other_files_need_nothing(
        self, hass: HomeAssistant, hass_config_dir: str, path: str
    ) -> None:
        reload_calls = async_mock_service(hass, "frontend", "reload_themes")

        assert await file_post_action.after_file_write(hass, path, 1) == {}
        assert reload_calls == []


async def test_an_edit_reports_the_theme_reload(
    hass: HomeAssistant, hass_config_dir: str, tmp_path: Path
) -> None:
    (tmp_path / "themes").mkdir()
    (tmp_path / "themes" / "rev.yaml").write_text("rev:\n  primary-color: red\n")
    async_mock_service(hass, "frontend", "reload_themes")
    handler = file_ops.build_edit_file_handler(
        hass,
        caller_token_ok=lambda _hass, _call: True,
        unauthorized_response=lambda service: {"success": False},
        write_denied=lambda _hass, _path: None,
        volume_root_for=lambda _path: None,
    )

    response = await handler(
        ServiceCall(
            hass=hass,
            domain=DOMAIN,
            service="edit_file",
            data={"path": "themes/rev.yaml", "edits": [_edit("red", "blue")]},
        )
    )

    assert response is not None
    assert response["success"] is True
    assert response["post_action"] == "reload_performed"
