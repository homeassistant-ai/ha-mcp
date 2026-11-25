"""Validation helpers for automation actions."""

from typing import Any


def _scene_create_in_choose(action: dict[str, Any]) -> bool:
    """True if any ``choose`` option's sequence contains scene.create."""
    opts = action.get("choose")
    if not isinstance(opts, list):
        return False
    for opt in opts:
        if isinstance(opt, dict) and isinstance(opt.get("sequence"), list):
            for sub in opt["sequence"]:
                if action_contains_scene_create(sub):
                    return True
    return False


def action_contains_scene_create(action: Any) -> bool:
    """True if the action — or any nested action under HA's wrapper keys —
    invokes ``scene.create``.

    Walks the standard wrappers: ``sequence``, ``parallel``, ``choose``
    (with options' inner ``sequence``), ``default``, and the ``then``/
    ``else`` siblings of ``if``. Returns True on the first hit, so a deep
    misroute is caught before the upsert reaches HA.
    """
    if not isinstance(action, dict):
        return False
    # 'service:' is the legacy key, 'action:' the modern HA service-call
    # key (HA 2024.8+). Both reach scene.create.
    if "scene.create" in (action.get("service"), action.get("action")):
        return True
    # Wrappers whose value is a list of nested actions.
    for nested_key in ("sequence", "parallel", "default", "then", "else"):
        nested = action.get(nested_key)
        if isinstance(nested, list):
            for sub in nested:
                if action_contains_scene_create(sub):
                    return True
    return _scene_create_in_choose(action)
