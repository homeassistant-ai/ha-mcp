"""Atomic load/save for entity_visibility.json (mirrors policy/persistence.py)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from .model import VisibilityConfig

VISIBILITY_FILENAME = "entity_visibility.json"

# Parsed-config memo keyed by resolved path, invalidated whenever the file's
# ``(mtime_ns, size)`` changes. ``ha_search`` loads this config up to 3x per
# call (visibility_filter_active / device_registry_needed_for_visibility /
# load_hidden_set); the memo turns the repeat reads into one stat() each. A
# concurrent double-parse across ``asyncio.to_thread`` workers is benign (both
# compute the same value; last write wins).
_CONFIG_MEMO: dict[str, tuple[tuple[int, int], VisibilityConfig]] = {}


def load_visibility_config(data_dir: Path) -> VisibilityConfig:
    path = data_dir / VISIBILITY_FILENAME
    key = str(path)
    try:
        stat = path.stat()
    except OSError:
        # Missing file → disabled default (not an error). Drop any stale memo.
        _CONFIG_MEMO.pop(key, None)
        return VisibilityConfig()
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _CONFIG_MEMO.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{VISIBILITY_FILENAME} is not valid JSON: {e}") from e
    try:
        config = VisibilityConfig.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"{VISIBILITY_FILENAME} failed schema validation: {e}") from e
    _CONFIG_MEMO[key] = (signature, config)
    return config


def save_visibility_config(data_dir: Path, config: VisibilityConfig) -> None:
    bumped = config.model_copy(update={"version": config.version + 1})
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / VISIBILITY_FILENAME
    fd, tmp_path = tempfile.mkstemp(prefix=f".{VISIBILITY_FILENAME}.", dir=data_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(bumped.model_dump(mode="json"), f, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    # Evict so the next load re-stats the just-written file rather than risk a
    # same-signature stale hit (defensive against coarse mtime granularity).
    _CONFIG_MEMO.pop(str(path), None)
