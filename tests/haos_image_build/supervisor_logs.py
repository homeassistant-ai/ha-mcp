"""Supervisor's log tail for a failed add-on install during the bake.

Supervisor answers a failed image build with "Check Supervisor logs for
details"; the detail is only in its log, read here through HA's
``/api/hassio/supervisor/logs`` proxy and written to the job log.
"""

from __future__ import annotations

import http.client
import logging
import urllib.request

LOG = logging.getLogger("haos_image_build")


def log_supervisor_tail(base_url: str, access_token: str) -> None:
    """Log the last Supervisor log lines; a failed read only warns."""
    req = urllib.request.Request(
        f"{base_url}/api/hassio/supervisor/logs?lines=200",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            LOG.error("Supervisor log tail:\n%s", resp.read().decode(errors="replace"))
    except (OSError, http.client.HTTPException) as e:
        # URLError/HTTPError/TimeoutError, and a truncated response body.
        LOG.warning("Could not fetch the Supervisor log tail: %r", e)
