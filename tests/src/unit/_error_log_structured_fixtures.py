"""The sample log the old error-log helpers read (``_error_log_test_helpers``)."""

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
