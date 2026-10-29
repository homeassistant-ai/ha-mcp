"""Error-log summaries order Core 2026.11's offset timestamps by instant.

From 2026.11 every log timestamp carries its UTC offset. Compared as text,
the repeated hour when daylight saving ends sorts backwards, and the
offset-less lines logged before the upgrade cannot be placed against them.
"""

import textwrap

from ha_mcp.tools.error_log_parsing import _parse_error_log_structured


def test_daylight_saving_end_keeps_the_later_instant_last() -> None:
    # 01:45 EDT is 05:45Z; the 01:15 EST line after it is 06:15Z.
    log = textwrap.dedent("""        2026-11-01 01:45:00.000-04:00 ERROR (MainThread) [a.b.c] boom
        2026-11-01 01:15:00.000-05:00 ERROR (MainThread) [a.b.c] boom
    """)
    result = _parse_error_log_structured(log)
    assert result["summary"]["window_end"] == "2026-11-01 01:15:00.000-05:00"
    assert result["top_issues"][0]["last_seen"] == "2026-11-01 01:15:00.000-05:00"


def test_lines_from_before_the_upgrade_take_the_logs_offset() -> None:
    # 08:00 before the upgrade, 09:00 after it, both local time at UTC+10.
    log = textwrap.dedent("""        2026-11-02 08:00:00.000 ERROR (MainThread) [a.b.c] boom
        2026-11-02 09:00:00.000+10:00 ERROR (MainThread) [a.b.c] boom
    """)
    summary = _parse_error_log_structured(log)["summary"]
    assert summary["window_start"] == "2026-11-02 08:00:00.000"
    assert summary["window_end"] == "2026-11-02 09:00:00.000+10:00"


def test_a_utc_offset_counts_as_the_logs_offset() -> None:
    # +00:00 is a zero offset, not a missing one: the offset-less line is
    # 10:30Z, not the 09:30Z it would be with the later line's +01:00.
    log = textwrap.dedent("""\
        2026-11-02 10:00:00.000+00:00 ERROR (MainThread) [a.b.c] boom
        2026-11-02 10:30:00.000 ERROR (MainThread) [a.b.c] boom
        2026-11-02 10:15:00.000+01:00 ERROR (MainThread) [a.b.c] boom
    """)
    result = _parse_error_log_structured(log)
    assert result["summary"]["window_end"] == "2026-11-02 10:30:00.000"
    # 09:15Z, though it sorts between the other two as text
    assert result["summary"]["window_start"] == "2026-11-02 10:15:00.000+01:00"
    assert result["top_issues"][0]["first_seen"] == "2026-11-02 10:15:00.000+01:00"
