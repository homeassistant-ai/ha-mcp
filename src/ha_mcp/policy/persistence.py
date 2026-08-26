"""Atomic load/save for tool_policy.json (mirrors tool_config.json pattern in settings_ui/__init__.py)."""

import json
import logging
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from ..renamed_tools import RENAMED_TOOLS, current_tool_name
from .model import Policy, Rule

logger = logging.getLogger(__name__)

POLICY_FILENAME = "tool_policy.json"


def load_policy(data_dir: Path) -> Policy:
    path = data_dir / POLICY_FILENAME
    if not path.exists():
        return Policy()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"tool_policy.json is not valid JSON: {e}") from e
    try:
        policy = Policy.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"tool_policy.json failed schema validation: {e}") from e
    return _follow_renamed_tools(policy)


def _follow_renamed_tools(policy: Policy) -> Policy:
    """Point rules naming a retired tool at the name it answers to today.

    A rule is a gate: left on the old name it matches nothing, so a tool the
    user had put behind approval or denial would run ungated after the rename,
    with the rule still visible in the editor as if it applied.

    Re-keying can leave two rules on one tool, and both are kept on purpose:
    gating is additive — ANY matching rule requires approval — so dropping
    either would weaken the gate the user configured. What the duplication does
    decide is ``remember_minutes``, which the middleware reads off the FIRST
    matching rule, so an inherited rule moves behind the rules that already
    name the current tool: the one authored against the tool as it is called
    today is the deliberate one.

    Nothing else moves. An inherited rule with no same-tool counterpart keeps
    its position, so a ``*`` rule that used to sit behind it still does — this
    re-keying is not the place to change which rule answers for a tool that
    only ever had one.
    """
    if not any(rule.tool_name in RENAMED_TOOLS for rule in policy.rules):
        return policy

    resolved: list[tuple[Rule, bool]] = [
        (rule.model_copy(update={"tool_name": current_tool_name(rule.tool_name)}), True)
        if rule.tool_name in RENAMED_TOOLS
        else (rule, False)
        for rule in policy.rules
    ]
    authored: dict[str, int] = {
        rule.tool_name: index
        for index, (rule, inherited) in enumerate(resolved)
        if not inherited
    }

    def anchor(index: int) -> tuple[int, int]:
        rule, inherited = resolved[index]
        if not inherited:
            return (index, 0)
        # Sort right behind the last rule authored on this tool, or stay put.
        return (authored.get(rule.tool_name, index), 1)

    order = sorted(range(len(resolved)), key=anchor)
    return policy.model_copy(update={"rules": [resolved[index][0] for index in order]})


def migrate_policy_any_semantics(data_dir: Path) -> bool:
    """One-time migration of a pre-ANY policy file (PR #1993). Returns True on write.

    The pre-#1993 editor packed every UI condition into ONE rule with the
    predicates AND-ed ("require approval when ALL conditions match"). The
    editor now writes one rule per condition and the UI reads "ANY condition
    matches" — so an untouched old file would silently enforce AND while the
    UI claims ANY. This splits every multi-predicate rule of an UNSTAMPED
    file into one single-predicate rule each (the explicit, documented
    breaking change: old AND conditions become OR, the more-restrictive
    direction) and stamps ``schema_version`` so the migration never re-runs —
    post-upgrade multi-predicate rules (a condition with AND-ed
    sub-parameters, hand-authored or via future UI) are left intact.

    Detection reads the RAW json: the Policy model defaults
    ``schema_version`` to current, which would mask an unstamped file.
    Missing or corrupt files are left alone (load_policy surfaces corruption).

    The whole read-split-save runs under ``config_file_lock()`` so a
    concurrent writer in another process (the stdio settings sidecar) can't
    interleave with the migration's read-modify-write. The blocking lock is
    taken inline: this runs once during server construction, before anything
    is served. The sync entry points construct pre-loop; OAuth/OIDC construct
    on a just-started ``asyncio.run`` loop (``_run_oauth_server`` /
    ``_run_oidc_server``) where the sync constructor cannot await — but no
    other task is scheduled on that loop yet, so a held lock can only delay
    startup, never stall a served client.
    """
    from ..utils.config_write_lock import config_file_lock

    with config_file_lock(data_dir):
        return _migrate_policy_any_semantics_locked(data_dir)


def _migrate_policy_any_semantics_locked(data_dir: Path) -> bool:
    path = data_dir / POLICY_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, json.JSONDecodeError):
        logger.warning("policy migration: cannot read %s; leaving as-is", path)
        return False
    if not isinstance(raw, dict) or raw.get("schema_version") is not None:
        return False
    try:
        policy = Policy.model_validate(raw)
    except ValidationError:
        logger.warning("policy migration: %s failed validation; leaving as-is", path)
        return False
    new_rules: list[Rule] = []
    split = 0
    for rule in policy.rules:
        if len(rule.when) > 1:
            split += 1
            new_rules.extend(
                Rule(
                    tool_name=rule.tool_name,
                    when=[predicate],
                    remember_minutes=rule.remember_minutes,
                )
                for predicate in rule.when
            )
        else:
            new_rules.append(rule)
    save_policy(data_dir, policy.model_copy(update={"rules": new_rules}))
    logger.info(
        "Migrated %s to ANY-match condition semantics: split %d multi-condition "
        "rule(s) into one rule per condition (%d -> %d rules) and stamped "
        "schema_version. Conditions that previously ALL had to match now EACH "
        "gate on their own.",
        path,
        split,
        len(policy.rules),
        len(new_rules),
    )
    return True


def save_policy(data_dir: Path, policy: Policy) -> None:
    # Bump version on every save so optimistic-concurrency callers can
    # detect mid-flight edits (PUT /api/policy/config 409s when the
    # caller's payload version != on-disk version).
    bumped = policy.model_copy(update={"version": policy.version + 1})
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / POLICY_FILENAME
    fd, tmp_path = tempfile.mkstemp(prefix=f".{POLICY_FILENAME}.", dir=data_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(bumped.model_dump(mode="json"), f, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
