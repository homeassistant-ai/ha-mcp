"""Atomic load/save for tool_policy.json (mirrors tool_config.json pattern in settings_ui.py)."""

import json
import os
import tempfile
from pathlib import Path

from .model import Policy

POLICY_FILENAME = "tool_policy.json"


def load_policy(data_dir: Path) -> Policy:
    path = data_dir / POLICY_FILENAME
    if not path.exists():
        return Policy()
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"tool_policy.json is not valid JSON: {e}") from e
    return Policy.model_validate(raw)


def save_policy(data_dir: Path, policy: Policy) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / POLICY_FILENAME
    fd, tmp_path = tempfile.mkstemp(prefix=f".{POLICY_FILENAME}.", dir=data_dir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(policy.model_dump(mode="json"), f, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
