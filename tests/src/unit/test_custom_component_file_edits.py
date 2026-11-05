"""The edit_file service changes only the matched text of a stored file."""

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Mock the Home Assistant imports before importing the module
sys.modules["voluptuous"] = MagicMock()
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.components.persistent_notification"] = MagicMock()
sys.modules["homeassistant.config"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
# setdefault (not =): other test modules stub homeassistant.core with a real
# `callback` identity function that production closures rely on.
sys.modules.setdefault("homeassistant.core", MagicMock())
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.config_validation"] = MagicMock()
sys.modules["homeassistant.helpers.storage"] = MagicMock()
sys.modules["homeassistant.loader"] = MagicMock()

from custom_components.ha_mcp_tools import file_ops  # noqa: E402
from custom_components.ha_mcp_tools.file_ops import (  # noqa: E402
    build_edit_file_handler,
    edit_file_sync,
)


def _edit(old: str, new: str) -> dict[str, str]:
    return {"old": old, "new": new}


def _file(tmp_path: Path, data: bytes) -> Path:
    target = tmp_path / "card.js"
    target.write_bytes(data)
    return target


class TestEditKeepsTheRestOfTheFile:
    def test_crlf_file_stays_crlf_outside_the_edit(self, tmp_path: Path) -> None:
        target = _file(tmp_path, b"one\r\ntwo\r\nthree\r\n")

        result = edit_file_sync(target, [_edit("two", "TWO")])

        assert target.read_bytes() == b"one\r\nTWO\r\nthree\r\n"
        assert result["size"] == len(target.read_bytes())

    def test_multiline_edit_read_as_lf_matches_a_crlf_file(
        self, tmp_path: Path
    ) -> None:
        # ha_read_file shows a CRLF file with LF line endings.
        target = _file(tmp_path, b"one\r\ntwo\r\nthree\r\n")

        edit_file_sync(target, [_edit("one\ntwo", "ONE\nTWO\nnew")])

        assert target.read_bytes() == b"ONE\r\nTWO\r\nnew\r\nthree\r\n"

    def test_mixed_line_endings_are_not_normalized(self, tmp_path: Path) -> None:
        target = _file(tmp_path, b"one\r\ntwo\nthree\r\n")

        edit_file_sync(target, [_edit("two", "TWO")])

        assert target.read_bytes() == b"one\r\nTWO\nthree\r\n"

    def test_later_edit_sees_the_earlier_edit(self, tmp_path: Path) -> None:
        target = _file(tmp_path, b"alpha\n")

        edit_file_sync(target, [_edit("alpha", "beta"), _edit("beta", "gamma")])

        assert target.read_bytes() == b"gamma\n"


class TestRefusedEditSavesNothing:
    @pytest.mark.parametrize(
        ("data", "edits", "reason", "edit_index"),
        [
            pytest.param(
                b"one\n",
                [_edit("one", "1"), _edit("missing", "x")],
                "old_not_found",
                1,
                id="second-edit-misses",
            ),
            pytest.param(
                b"aaa\n", [_edit("aa", "b")], "old_not_unique", 0, id="overlapping"
            ),
            pytest.param(b"one\n", [_edit("", "x")], "invalid_edit", 0, id="empty-old"),
            pytest.param(
                b"one\n", [_edit("one", "one")], "invalid_edit", 0, id="no-change"
            ),
            pytest.param(
                b"one\n", [_edit("one", "\ud800")], "invalid_edit", 0, id="surrogate"
            ),
        ],
    )
    def test_file_is_unchanged(
        self,
        tmp_path: Path,
        data: bytes,
        edits: list[dict[str, str]],
        reason: str,
        edit_index: int,
    ) -> None:
        target = _file(tmp_path, data)

        result = edit_file_sync(target, edits)

        assert result["_error"] == reason
        assert result["edit_index"] == edit_index
        assert target.read_bytes() == data

    def test_binary_file_is_refused(self, tmp_path: Path) -> None:
        target = _file(tmp_path, b"\xff\xfe\x00one")

        result = edit_file_sync(target, [_edit("one", "1")])

        assert result["_error"] == "not_text"
        assert target.read_bytes() == b"\xff\xfe\x00one"

    def test_oversized_file_is_refused_before_it_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(file_ops, "MAX_EDITABLE_FILE_SIZE", 3)
        target = _file(tmp_path, b"one\n")

        assert edit_file_sync(target, [_edit("one", "1")])["_error"] == "too_large"
        assert target.read_bytes() == b"one\n"

    def test_missing_file_is_not_created(self, tmp_path: Path) -> None:
        target = tmp_path / "missing.js"

        result = edit_file_sync(target, [_edit("one", "1")])

        assert result["_error"] == "not_found"
        assert not target.exists()

    def test_directory_is_refused(self, tmp_path: Path) -> None:
        assert edit_file_sync(tmp_path, [_edit("a", "b")])["_error"] == "not_a_file"

    def test_unmounted_volume_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(file_ops, "on_mounted_volume", lambda _path, _root: False)
        target = _file(tmp_path, b"one\n")

        result = edit_file_sync(
            target, [_edit("one", "1")], volume_root_for=lambda _: "/media"
        )

        assert result["_error"] == "not_mounted"
        assert target.read_bytes() == b"one\n"


class _Hass:
    def __init__(self, config_dir: Path) -> None:
        self.config = MagicMock(config_dir=str(config_dir))

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


def _handler(
    tmp_path: Path, *, token_ok: bool = True, denied: str | None = None
) -> Any:
    return build_edit_file_handler(
        _Hass(tmp_path),  # type: ignore[arg-type]
        caller_token_ok=lambda _hass, _call: token_ok,
        unauthorized_response=lambda service: {"success": False, "error": service},
        write_denied=lambda _hass, _path: denied,
        volume_root_for=lambda _path: None,
    )


def _call(path: str, edits: list[dict[str, str]]) -> MagicMock:
    return MagicMock(data={"path": path, "edits": edits})


class TestEditFileService:
    @pytest.mark.asyncio
    async def test_reports_the_applied_edits(self, tmp_path: Path) -> None:
        target = _file(tmp_path, b"one two\n")

        response = await _handler(tmp_path)(
            _call("card.js", [_edit("one", "1"), _edit("two", "2")])
        )

        assert response["success"] is True
        assert response["path"] == "card.js"
        assert response["edits_applied"] == 2
        assert target.read_bytes() == b"1 2\n"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "guard",
        [{"token_ok": False}, {"denied": "Write not allowed"}],
        ids=["bad-token", "path-outside-allowlist"],
    )
    async def test_refused_caller_changes_nothing(
        self, tmp_path: Path, guard: dict[str, Any]
    ) -> None:
        target = _file(tmp_path, b"one\n")

        response = await _handler(tmp_path, **guard)(
            _call("card.js", [_edit("one", "1")])
        )

        assert response["success"] is False
        assert target.read_bytes() == b"one\n"

    @pytest.mark.asyncio
    async def test_refusal_tells_the_model_which_edit_failed(
        self, tmp_path: Path
    ) -> None:
        _file(tmp_path, b"one\n")

        response = await _handler(tmp_path)(
            _call("card.js", [_edit("one", "1"), _edit("missing", "x")])
        )

        assert response["success"] is False
        assert response["reason"] == "old_not_found"
        assert response["edit_index"] == 1
        assert "card.js" in response["error"]
