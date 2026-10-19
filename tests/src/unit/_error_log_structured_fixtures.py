"""Sample log fixtures for the structured error-log tests."""

import textwrap

_SAMPLE_LOG = textwrap.dedent("""\
    2026-05-27 10:00:01.123 ERROR (MainThread) [homeassistant.components.zha.core.device] Device timeout
    2026-05-27 10:00:02.456 ERROR (MainThread) [homeassistant.components.zha.core.device] Device timeout
    2026-05-27 10:00:03.789 WARNING (MainThread) [homeassistant.components.tuya] Couldn't fetch data
    2026-05-27 10:00:04.000 ERROR (MainThread) [homeassistant.components.zha.core.device] Device timeout
    2026-05-27 10:00:05.111 INFO (MainThread) [homeassistant.loader] Loaded component: zha
    2026-05-27 10:00:06.222 ERROR (MainThread) [homeassistant.components.tuya] Auth failed
    2026-05-27 10:00:07.333 DEBUG (MainThread) [homeassistant.core] State changed
    2026-05-27 10:00:08.444 CRITICAL (MainThread) [homeassistant.components.recorder] DB error
""")

_EMPTY_LOG = ""

_UNPARSEABLE_LOG = textwrap.dedent("""\
    Not a valid log line
    Another unparseable line
    2026-05-27 10:00:01 INVALIDLEVEL (thread) [logger] message
""")


def _many_small_issues_log() -> str:
    """`chatty` out-totals `noisy` while every single one of its issues is smaller.

    Both sorts are load-bearing against this fixture:

    * `top_issues` ranks per issue, so `noisy`'s one count-5 issue must come
      first — but `chatty`'s issues are emitted first, so insertion order alone
      gives the opposite answer.
    * `by_component` ranks per component *total*, so `chatty` (10 issues x2 = 20)
      must come first — but it is built by iterating the already-count-sorted
      issues, so insertion order alone again gives the opposite answer.
    """
    lines = [
        f"2026-05-27 10:{i:02d}:{repeat:02d}.000 ERROR (MainThread) "
        f"[chatty.sub.mod] issue {i}"
        for i in range(10)
        for repeat in range(2)
    ]
    lines += [
        f"2026-05-27 11:00:{i:02d}.000 ERROR (MainThread) [noisy.sub.mod] frequent"
        for i in range(5)
    ]
    return "\n".join(lines) + "\n"


_UNSORTED_LOG = _many_small_issues_log()

# Timestamps that neither ascend nor descend, so first-wins/last-wins and
# min/max disagree on both the per-issue bounds and the covered window.
_OUT_OF_ORDER_LOG = textwrap.dedent("""\
    2026-05-27 10:00:09.000 ERROR (MainThread) [a.b.c] boom
    2026-05-27 10:00:01.000 ERROR (MainThread) [a.b.c] boom
    2026-05-27 10:00:05.000 ERROR (MainThread) [a.b.c] boom
""")

# Since Python 3.10 an unnamed thread is called "Thread-N (target_fn)", so the
# thread field of a log line can itself contain parentheses.
_NESTED_PAREN_THREAD_LOG = (
    "2026-05-27 10:00:01.000 ERROR (Thread-4 (_read_loop)) "
    "[pychromecast.socket_client] Connection reset\n"
)

# What Supervisor-backed installs (add-on / HAOS / supervised) actually return:
# HA Core's journald stream, ANSI-coloured. Only container/pip installs read the
# plain file the other fixtures imitate.
_ANSI_LOG = (
    "\x1b[31m2026-05-27 10:00:01.123 ERROR (MainThread) "
    "[homeassistant.components.zha] Device timeout\x1b[0m\n"
    "\x1b[33m2026-05-27 10:00:02.456 WARNING (MainThread) "
    "[homeassistant.components.tuya] Slow response\x1b[0m\n"
    "\x1b[31m2026-05-27 10:00:03.789 ERROR (MainThread) "
    "[homeassistant.components.zha] Device timeout\x1b[0m\n"
)

# A real error followed by its traceback — the traceback lines are unparseable
# by design, which is what makes parsed_entries < total_raw_lines meaningful.
_TRACEBACK_LOG = textwrap.dedent("""\
    2026-05-27 10:00:01.123 ERROR (MainThread) [homeassistant.components.zha] Update failed
    Traceback (most recent call last):
      File "/usr/src/homeassistant/zha.py", line 42, in _async_update
        await self._device.read()
    ValueError: device did not respond
""")
