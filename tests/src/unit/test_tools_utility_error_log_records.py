"""Record grouping for ``ha_get_logs(source="error_log")``.

Covers #2768: raw reads return whole records (header plus traceback frames),
``search`` selects a record by any line, ``level`` matches the header only,
``limit`` stays line-based without splitting records, and paging resumes before
the oldest returned record. The tool reads Core's own ``/api/error_log`` file
(``_error_log_core.py``) or, for the journald paging tokens, Core's journal
from Supervisor as the app does (``in_app_client`` over ``FakeGatewayd``).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.error_log_journald import parse_cursor_token
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import core_log_reader
from ha_mcp.tools.log_common import MAX_LIMIT, SUPERVISOR_SEARCH_WINDOW_LINES

from ._error_log_core import (
    assert_cursor_resumes_at,
    call_logs,
    error_log,
    numbered_log,
    serve_log,
)
from ._error_log_core import log_file as log_file
from ._error_log_routes_core import load_lines
from ._hassio_core import set_up_hassio
from ._journal_gatewayd import FakeGatewayd
from ._journal_gatewayd import gatewayd as gatewayd
from ._journal_gatewayd import in_app_client as in_app_client
from ._log_tool_calls import set_up_core_logs
from ._tools_addons_core import Supervisor, TargetApp


@pytest.fixture
async def journal_client(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    in_app_client: HomeAssistantClient,
    gatewayd: FakeGatewayd,
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> HomeAssistantClient:
    """Core 2026.12 reads the journal over its own Supervisor client, the app
    directly: both reach the same fake journal."""
    supervisor = Supervisor(TargetApp())
    app = web.Application()
    app.router.add_get("/{path:.+}/logs", gatewayd.handle)
    app.router.add_route("*", "/{path:.*}", supervisor.serve)
    server = await aiohttp_client(app)
    supervisor.url = f"127.0.0.1:{server.port}"
    with monkeypatch.context() as setup_env:
        await set_up_hassio(hass, supervisor, setup_env)
    return in_app_client


_HEADER = (
    "2026-05-27 10:00:01.123 ERROR (MainThread) "
    "[homeassistant.components.zha] Update failed"
)
_FRAMES = (
    "Traceback (most recent call last):\n"
    '  File "/usr/src/homeassistant/zha.py", line 42, in _async_update\n'
    "    await self._device.read()\n"
    "ValueError: device did not respond"
)
_ERROR_RECORD = f"{_HEADER}\n{_FRAMES}"
_WARNING_RECORD = (
    "2026-05-27 10:00:02.456 WARNING (MainThread) "
    "[homeassistant.components.tuya] Fetch failed\n"
    "ValueError: parse error: bad"
)
_CHAINED_RECORD = (
    f"{_HEADER}\n{_FRAMES}\n"
    "\nDuring handling of the above exception, another exception occurred:\n\n"
    "Traceback (most recent call last):\n"
    '  File "/usr/src/homeassistant/zha.py", line 50, in _async_update\n'
    "RuntimeError: retry exhausted"
)


def _filler(count: int) -> list[str]:
    return [
        f"2026-05-27 10:00:00.000 WARNING (MainThread) [a.b.c] filler {i}"
        for i in range(count)
    ]


def _text(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def _issues(log: str) -> list[int]:
    """The ``issue <n>`` numbers of a page, in the order it shows them."""
    return [int(m.group(1)) for m in re.finditer(r"issue (\d+)$", log, re.MULTILINE)]


async def _read(client: HomeAssistantClient, **arguments: Any) -> dict[str, Any]:
    return await call_logs(client, source="error_log", **arguments)


class TestRecordGrouping:
    """A record is a header line and every line up to the next header."""

    async def test_traceback_frames_belong_to_the_header(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(log_file, component_core_client, _ERROR_RECORD)
        assert result["total_records"] == 1
        assert result["total_lines"] == 5
        assert result["log"].splitlines()[0] == _HEADER

    async def test_blank_lines_stay_inside_a_chained_exception(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(log_file, component_core_client, _CHAINED_RECORD)
        assert result["total_records"] == 1
        lines = result["log"].splitlines()
        assert any(not line.strip() for line in lines)
        assert "RuntimeError: retry exhausted" in lines

    async def test_two_headers_split_records(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, component_core_client, f"{_ERROR_RECORD}\n{_WARNING_RECORD}"
        )
        assert result["total_records"] == 2

    async def test_core_2026_11_utc_offset_stamps_start_records(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file,
            component_core_client,
            "2026-10-28 14:45:10.501-04:00 ERROR (MainThread) [a.b] boom\n"
            "  frame\n"
            "2026-10-28 18:45:11.002+00:00 WARNING (Recorder) [c.d] careful\n",
        )
        assert result["total_records"] == 2

    async def test_lines_that_are_not_ha_log_lines_join_the_preceding_record(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """s6-overlay restart banners have no HA header, so they attach.

        The Core journald stream carries them at every restart; treating them
        as a record's continuation lines is the documented behaviour the
        Supervisor path depends on.
        """
        banner = [
            "s6-rc: info: service legacy-services: stopping",
            "[12:00:00] INFO: Home Assistant Core finish process exit code 0",
        ]
        result = await error_log(
            log_file, component_core_client, _text(_ERROR_RECORD.splitlines() + banner)
        )
        assert result["total_records"] == 1
        assert result["log"].splitlines()[-2:] == banner


class TestRecordFiltering:
    async def test_header_search_returns_the_frames(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, component_core_client, _ERROR_RECORD, search="Update failed"
        )
        assert result["total_records"] == 1
        assert result["returned_records"] == 1
        assert "ValueError: device did not respond" in result["log"]
        assert result["log"].splitlines()[0] == _HEADER

    async def test_oldest_keeps_each_records_lines_in_order(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file,
            component_core_client,
            f"{_ERROR_RECORD}\n{_WARNING_RECORD}",
            limit=10,
            order="oldest",
        )
        lines = result["log"].splitlines()
        assert lines[0] == _HEADER
        assert lines.index(_HEADER) < lines.index("ValueError: device did not respond")
        assert lines.index(_WARNING_RECORD.splitlines()[0]) > lines.index(
            "ValueError: device did not respond"
        )

    async def test_frame_search_returns_the_header_and_frames(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file, component_core_client, _ERROR_RECORD, search="ValueError"
        )
        assert result["returned_records"] == 1
        lines = result["log"].splitlines()
        assert lines[0] == _HEADER
        assert lines[-1] == "ValueError: device did not respond"

    async def test_level_matches_the_header_only(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """A frame line containing "Error" must not satisfy level=ERROR."""
        result = await error_log(
            log_file,
            component_core_client,
            f"{_ERROR_RECORD}\n{_WARNING_RECORD}",
            level="ERROR",
        )
        assert result["returned_records"] == 1
        assert _WARNING_RECORD.splitlines()[0] not in result["log"]
        assert "ValueError: parse error: bad" not in result["log"]

    async def test_newest_keeps_each_records_lines_in_order(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        result = await error_log(
            log_file,
            component_core_client,
            f"{_ERROR_RECORD}\n{_WARNING_RECORD}",
            limit=10,
        )
        assert result["log"].splitlines()[0] == _WARNING_RECORD.splitlines()[0]
        assert result["log"].index("ValueError: parse error: bad") > (
            result["log"].index(_WARNING_RECORD.splitlines()[0])
        )
        # The traceback record follows whole, header before its frames.
        assert result["log"].index(_HEADER) < result["log"].index(
            "ValueError: device did not respond"
        )

    async def test_system_search_matches_the_exception_text(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await set_up_core_logs(hass)
        try:
            raise ValueError("UndefinedError: template failed")
        except ValueError:
            logging.getLogger("homeassistant.components.template").exception(
                "Render failed"
            )

        result = await call_logs(core_client, source="system", search="UndefinedError")

        assert result["returned_entries"] == 1


class TestRecordLimitAndPaging:
    async def test_limit_never_splits_a_record(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        older = (
            "2026-05-27 10:00:00.000 INFO (MainThread) [a.b.c] first\n"
            "  continuation one\n"
            "  continuation two"
        )
        newer = (
            "2026-05-27 10:00:03.000 ERROR (MainThread) [a.b.c] second\n"
            "  frame one\n"
            "  frame two"
        )
        # History behind the window, so the page has a next one.
        result = await error_log(
            log_file,
            component_core_client,
            f"{numbered_log(300)}{older}\n{newer}\n",
            limit=3,
        )
        assert result["returned_records"] == 1
        assert result["returned_lines"] == 3
        assert result["log"].startswith(newer.splitlines()[0])
        assert "frame two" in result["log"]
        # Resume just before the older record, not inside the newer one.
        await assert_cursor_resumes_at(component_core_client, result, 3)

    async def test_over_long_record_is_cut_with_a_larger_limit_hint(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        frames = "\n".join(f"  frame {i}" for i in range(8)) + "\nValueError: NEEDLE"
        result = await error_log(
            log_file, component_core_client, f"{_HEADER}\n{frames}", limit=3
        )
        assert result["returned_lines"] == 3
        assert result["returned_records"] == 1
        # The header names the logger and message; the exception line is the
        # match the caller filtered for. Both ends survive the cut.
        assert result["log"].splitlines()[0] == _HEADER
        assert result["log"].splitlines()[-1] == "ValueError: NEEDLE"
        warning = next(w for w in result["warnings"] if "newest record" in w)
        assert "10 lines" in warning
        assert "cut 7" in warning
        assert "from the middle" in warning
        assert "limit=10" in warning

    async def test_over_long_record_at_max_limit_names_the_per_call_cap(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        frames = "\n".join(f"  frame {i}" for i in range(MAX_LIMIT + 99))
        result = await error_log(
            log_file, component_core_client, f"{_HEADER}\n{frames}", limit=MAX_LIMIT
        )
        assert result["returned_lines"] == MAX_LIMIT
        warning = next(w for w in result["warnings"] if "newest record" in w)
        assert f"per-call maximum ({MAX_LIMIT})" in warning
        assert "100 lines were cut" in warning

    async def test_cut_record_with_limit_one_keeps_only_the_header(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        frames = "\n".join(f"  frame {i}" for i in range(4))
        result = await error_log(
            log_file, component_core_client, f"{_HEADER}\n{frames}", limit=1
        )
        assert result["returned_lines"] == 1
        assert result["log"] == _HEADER

    async def test_a_record_longer_than_the_window_returns_as_a_partial(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """A tail whose header is more than a window away must not be withheld.

        Holding it back cannot help: the next window opens inside the same
        record again, and the full-window step would skip the lines the caller
        matched. It comes back as a partial record with a warning instead, and
        the counts include it.
        """
        frames = "\n".join(
            f"  frame {i}" for i in range(SUPERVISOR_SEARCH_WINDOW_LINES + 100)
        )
        result = await error_log(
            log_file,
            component_core_client,
            f"{_HEADER}\n{frames}\nValueError: NEEDLE\n",
            limit=100,
            search="NEEDLE",
        )

        assert result["returned_records"] == 1
        assert result["total_records"] == 1
        assert result["total_lines"] >= result["returned_lines"] > 0
        assert result["has_more"] is True
        await assert_cursor_resumes_at(
            component_core_client, result, SUPERVISOR_SEARCH_WINDOW_LINES
        )
        assert any(
            "longer than one fetch window" in warning for warning in result["warnings"]
        )
        assert any("partial record" in warning for warning in result["warnings"])

    async def test_a_level_read_held_back_does_not_claim_a_match(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """A level read cannot know the withheld tail matches, so say so.

        The level lives in the missing header; the warning must talk about the
        window's edge, not about a matching record.
        """
        filler = _filler(3970)
        record = [_HEADER] + [f"  frame {i}" for i in range(40)]
        lines = filler[:1985] + record + filler[1985:]

        result = await error_log(
            log_file, component_core_client, _text(lines), limit=20, level="ERROR"
        )

        warning = next(w for w in result["warnings"] if "held back" in w)
        assert "at this window's edge" in warning
        assert "matching" not in warning

    async def test_a_cut_leading_partial_is_held_back_with_consistent_counts(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """A held-back tail must not be returned cut while counts drop it.

        Cutting a record builds a new one, so an identity-based hold-back left
        the cut copy on the page: the response said `returned_records=1` while
        `total_records` and `total_lines` counted nothing. The next page must
        also still reach the header.
        """
        filler = _filler(3970)
        record = (
            [_HEADER] + [f"  frame {i}" for i in range(40)] + ["ValueError: NEEDLE"]
        )
        await serve_log(log_file, _text(filler[:1985] + record + filler[1985:]))

        first = await _read(component_core_client, limit=5, search="NEEDLE")

        assert first["returned_records"] == first["returned_lines"] == 0
        assert first["total_records"] == first["total_lines"] == 0
        assert any("held back" in warning for warning in first["warnings"])
        assert first["has_more"] is True

        second = await _read(
            component_core_client, limit=5, search="NEEDLE", offset=first["next_offset"]
        )
        assert second["returned_records"] == 1
        assert second["log"].splitlines()[0] == _HEADER

    async def test_a_journald_page_token_resumes_behind_its_oldest_returned_line(
        self, gatewayd: FakeGatewayd, journal_client: HomeAssistantClient
    ) -> None:
        """A page that left part of its window behind must not jump to the top.

        The token in ``next_offset`` and in the hint names the window's first
        cursor plus the entries this page consumed, so the next page is the
        exact unreturned remainder instead of a restart at the newest entry.
        """
        load_lines(gatewayd, numbered_log(300).splitlines())

        first = await _read(journal_client, limit=5)
        token = first["next_offset"]
        assert isinstance(token, str)
        assert f"offset='{token}'" in first["pagination_hint"]
        second = await _read(journal_client, limit=5, offset=token)

        assert _issues(first["log"]) == [299, 298, 297, 296, 295]
        assert _issues(second["log"]) == [294, 293, 292, 291, 290]

    async def test_a_held_back_tail_is_reached_by_the_journald_token(
        self, gatewayd: FakeGatewayd, journal_client: HomeAssistantClient
    ) -> None:
        """A held-back tail must be the next page's newest edge, not skipped.

        The page hands out a token whose skip lands on the tail's last line, so
        the next block still reaches the record's header; anchoring on the
        window's first entry instead would return nothing that matches.
        """
        filler = _filler(4000)
        record = [_HEADER] + [f"  frame {i}" for i in range(1, 30)]
        record[-1] = "ValueError: NEEDLE"
        # The record opens just before the 2,000-entry filtered window, so the
        # window starts inside it: its matching tail is what page 1 must hold
        # back, and there is enough history behind it that the next block is
        # not clamped to the journal's start.
        load_lines(gatewayd, filler[:1999] + record + filler[2029:])

        first = await _read(journal_client, limit=5, search="NEEDLE")

        assert first["returned_records"] == first["returned_lines"] == 0
        assert any("held back" in warning for warning in first["warnings"])
        assert first["has_more"] is True
        assert isinstance(first["next_offset"], str)

        second = await _read(
            journal_client, limit=5, search="NEEDLE", offset=first["next_offset"]
        )

        assert second["returned_records"] == 1
        assert second["log"].splitlines()[0] == _HEADER
        assert "ValueError: NEEDLE" in second["log"]

    async def test_journal_entries_holding_a_carriage_return_page_once_each(
        self, gatewayd: FakeGatewayd, journal_client: HomeAssistantClient
    ) -> None:
        entries = [
            f"2026-05-27 10:00:00.000 ERROR (MainThread) [a.b.c] got OK\rtail {index}"
            for index in range(450)
        ]
        load_lines(gatewayd, entries)
        seen: list[str] = []
        arguments: dict[str, Any] = {"limit": 100}
        for _ in range(12):
            page = await _read(journal_client, **arguments)
            assert page["returned_lines"] == page["returned_records"]
            seen += page["log"].split("\n")
            if not page["has_more"]:
                break
            arguments["offset"] = page["next_offset"]
        assert seen == entries[::-1]

    async def test_a_window_longer_than_one_records_call_is_grouped_whole(
        self,
        gatewayd: FakeGatewayd,
        journal_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The component parses record headers from text of a bounded length."""
        entries = numbered_log(300).splitlines()
        load_lines(gatewayd, entries)
        monkeypatch.setattr(core_log_reader, "RECORDS_CHARS", 500)

        summary = (await _read(journal_client, structured=True))["summary"]

        assert summary["parsed_entries"] == summary["unique_issues"] == len(entries)

    async def test_a_structured_journald_read_round_trips_its_token(
        self, gatewayd: FakeGatewayd, journal_client: HomeAssistantClient
    ) -> None:
        """The summary consumes its whole window, so its token resumes behind it.

        With the integer step the next page re-counted part of this window when
        lines arrived in between; the token is exact and the next structured
        call accepts it.
        """
        entries = numbered_log(2510).splitlines()
        load_lines(gatewayd, entries)

        first = await _read(journal_client, structured=True)
        assert first["has_more"] is True
        token = first["next_offset"]
        assert isinstance(token, str), "a journald summary must hand out a token"
        assert parse_cursor_token(token)[1] == 0

        second = await _read(journal_client, structured=True, offset=token)

        # The two windows partition the journal: the integer step overlapped
        # them, so their line counts also double-counted at the boundary.
        assert not second["has_more"]
        assert first["summary"]["total_raw_lines"] + second["summary"][
            "total_raw_lines"
        ] == len(entries)

    async def test_a_journald_walk_through_partial_pages_is_contiguous(
        self, gatewayd: FakeGatewayd, journal_client: HomeAssistantClient
    ) -> None:
        """A token walk returns every entry exactly once, head edge included.

        The journal is sized so the last resume point falls inside the block
        gatewayd clamps to the journal's head: that block then runs past the
        resume point, and returning it whole repeats entries while hiding the
        oldest ones behind them.
        """
        load_lines(gatewayd, numbered_log(900).splitlines())

        seen: list[int] = []
        offset: int | str = 0
        pages = 0
        partial_pages = 0
        for _ in range(20):
            page = await _read(journal_client, limit=300, offset=offset)
            pages += 1
            seen.extend(_issues(page["log"]))
            if not page["has_more"]:
                break
            offset = page["next_offset"]
            assert isinstance(offset, str), "journald paging must hand out a token"
            if parse_cursor_token(offset)[1]:
                partial_pages += 1
        else:
            raise AssertionError("journald walk did not reach the end of history")

        assert pages >= 2, "the journal must force more than one page"
        assert partial_pages >= 1, "the walk must cross a page that left its window"
        assert seen == list(range(899, -1, -1)), (
            "the walk must return every entry exactly once, newest first"
        )

    async def test_a_filtered_head_window_with_more_matches_than_the_cap_is_exhaustive(
        self, gatewayd: FakeGatewayd, journal_client: HomeAssistantClient
    ) -> None:
        """Matches behind a token page at the head must stay pageable.

        The head window holds more matches than one response may carry, and
        raising ``limit`` cannot reach them because the window's count exceeds
        the cap. The token has to keep the walk going until every match comes
        back exactly once.
        """
        load_lines(gatewayd, numbered_log(2600).splitlines())

        seen: list[int] = []
        offset: int | str = 0
        pages = 0
        for _ in range(30):
            page = await _read(journal_client, limit=300, search="issue", offset=offset)
            pages += 1
            seen.extend(_issues(page["log"]))
            if not page["has_more"]:
                break
            offset = page["next_offset"]
            assert isinstance(offset, str), "the head window must hand out a token"
        else:
            raise AssertionError("the filtered head window did not finish paging")

        assert pages > 2, "the journal must force several pages"
        assert seen == list(range(2599, -1, -1)), (
            "every match in the head window must come back exactly once"
        )

    async def test_numeric_string_offsets_keep_working(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """The offset union admits strings; a stringified integer still pages."""
        await serve_log(log_file, numbered_log(100))

        seven = await _read(component_core_client, limit=1, offset="7")
        blank = await _read(component_core_client, limit=1, offset="")

        assert _issues(seven["log"]) == [92]
        assert _issues(blank["log"]) == [99]

    @pytest.mark.parametrize(
        "offset",
        [
            pytest.param("-2", id="negative"),
            pytest.param("not-a-token", id="not-a-token"),
            # The pre-skip token format cannot name a resume point.
            pytest.param("cursor:s=abc;i=1", id="token-without-a-skip"),
        ],
    )
    async def test_an_offset_that_names_no_page_is_refused(
        self, log_file: Path, component_core_client: HomeAssistantClient, offset: str
    ) -> None:
        await serve_log(log_file, _ERROR_RECORD)
        with pytest.raises(ToolError):
            await _read(component_core_client, offset=offset)

    async def test_an_unmatched_leading_partial_is_not_held_back(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """Only a matching leading tail may be withheld.

        Otherwise a search that hits a newer record would emit a held-back
        warning and re-anchor the step on a record it never matched, skipping
        matches that did not fit the page.
        """
        lines = _filler(2100)
        # A long record straddling the window's oldest edge: the window opens
        # inside it, so it forms a leading partial that matches nothing.
        long_start = 85
        for i in range(30):
            lines[long_start + i] = f"  frame {i}"
        lines[long_start] = _HEADER
        lines[2050] = "2026-05-27 10:00:00.000 ERROR (MainThread) [a.b.c] target"
        lines.insert(2051, "ValueError: TARGET")

        result = await error_log(
            log_file, component_core_client, _text(lines), limit=5, search="TARGET"
        )

        assert result["returned_records"] == 1
        assert not any("held back" in warning for warning in result.get("warnings", []))
        assert "TARGET" in result["log"]

    async def test_a_filtered_read_is_exhaustive_through_the_terminal_hint(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """A filtered read must reach every match, not just a page or two.

        A filtered window is fixed at the search-window bound, so matches left
        inside a terminal window are reachable only by raising the limit. With
        the whole matching set inside one hinted call, the union has to cover
        every match exactly once.
        """
        records = [
            f"2026-05-27 10:00:00.000 ERROR (MainThread) [a.b.c] issue {i}\n"
            f"  frame {i}.0"
            for i in range(200)
        ]
        await serve_log(log_file, "\n".join(records) + "\n")

        first = await _read(component_core_client, limit=20, search="issue")
        assert first["has_more"] is False
        first_seen = set(_issues(first["log"]))
        hint = first.get("pagination_hint") or ""
        match = re.search(r"limit=(\d+)", hint)
        assert match, f"terminal hint must name an actionable limit: {hint}"

        final = await _read(
            component_core_client, limit=int(match.group(1)), search="issue", offset=0
        )
        final_seen = set(_issues(final["log"]))

        assert final_seen >= first_seen
        assert len(final_seen) == len(records)

    async def test_walking_a_filtered_read_keeps_matches_in_order(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """A filtered walk must keep cross-page ordering, not just uniqueness.

        It runs until the terminal window (``has_more`` false) rather than a
        fixed page count; matches left inside that window are the raised-limit
        contract, covered separately by the exhaustiveness test.
        """
        records = [
            "2026-05-27 10:00:00.000 WARNING (MainThread) [a.b.c] filler\n"
            f"2026-05-27 10:00:00.000 ERROR (MainThread) [a.b.c] issue {i}\n"
            f"  frame {i}.0\n  frame {i}.1\n  frame {i}.2"
            for i in range(600)
        ]
        lines = "\n".join(records).splitlines()
        # 3,000 lines: larger than the 2,000-line filtered window, so the walk
        # has to cross pages for the ordering assertion to mean anything.
        assert len(lines) > SUPERVISOR_SEARCH_WINDOW_LINES
        await serve_log(log_file, _text(lines))

        seen: list[int] = []
        offset = 0
        pages = 0
        for _ in range(200):
            page = await _read(
                component_core_client, limit=20, offset=offset, search="issue"
            )
            pages += 1
            if page["returned_lines"]:
                assert page["log"].splitlines()[0].startswith("2026-")
            seen.extend(int(n) for n in re.findall(r"issue (\d+)", page["log"]))
            if not page["has_more"]:
                break
            offset = page["next_offset"]
        else:
            raise AssertionError("filtered walk did not reach a terminal window")

        assert pages >= 2, "the log must force more than one page"
        assert seen, "the filtered walk returned no matches"
        assert len(seen) == len(set(seen)), "a filtered match repeated across pages"
        assert seen == sorted(seen, reverse=True), (
            "filtered paging must keep newest-first order across pages"
        )

    async def test_leading_partial_is_widened_to_reach_its_header(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        log = f"{_ERROR_RECORD}\n" + "\n".join(f"  frame {i}" for i in range(400))

        with patch.object(
            component_core_client,
            "send_websocket_message",
            wraps=component_core_client.send_websocket_message,
        ) as reads:
            result = await error_log(
                log_file, component_core_client, log + "\n", limit=5
            )

        first, second = (
            call.args[0]["lines"]
            for call in reads.await_args_list
            if call.args[0]["type"] == "ha_mcp_tools/core_log_window"
        )
        assert second > first
        assert result["log"].splitlines()[0] == _HEADER

    async def test_a_window_that_reaches_history_start_is_marked_not_refetched(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """A partial tail with no older history is marked, not widened into.

        On a container install every widening downloads the whole file again.
        """
        with patch.object(
            component_core_client,
            "send_websocket_message",
            wraps=component_core_client.send_websocket_message,
        ) as reads:
            result = await error_log(
                log_file,
                component_core_client,
                "  frame\nValueError: device did not respond\n",
                limit=5,
            )

        assert (
            sum(
                call.args[0]["type"] == "ha_mcp_tools/core_log_window"
                for call in reads.await_args_list
            )
            == 1
        )
        assert any("partial record" in w for w in result["warnings"])
        assert result["log"].splitlines()[0] == "  frame"

    async def test_a_failed_widening_keeps_the_first_page(
        self,
        gatewayd: FakeGatewayd,
        journal_client: HomeAssistantClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        load_lines(gatewayd, [_HEADER] + [f"  frame {i}" for i in range(400)])

        def refuse_after_the_first_page(_range: str) -> None:
            # The first page is its window and the probe behind it.
            if len(gatewayd.ranges) == 2:
                gatewayd.rejection = "journal unavailable"

        gatewayd.after_each = refuse_after_the_first_page
        with caplog.at_level("WARNING", logger="ha_mcp.tools.log_sources"):
            result = await _read(journal_client, limit=5)

        assert any("partial record" in w for w in result["warnings"])
        assert any("wider read" in w and "failed" in w for w in result["warnings"])
        assert not any(
            "lies outside the fetched history" in w for w in result["warnings"]
        )
        assert result["log"].splitlines()[0].startswith("  frame")
        assert any(
            "wider read for a record header failed" in record.message
            for record in caplog.records
        )

    @pytest.mark.parametrize(
        ("filter_name", "value"), [("search", "NEEDLE"), ("level", "ERROR")]
    )
    async def test_a_filtered_page_held_back_from_mid_record_returns_it_whole(
        self,
        log_file: Path,
        component_core_client: HomeAssistantClient,
        filter_name: str,
        value: str,
    ) -> None:
        """A filtered window that opens inside a record must not split it.

        The filtered window is already the search-window bound, so the widening
        refetch cannot grow it; the tail has to be left off this page and the
        next page re-opened at its last line, or the record's header is lost
        (and a `level` read drops the tail silently).
        """
        filler = _filler(3985)
        long_record = (
            [_HEADER] + [f"  frame {i}" for i in range(40)] + ["ValueError: NEEDLE"]
        )
        await serve_log(log_file, _text(filler[:1990] + long_record + filler[1990:]))

        first = await _read(component_core_client, limit=20, **{filter_name: value})

        # The tail is held back, so page 1 cannot present it as a partial.
        assert first["returned_records"] == 0
        assert not any("partial record" in w for w in first.get("warnings", []))
        assert first["has_more"] is True

        second = await _read(
            component_core_client,
            limit=20,
            offset=first["next_offset"],
            **{filter_name: value},
        )
        assert second["returned_records"] == 1
        assert second["log"].splitlines()[0] == _HEADER
        assert second["log"].splitlines()[-1] == "ValueError: NEEDLE"

    async def test_walking_next_offset_never_repeats_a_record(
        self, log_file: Path, component_core_client: HomeAssistantClient
    ) -> None:
        """Following the hint pages backwards without repeats or split records."""
        records = [
            f"2026-05-27 10:00:00.000 ERROR (MainThread) [a.b.c] issue {i}\n"
            f"  frame {i}.0\n  frame {i}.1"
            for i in range(700)
        ]
        await serve_log(log_file, "\n".join(records) + "\n")

        seen: list[int] = []
        page_seen: list[int] = []
        offset = 0
        last: dict[str, Any] = {}
        for _ in range(40):
            last = await _read(component_core_client, limit=100, offset=offset)
            if last["returned_lines"]:
                assert (
                    last["log"]
                    .splitlines()[0]
                    .startswith("2026-05-27 10:00:00.000 ERROR")
                ), "every page must open on a record header"
            page_seen = [int(n) for n in re.findall(r"issue (\d+)", last["log"])]
            seen.extend(page_seen)
            if not last["has_more"]:
                break
            offset = last["next_offset"]

        assert len(seen) == len(set(seen)), "a record must not be returned twice"
        assert seen == sorted(seen, reverse=True), (
            "newest-first paging must keep records in descending order"
        )

        if len(seen) < len(records):
            # The terminal window cannot page further; its hint has to make the
            # remainder reachable, and one call at the hinted limit must prove
            # that no record is silently lost.
            hint = last.get("pagination_hint") or ""
            match = re.search(r"limit=(\d+)", hint)
            assert match, f"terminal hint must name an actionable limit: {hint}"
            final = await _read(
                component_core_client, limit=int(match.group(1)), offset=offset
            )
            final_seen = [int(n) for n in re.findall(r"issue (\d+)", final["log"])]
            assert final["has_more"] is False
            # The hinted request re-reads the terminal window with a wider
            # limit, so it must include everything that window already showed.
            assert set(final_seen) >= set(page_seen)
            assert len(set(seen) | set(final_seen)) == len(records), (
                "the terminal hint must make every remaining record reachable"
            )
