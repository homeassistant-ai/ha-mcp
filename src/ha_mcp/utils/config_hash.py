"""Shared config hash utility for optimistic locking.

Used by automation, script, and dashboard tools to detect concurrent modifications.
"""

import hashlib
import json
from typing import Any


def compute_config_hash(config: dict[str, Any]) -> str:
    """Compute a stable hash of a config dict for optimistic locking.

    Uses SHA256 truncated to 16 hex characters (64 bits). Deterministic
    via sorted keys and minimal separators.

    Stability across reads is the caller's contract, not this function's.
    Automations canonicalize via ``_normalize_config_for_roundtrip`` before
    hashing; scripts and dashboards hash raw HA responses, so their
    stability depends on HA returning structurally identical responses on
    consecutive reads (``sort_keys=True`` canonicalizes dict-key order but
    not list-element order). The ``test_config_hash_stable_across_reads``
    E2E tests pin this for each tool family. See issue #980 for the
    contract analysis.
    """
    config_str = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(config_str.encode()).hexdigest()[:16]
