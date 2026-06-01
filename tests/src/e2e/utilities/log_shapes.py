"""Shared regex patterns for asserting Supervisor / addon log shapes.

Both ``test_addon_lifecycle.py`` (haos_only) and ``test_supervisor_inaddon.py``
(inaddon_only) assert the same journald-style timestamp shape on real
Supervisor log output. Centralising the pattern here:

* keeps the two test modules in sync (a Supervisor migration to a
  different log format only has to update one place), and
* surfaces the assertion intent (proves log content is real journald
  output, not an empty stub or a sentinel string) once at the
  definition site rather than in every test docstring.

Pattern matches the leading ``YYYY-MM-DDTHH:MM:SS`` of journald lines
(e.g. ``2026-05-18T14:23:01.234567+00:00 ha_mcp.tools.... ...``).
Doesn't validate the full RFC3339 / journald shape — the leading
date+time prefix is enough to distinguish real log content from
empty / sentinel responses.
"""

from __future__ import annotations

import re

LOG_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
