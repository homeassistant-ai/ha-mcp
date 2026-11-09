"""The edit_file service changes only the matched text of a stored file."""

import asyncio
import errno
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import custom_components.ha_mcp_tools as component
from custom_components.ha_mcp_tools import file_ops
from custom_components.ha_mcp_tools.file_ops import (
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

        # A multi-line 'new' stays as sent: treating the file as CRLF would turn
        # this newline into \r\n.
        edit_file_sync(target, [_edit("two", "T\nW")])

        assert target.read_bytes() == b"one\r\nT\nW\nthree\r\n"

    def test_longest_valid_file_name_can_be_edited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A name at the filesystem's limit must stay editable, so the temp file
        cannot inherit it. The target's length stands in for that limit, which
        Windows' shorter path limit would otherwise hide."""
        target = _file(tmp_path, b"one\n").rename(tmp_path / ("a" * 100))
        real_mkstemp = tempfile.mkstemp

        def limited_mkstemp(*, dir: Path, prefix: str, suffix: str) -> tuple[int, str]:  # noqa: A002
            random_part = 8
            if len(prefix) + random_part + len(suffix) > len(target.name):
                raise OSError(errno.ENAMETOOLONG, "File name too long")
            return real_mkstemp(dir=dir, prefix=prefix, suffix=suffix)

        monkeypatch.setattr(file_ops.tempfile, "mkstemp", limited_mkstemp)

        edit_file_sync(target, [_edit("one", "1")])

        assert target.read_bytes() == b"1\n"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_file_mode_survives_an_edit(self, tmp_path: Path) -> None:
        target = _file(tmp_path, b"one\n")
        target.chmod(0o640)

        edit_file_sync(target, [_edit("one", "1")])

        assert target.stat().st_mode & 0o777 == 0o640

    @pytest.mark.skipif(os.name == "nt", reason="Symlinks need a privilege on Windows")
    def test_symlink_stays_a_symlink_and_its_target_changes(
        self, tmp_path: Path
    ) -> None:
        target = _file(tmp_path, b"one\n")
        link = tmp_path / "link.js"
        link.symlink_to(target)

        edit_file_sync(link, [_edit("one", "1")])

        assert link.is_symlink()
        assert target.read_bytes() == b"1\n"

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

    def test_oversized_file_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(file_ops, "MAX_EDITABLE_FILE_SIZE", 3)
        target = _file(tmp_path, b"one\n")

        assert edit_file_sync(target, [_edit("one", "1")])["_error"] == "too_large"
        assert target.read_bytes() == b"one\n"

    def test_edit_cannot_grow_a_file_past_the_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise the write succeeds and every later edit is refused."""
        monkeypatch.setattr(file_ops, "MAX_EDITABLE_FILE_SIZE", 5)
        target = _file(tmp_path, b"one\n")

        assert edit_file_sync(target, [_edit("one", "12345")])["_error"] == "too_large"
        assert target.read_bytes() == b"one\n"

    def test_every_invalid_edit_is_named_at_once(self, tmp_path: Path) -> None:
        target = _file(tmp_path, b"one\n")

        result = edit_file_sync(
            target, [_edit("", "x"), _edit("one", "1"), _edit("one", "one")]
        )

        assert result["_error"] == "invalid_edit"
        assert result["invalid_edit_indexes"] == [0, 2]
        assert target.read_bytes() == b"one\n"

    def test_full_disk_leaves_the_file_and_no_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Writing in place would have emptied the file before failing."""

        def disk_full(_fd: int) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(file_ops.os, "fsync", disk_full)
        target = _file(tmp_path, b"one\n")

        with pytest.raises(OSError, match="No space left"):
            edit_file_sync(target, [_edit("one", "1")])

        assert target.read_bytes() == b"one\n"
        assert [path.name for path in tmp_path.iterdir()] == [target.name]

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
        self.data: dict[str, Any] = {}
        self.running = 0
        self.most_running = 0
        self.before_job: Any = lambda: asyncio.sleep(0)

    def async_add_executor_job(self, func: Any, *args: Any) -> asyncio.Future[Any]:
        """Return a future, as the real executor does."""

        async def job() -> Any:
            self.running += 1
            self.most_running = max(self.most_running, self.running)
            # Hands over to any other call, as a real executor thread would.
            await self.before_job()
            try:
                return func(*args)
            finally:
                self.running -= 1

        # Shielded because cancelling a call never stops a running thread.
        return asyncio.shield(asyncio.ensure_future(job()))


def _handler(
    tmp_path: Path,
    *,
    token_ok: bool = True,
    denied: str | None = None,
    hass: _Hass | None = None,
) -> Any:
    return build_edit_file_handler(
        hass or _Hass(tmp_path),  # type: ignore[arg-type]
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
    async def test_concurrent_edits_do_not_overlap(self, tmp_path: Path) -> None:
        """Two overlapping read-modify-writes would lose one edit and report
        success for both."""
        target = _file(tmp_path, b"one two\n")
        hass = _Hass(tmp_path)
        handler = _handler(tmp_path, hass=hass)

        responses = await asyncio.gather(
            handler(_call("card.js", [_edit("one", "1")])),
            handler(_call("card.js", [_edit("two", "2")])),
        )

        assert [response["success"] for response in responses] == [True, True]
        assert hass.most_running == 1
        assert target.read_bytes() == b"1 2\n"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("build_other", "other_data"),
        [
            (
                "_build_write_file_handler",
                {"path": "card.js", "content": "written\n", "overwrite": True},
            ),
            ("_build_delete_file_handler", {"path": "card.js"}),
        ],
        ids=["write", "delete"],
    )
    async def test_edit_does_not_overlap_other_file_changes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        build_other: str,
        other_data: dict[str, Any],
    ) -> None:
        """An edit that read the file first would swap its stale result back in,
        undoing the write or delete while both report success."""
        monkeypatch.setattr(component, "_caller_token_ok", lambda *_: True)
        monkeypatch.setattr(component, "_write_denied", lambda *_: None)
        monkeypatch.setattr(component, "_current_extra_dirs", lambda _: [])
        monkeypatch.setattr(component, "_is_path_allowed_for_dir", lambda *_: True)
        _file(tmp_path, b"one\n")
        hass = _Hass(tmp_path)
        other = getattr(component, build_other)(hass)

        responses = await asyncio.gather(
            _handler(tmp_path, hass=hass)(_call("card.js", [_edit("one", "1")])),
            other(MagicMock(data=other_data)),
        )

        assert [response["success"] for response in responses] == [True, True]
        assert hass.most_running == 1

    @pytest.mark.asyncio
    async def test_cancelled_call_keeps_the_lock_until_its_thread_ends(
        self, tmp_path: Path
    ) -> None:
        """A cancelled call can't stop its executor thread, so the next change
        has to wait for that thread or one of the two is lost."""
        target = _file(tmp_path, b"one two\n")
        hass = _Hass(tmp_path)
        thread_may_finish = asyncio.Event()
        hass.before_job = thread_may_finish.wait
        handler = _handler(tmp_path, hass=hass)

        cancelled = asyncio.create_task(handler(_call("card.js", [_edit("one", "1")])))
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.sleep(0)
        following = asyncio.create_task(handler(_call("card.js", [_edit("two", "2")])))
        await asyncio.sleep(0)
        # A second cancel must not release the lock either.
        cancelled.cancel()
        await asyncio.sleep(0)
        thread_may_finish.set()
        outcome, response = await asyncio.gather(
            cancelled, following, return_exceptions=True
        )

        assert isinstance(outcome, asyncio.CancelledError)
        assert response["success"] is True
        assert hass.most_running == 1
        assert target.read_bytes() == b"1 2\n"

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
