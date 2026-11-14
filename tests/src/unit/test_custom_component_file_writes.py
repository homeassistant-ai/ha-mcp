"""File writes keep stored bytes, and report what Home Assistant needs after them.

Kept apart from ``test_custom_component_file_edits.py``, which covers the edit
matching itself.
"""

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.homeassistant import services as homeassistant_services
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.template import MAX_CUSTOM_TEMPLATE_SIZE
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import async_mock_service

from custom_components.ha_mcp_tools import (
    SERVICE_READ_FILE,
    SERVICE_WRITE_FILE,
    file_ops,
    file_post_action,
)
from custom_components.ha_mcp_tools.file_ops import (
    edit_file_sync,
    refusal_text,
    write_text_atomic,
)

from ._edit_yaml import EditYaml, make_edit_yaml
from ._file_services import make_file_service

needs_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="creating symlinks needs privileges on Windows"
)


def _edit(old: str, new: str) -> dict[str, str]:
    return {"old": old, "new": new}


def _replace_file(path: str, content: str) -> dict[str, str]:
    return {
        "file": path,
        "action": "replace_file",
        "yaml_path": "",
        "content": content,
    }


@pytest.fixture
async def edit_yaml(hass: HomeAssistant) -> EditYaml:
    return await make_edit_yaml(hass)


class TestWholeFileReplace:
    async def test_a_sibling_tmp_file_survives(
        self, hass: HomeAssistant, edit_yaml: EditYaml
    ) -> None:
        """The swap used a fixed foo.tmp, which deleted a file of that name."""
        themes = Path(hass.config.config_dir) / "themes"
        themes.mkdir()
        target = themes / "foo.yaml"
        target.write_text("a: 1\n")
        sibling = themes / "foo.tmp"
        sibling.write_text("keep me")

        result = await edit_yaml(_replace_file("themes/foo.yaml", "a: 2\n"))

        assert result["success"] is True, result
        assert target.read_text() == "a: 2\n"
        assert sibling.read_text() == "keep me"
        assert sorted(p.name for p in themes.iterdir()) == ["foo.tmp", "foo.yaml"]

    async def test_a_missing_file_is_created_with_its_folder(
        self, hass: HomeAssistant, edit_yaml: EditYaml
    ) -> None:
        target = Path(hass.config.config_dir) / "themes" / "new.yaml"

        result = await edit_yaml(_replace_file("themes/new.yaml", "a: 1\r\n"))

        assert result["success"] is True
        assert target.read_bytes() == b"a: 1\r\n"
        assert result["size"] == 6

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    async def test_the_file_mode_survives(
        self, hass: HomeAssistant, edit_yaml: EditYaml
    ) -> None:
        themes = Path(hass.config.config_dir) / "themes"
        themes.mkdir()
        target = themes / "foo.yaml"
        target.write_text("a: 1\n")
        os.chmod(target, 0o640)

        result = await edit_yaml(_replace_file("themes/foo.yaml", "a: 2\n"))

        assert result["success"] is True, result
        assert target.stat().st_mode & 0o777 == 0o640

    @needs_symlinks
    async def test_a_symlink_stays_a_link(
        self, hass: HomeAssistant, edit_yaml: EditYaml
    ) -> None:
        themes = Path(hass.config.config_dir) / "themes"
        themes.mkdir()
        real = themes / "real.yaml"
        real.write_text("a: 1\n")
        link = themes / "link.yaml"
        link.symlink_to(real)

        result = await edit_yaml(_replace_file("themes/link.yaml", "a: 2\n"))

        assert result["success"] is True, result
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
        call = await make_file_service(hass)
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
        call = await make_file_service(hass)
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


@pytest.mark.parametrize("reason", file_ops.REFUSALS)
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

    assert reasons == set(file_ops.REFUSALS)


class TestAfterFileWrite:
    @pytest.mark.parametrize("path", ["themes/rev.yaml", "themes/sub/rev.yml"])
    async def test_a_theme_is_reloaded(self, hass: HomeAssistant, path: str) -> None:
        reload_calls = async_mock_service(hass, "frontend", "reload_themes")

        info = await file_post_action.after_file_write(hass, path, 10)

        assert len(reload_calls) == 1
        assert reload_calls[0].data == {}
        assert info == {
            "post_action": "reload_performed",
            "reload_service": "frontend.reload_themes",
        }

    async def test_a_failed_custom_template_reload_is_reported(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Core's reload handler can report a failure after the write."""
        assert await async_setup_component(hass, "homeassistant", {})

        async def fail_reload(_hass: HomeAssistant) -> None:
            raise HomeAssistantError("reload boom")

        monkeypatch.setattr(
            homeassistant_services, "async_load_custom_templates", fail_reload
        )

        info = await file_post_action.after_file_write(
            hass, "custom_templates/x.jinja", 1
        )

        assert info["post_action"] == "reload_available"
        assert info["reload_service"] == "homeassistant.reload_custom_templates"
        assert info["reload_error"] == "reload boom"

    async def test_a_custom_template_is_reloaded(self, hass: HomeAssistant) -> None:
        assert await async_setup_component(hass, "homeassistant", {})

        info = await file_post_action.after_file_write(
            hass,
            "custom_templates/sub/rev.jinja",
            MAX_CUSTOM_TEMPLATE_SIZE,
        )

        assert info == {
            "post_action": "reload_performed",
            "reload_service": "homeassistant.reload_custom_templates",
        }

    @pytest.mark.parametrize(
        ("path", "size", "says"),
        [
            ("custom_templates/rev.txt", 10, "*.jinja"),
            (
                "custom_templates/rev.jinja",
                MAX_CUSTOM_TEMPLATE_SIZE + 1,
                f"over {MAX_CUSTOM_TEMPLATE_SIZE:,} bytes",
            ),
        ],
        ids=["not-jinja", "too-large"],
    )
    async def test_a_template_core_skips_is_flagged(
        self,
        hass: HomeAssistant,
        path: str,
        size: int,
        says: str,
    ) -> None:
        assert await async_setup_component(hass, "homeassistant", {})

        info = await file_post_action.after_file_write(hass, path, size)

        assert "post_action" not in info
        assert says in info["warnings"][0]

    async def test_a_dashboard_file_names_its_dashboard(
        self, hass: HomeAssistant
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
        assert set(info) == {"dashboards"}

    async def test_an_unused_dashboard_file_is_flagged(
        self, hass: HomeAssistant
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
    ) -> None:
        """A custom directory can name the config dir by its absolute path."""
        assert await async_setup_component(hass, "homeassistant", {})
        path = str(Path(hass.config.config_dir) / "custom_templates" / "rev.jinja")

        info = await file_post_action.after_file_write(
            hass, path, MAX_CUSTOM_TEMPLATE_SIZE
        )

        assert info == {
            "post_action": "reload_performed",
            "reload_service": "homeassistant.reload_custom_templates",
        }

    async def test_unreadable_dashboards_claim_nothing(
        self, hass: HomeAssistant
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
        self, hass: HomeAssistant, path: str
    ) -> None:
        reload_calls = async_mock_service(hass, "frontend", "reload_themes")

        assert await file_post_action.after_file_write(hass, path, 1) == {}
        assert reload_calls == []


async def test_an_edit_reports_the_theme_reload(
    hass: HomeAssistant,
) -> None:
    call = await make_file_service(hass)
    themes = Path(hass.config.config_dir) / "themes"
    themes.mkdir()
    target = themes / "rev.yaml"
    target.write_text("rev:\n  primary-color: red\n")
    reload_calls = async_mock_service(hass, "frontend", "reload_themes")

    response = await call(
        file_ops.SERVICE_EDIT_FILE,
        {"path": "themes/rev.yaml", "edits": [_edit("red", "blue")]},
    )

    assert response is not None
    assert response["success"] is True
    assert response["post_action"] == "reload_performed"
    assert target.read_text() == "rev:\n  primary-color: blue\n"
    assert len(reload_calls) == 1
