"""File writes keep stored bytes, and report what Home Assistant needs after them.

Kept apart from ``test_custom_component_file_edits.py``, which covers the edit
matching itself.
"""

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.ha_mcp_tools as component
from custom_components.ha_mcp_tools import file_ops, file_post_action
from custom_components.ha_mcp_tools.file_ops import (
    _REFUSALS,
    _replace_file_sync,
    edit_file_sync,
    refusal_text,
    write_text_atomic,
)

needs_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="creating symlinks needs privileges on Windows"
)


def _edit(old: str, new: str) -> dict[str, str]:
    return {"old": old, "new": new}


class TestWholeFileReplace:
    def test_a_sibling_tmp_file_survives(self, tmp_path: Path) -> None:
        """The swap used a fixed foo.tmp, which deleted a file of that name."""
        target = tmp_path / "foo.yaml"
        target.write_text("a: 1\n")
        sibling = tmp_path / "foo.tmp"
        sibling.write_text("keep me")

        _replace_file_sync(target, "a: 2\n")

        assert target.read_text() == "a: 2\n"
        assert sibling.read_text() == "keep me"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["foo.tmp", "foo.yaml"]

    def test_a_missing_file_is_created_with_its_folder(self, tmp_path: Path) -> None:
        target = tmp_path / "themes" / "new.yaml"

        result = _replace_file_sync(target, "a: 1\r\n")

        assert target.read_bytes() == b"a: 1\r\n"
        assert result["size"] == 6

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_the_file_mode_survives(self, tmp_path: Path) -> None:
        target = tmp_path / "foo.yaml"
        target.write_text("a: 1\n")
        os.chmod(target, 0o640)

        _replace_file_sync(target, "a: 2\n")

        assert target.stat().st_mode & 0o777 == 0o640

    @needs_symlinks
    def test_a_symlink_stays_a_link(self, tmp_path: Path) -> None:
        real = tmp_path / "real.yaml"
        real.write_text("a: 1\n")
        link = tmp_path / "link.yaml"
        link.symlink_to(real)

        _replace_file_sync(link, "a: 2\n")

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

            def __enter__(self) -> "_FailingWrite":
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
    def test_read_can_keep_crlf(self, tmp_path: Path) -> None:
        target = tmp_path / "card.js"
        target.write_bytes(b"one\r\ntwo\r\n")

        kept = component._read_file_sync(target, keep_line_endings=True)
        shown = component._read_file_sync(target)

        assert kept["content"] == "one\r\ntwo\r\n"
        assert shown["content"] == "one\ntwo\n"

    def test_write_stores_line_endings_as_sent(self, tmp_path: Path) -> None:
        """A restore of a CRLF snapshot must write CRLF back, on any OS."""
        target = tmp_path / "card.js"

        component._write_file_sync(target, "one\r\ntwo\n", True, True, tmp_path)

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


@pytest.mark.parametrize("reason", sorted(_REFUSALS))
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

    assert reasons <= set(_REFUSALS)


class _Hass:
    def __init__(self, config_dir: Path) -> None:
        self.config = MagicMock(config_dir=str(config_dir))
        self.config.path = lambda *parts: os.path.join(str(config_dir), *parts)
        self.data: dict[str, Any] = {}
        self.services = MagicMock(async_call=AsyncMock())


def _yaml_dashboard(path: str, url_path: str) -> MagicMock:
    return MagicMock(
        path=path,
        mode="yaml",
        config={"mode": "yaml", "filename": "dashboards/x.yaml", "title": "Rev"},
        url_path=url_path,
    )


class TestAfterFileWrite:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["themes/rev.yaml", "themes/sub/rev.yml"])
    async def test_a_theme_is_reloaded(self, tmp_path: Path, path: str) -> None:
        hass = _Hass(tmp_path)

        info = await file_post_action.after_file_write(hass, path, 10)  # type: ignore[arg-type]

        hass.services.async_call.assert_awaited_once_with(
            "frontend", "reload_themes", {}, blocking=True
        )
        assert info == {
            "post_action": "reload_performed",
            "reload_service": "frontend.reload_themes",
        }

    @pytest.mark.asyncio
    async def test_a_failed_reload_is_reported(self, tmp_path: Path) -> None:
        hass = _Hass(tmp_path)
        hass.services.async_call.side_effect = RuntimeError("boom")

        info = await file_post_action.after_file_write(hass, "themes/x.yaml", 1)  # type: ignore[arg-type]

        assert info["post_action"] == "reload_available"
        assert info["reload_error"] == "boom"

    @pytest.mark.asyncio
    async def test_a_custom_template_is_reloaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(file_post_action, "_max_custom_template_size", lambda: 100)
        hass = _Hass(tmp_path)

        info = await file_post_action.after_file_write(
            hass,  # type: ignore[arg-type]
            "custom_templates/sub/rev.jinja",
            100,
        )

        hass.services.async_call.assert_awaited_once_with(
            "homeassistant", "reload_custom_templates", {}, blocking=True
        )
        assert info["post_action"] == "reload_performed"

    @pytest.mark.asyncio
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
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
        size: int,
        says: str,
    ) -> None:
        monkeypatch.setattr(file_post_action, "_max_custom_template_size", lambda: 100)
        hass = _Hass(tmp_path)

        info = await file_post_action.after_file_write(hass, path, size)  # type: ignore[arg-type]

        hass.services.async_call.assert_not_awaited()
        assert "post_action" not in info
        assert says in info["warnings"][0]

    @pytest.mark.asyncio
    async def test_a_dashboard_file_names_its_dashboard(self, tmp_path: Path) -> None:
        hass = _Hass(tmp_path)
        backed = _yaml_dashboard(str(tmp_path / "dashboards" / "rev.yaml"), "rev-2976")
        other = _yaml_dashboard(str(tmp_path / "dashboards" / "other.yaml"), "other")
        hass.data["lovelace"] = MagicMock(
            dashboards={None: MagicMock(), "rev-2976": backed, "other": other}
        )

        info = await file_post_action.after_file_write(
            hass,  # type: ignore[arg-type]
            "dashboards/rev.yaml",
            10,
        )

        assert [row["url_path"] for row in info["dashboards"]] == ["rev-2976"]
        assert info["dashboards"][0]["mode"] == "yaml"
        assert info["dashboards"][0]["title"] == "Rev"
        hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unused_dashboard_file_is_flagged(self, tmp_path: Path) -> None:
        hass = _Hass(tmp_path)
        hass.data["lovelace"] = MagicMock(dashboards={})

        info = await file_post_action.after_file_write(
            hass,  # type: ignore[arg-type]
            "dashboards/rev.yaml",
            10,
        )

        assert "No YAML dashboard names dashboards/rev.yaml" in info["warnings"][0]

    @pytest.mark.asyncio
    async def test_an_absolute_path_inside_the_config_dir_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A custom directory can name the config dir by its absolute path."""
        monkeypatch.setattr(file_post_action, "_max_custom_template_size", lambda: 100)
        hass = _Hass(tmp_path)
        path = str(tmp_path / "custom_templates" / "rev.jinja")

        info = await file_post_action.after_file_write(hass, path, 10)  # type: ignore[arg-type]

        assert info["reload_service"] == "homeassistant.reload_custom_templates"

    @pytest.mark.asyncio
    async def test_unreadable_dashboards_claim_nothing(self, tmp_path: Path) -> None:
        """Without Core's dashboards nothing was checked, so nothing is said."""
        hass = _Hass(tmp_path)

        info = await file_post_action.after_file_write(
            hass,  # type: ignore[arg-type]
            "dashboards/rev.yaml",
            10,
        )

        assert info == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path", ["www/card.js", "/share/themes/x.yaml", "themes/readme.md"]
    )
    async def test_other_files_need_nothing(self, tmp_path: Path, path: str) -> None:
        hass = _Hass(tmp_path)

        assert await file_post_action.after_file_write(hass, path, 1) == {}  # type: ignore[arg-type]
        hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_edit_reports_the_theme_reload(tmp_path: Path) -> None:
    (tmp_path / "themes").mkdir()
    (tmp_path / "themes" / "rev.yaml").write_text("rev:\n  primary-color: red\n")
    hass = MagicMock()
    hass.config = _Hass(tmp_path).config
    hass.data = {}
    hass.services = MagicMock(async_call=AsyncMock())

    async def run(func: Any, *args: Any) -> Any:
        return func(*args)

    hass.async_add_executor_job = run
    handler = file_ops.build_edit_file_handler(
        hass,
        caller_token_ok=lambda _hass, _call: True,
        unauthorized_response=lambda service: {"success": False},
        write_denied=lambda _hass, _path: None,
        volume_root_for=lambda _path: None,
    )

    response = await handler(
        MagicMock(data={"path": "themes/rev.yaml", "edits": [_edit("red", "blue")]})
    )

    assert response["success"] is True
    assert response["post_action"] == "reload_performed"
