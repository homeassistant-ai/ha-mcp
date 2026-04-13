"""Shared config hash utility for optimistic locking.

Used by automation and script tools to detect concurrent modifications.
Dashboard tools (tools_config_dashboards.py) have their own copy — a future
PR could migrate that to use this shared implementation as well.
"""

import hashlib
import json
from typing import Any


def compute_config_hash(config: dict[str, Any]) -> str:
    """Compute a stable hash of a config dict for optimistic locking.

    Uses SHA256 truncated to 16 hex characters (64 bits). Deterministic
    via sorted keys and minimal separators.
    """
    config_str = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(config_str.encode()).hexdigest()[:16]
