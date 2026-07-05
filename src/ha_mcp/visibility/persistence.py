"""Atomic load/save for entity_visibility.json (mirrors policy/persistence.py)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from .model import VisibilityConfig

VISIBILITY_FILENAME = "entity_visibility.json"


def load_visibility_config(data_dir: Path) -> VisibilityConfig:
    path = data_dir / VISIBILITY_FILENAME
    if not path.exists():
        return VisibilityConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{VISIBILITY_FILENAME} is not valid JSON: {e}") from e
    try:
        return VisibilityConfig.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"{VISIBILITY_FILENAME} failed schema validation: {e}") from e


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
