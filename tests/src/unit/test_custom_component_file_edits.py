"""The edit_file service changes only the matched text of a stored file."""

import asyncio
import errno
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

import custom_components.ha_mcp_tools as component
from custom_components.ha_mcp_tools import file_ops
from custom_components.ha_mcp_tools.file_ops import edit_file_sync

from ._file_services import make_file_service


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


class _FileJobs:
    """Observe real executor jobs, holding the first until both callers arrive."""

    def __init__(self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch):
        self.hass = hass
        self.entered = asyncio.Event()
        self.attempted = asyncio.Event()
        self.overlap = asyncio.Event()
        self.release = threading.Event()
        self.running = 0
        self.attempts = 0
        self.lock = threading.Lock()
        original = file_ops.run_file_job

        async def run(core, function, *args):
            self.attempts += 1
            ordinal = self.attempts
            if ordinal == 2:
                self.attempted.set()

            def observed():
                with self.lock:
                    self.running += 1
                    if self.running > 1:
                        core.loop.call_soon_threadsafe(self.overlap.set)
                if ordinal == 1:
                    core.loop.call_soon_threadsafe(self.entered.set)
                try:
                    assert self.release.wait(5), "test did not release the file job"
                    return function(*args)
                finally:
                    with self.lock:
                        self.running -= 1

            return await original(core, observed)

        monkeypatch.setattr(file_ops, "run_file_job", run)
        monkeypatch.setattr(component, "run_file_job", run)

    async def assert_serialized(self):
        await asyncio.wait_for(self.attempted.wait(), 2)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(self.overlap.wait(), 0.05)
        finally:
            self.release.set()


def _service_file(hass: HomeAssistant, data: bytes) -> Path:
    directory = Path(hass.config.path("cards"))
    directory.mkdir(exist_ok=True)
    return _file(directory, data)


class TestEditFileService:
    async def test_reports_the_applied_edits(
        self, hass: HomeAssistant, tmp_path: Path
    ) -> None:
        target = _service_file(hass, b"one two\n")
        call = await make_file_service(hass, "cards")

        response = await call(
            component.SERVICE_EDIT_FILE,
            {"path": "cards/card.js", "edits": [_edit("one", "1"), _edit("two", "2")]},
        )

        assert response["success"] is True
        assert response["path"] == "cards/card.js"
        assert response["edits_applied"] == 2
        assert target.read_bytes() == b"1 2\n"

    @pytest.mark.parametrize(
        "bad_token", [True, False], ids=["bad-token", "path-outside-allowlist"]
    )
    async def test_refused_caller_changes_nothing(
        self, hass: HomeAssistant, tmp_path: Path, bad_token: bool
    ) -> None:
        target = _service_file(hass, b"one\n")
        call = await make_file_service(hass, "cards" if bad_token else "allowed")
        data = {"path": "cards/card.js", "edits": [_edit("one", "1")]}
        if bad_token:
            response = await hass.services.async_call(
                component.DOMAIN,
                component.SERVICE_EDIT_FILE,
                {**data, component.CALLER_TOKEN_FIELD: "incorrect-token"},
                blocking=True,
                return_response=True,
            )
        else:
            response = await call(component.SERVICE_EDIT_FILE, data)

        assert response is not None
        assert response["success"] is False
        assert target.read_bytes() == b"one\n"

    @pytest.mark.parametrize("other", ["edit_file", "write_file", "delete_file"])
    async def test_file_changes_do_not_overlap(
        self,
        hass: HomeAssistant,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        other: str,
    ) -> None:
        """An edit's read-modify-write shares the lock with all file mutations."""
        target = _service_file(hass, b"one two\n")
        call = await make_file_service(hass, "cards")
        jobs = _FileJobs(hass, monkeypatch)
        first = asyncio.ensure_future(
            call(
                component.SERVICE_EDIT_FILE,
                {"path": "cards/card.js", "edits": [_edit("one", "1")]},
            )
        )
        await asyncio.wait_for(jobs.entered.wait(), 2)
        data: dict[str, Any] = {"path": "cards/card.js"}
        if other == "edit_file":
            data["edits"] = [_edit("two", "2")]
        elif other == "write_file":
            data.update(content="written\n", overwrite=True)
        following = asyncio.ensure_future(call(other, data))
        await jobs.assert_serialized()
        responses = await asyncio.gather(first, following)

        assert [response["success"] for response in responses] == [True, True]
        if other == "delete_file":
            assert not target.exists()
        else:
            expected = b"1 2\n" if other == "edit_file" else b"written\n"
            assert target.read_bytes() == expected

    async def test_cancelled_call_keeps_the_lock_until_its_thread_ends(
        self, hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cancelled executor caller cannot release the shared write lock early."""
        target = _service_file(hass, b"one two\n")
        jobs = _FileJobs(hass, monkeypatch)
        cancelled = asyncio.create_task(
            file_ops.run_file_job(hass, edit_file_sync, target, [_edit("one", "1")])
        )
        await asyncio.wait_for(jobs.entered.wait(), 2)
        cancelled.cancel()
        following = asyncio.create_task(
            file_ops.run_file_job(hass, edit_file_sync, target, [_edit("two", "2")])
        )
        await jobs.attempted.wait()
        cancelled.cancel()
        await jobs.assert_serialized()
        outcomes = await asyncio.gather(cancelled, following, return_exceptions=True)

        assert isinstance(outcomes[0], asyncio.CancelledError)
        response = outcomes[1]
        assert isinstance(response, dict)
        assert "_error" not in response
        assert target.read_bytes() == b"1 2\n"

    async def test_refusal_tells_the_model_which_edit_failed(
        self, hass: HomeAssistant, tmp_path: Path
    ) -> None:
        _service_file(hass, b"one\n")
        call = await make_file_service(hass, "cards")

        response = await call(
            component.SERVICE_EDIT_FILE,
            {
                "path": "cards/card.js",
                "edits": [_edit("one", "1"), _edit("missing", "x")],
            },
        )

        assert response["success"] is False
        assert response["reason"] == "old_not_found"
        assert response["edit_index"] == 1
        assert "card.js" in response["error"]
