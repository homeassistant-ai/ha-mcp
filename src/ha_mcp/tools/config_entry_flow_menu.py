"""Menu-step handling for config-entry flows.

Extracted from ``config_entry_flow.py`` (which holds the public create/update
entry points) when that module crossed the ~1000-line split threshold. This is
the base of the flow machinery's import chain — it owns the menu selection keys
and depends on nothing else in the package; ``config_entry_flow_form`` and
``config_entry_flow_walker`` import from here, never the reverse.
"""

import json
from typing import Any, NoReturn

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error

# Keys used to specify a menu selection — stripped before submitting form data.
# The tuple fixes the multi-key precedence (frozenset iteration order is
# hash-randomized per process, so consuming in set order would make a config
# carrying two selection keys nondeterministic). The membership frozenset
# built from it (``_MENU_SELECTION_KEYS``) lives in config_entry_flow_form,
# its heaviest user. Mirrored by ``_MENU_CHOICE_CONFIG_KEYS`` in
# tools_config_helpers.py.
_MENU_SELECTION_KEY_ORDER = ("group_type", "next_step_id", "menu_option")


def _handle_menu_step(
    flow_id: str,
    current_step: dict[str, Any],
    remaining_config: dict[str, Any],
    consumed_selections: list[str] | None = None,
    consumed_selection_keys: list[str] | None = None,
) -> str:
    """Extract menu selection from config, raising on missing selection.

    Returns the menu choice string. Mutates remaining_config: a scalar
    selection is popped outright; a list of successive selections yields one
    element per menu encounter, the un-consumed tail replacing the key until
    it runs dry. Lists let flows that revisit a menu (issue #2116:
    battery_sim loops menu → branch form → menu until 'all_done') be walked
    within the step budget the walkers derive from the selection count. The
    caller's list object is never mutated.

    ``consumed_selections`` (when provided by the walker) accumulates every
    selection consumed during the walk, so a menu revisited after the
    selections ran dry can raise an error that names what was already
    consumed instead of claiming no selection was supplied;
    ``consumed_selection_keys`` records which selection key served each one,
    so that error's example uses the caller's own key.
    """
    menu_choice = None
    for key in _MENU_SELECTION_KEY_ORDER:
        if key not in remaining_config:
            continue
        value = remaining_config[key]
        if isinstance(value, list):
            if not value:
                # Empty list = no selection supplied under this key; another
                # selection key may still carry one.
                remaining_config.pop(key)
                continue
            menu_choice = value[0]
            if not menu_choice:
                # A falsy element mid-list is a malformed call, not an
                # exhausted list — without this check it would raise the
                # "ran dry" error while later selections sit unconsumed,
                # pointing the caller at a problem they don't have.
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Menu selection list under '{key}' contains an "
                        f"empty element ({menu_choice!r}) — every element "
                        "must be a menu option name.",
                        suggestions=[
                            f"Available options: "
                            f"{current_step.get('menu_options', [])}",
                        ],
                        context={
                            "flow_id": flow_id,
                            "step_id": current_step.get("step_id"),
                            "selection_key": key,
                        },
                    )
                )
            rest = list(value[1:])
            if rest:
                remaining_config[key] = rest
            else:
                remaining_config.pop(key)
        else:
            menu_choice = remaining_config.pop(key)
        break

    if not menu_choice:
        _raise_missing_menu_selection(
            flow_id, current_step, consumed_selections, consumed_selection_keys
        )

    choice = str(menu_choice)
    if consumed_selections is not None:
        consumed_selections.append(choice)
    if consumed_selection_keys is not None:
        consumed_selection_keys.append(key)
    return choice


def _raise_missing_menu_selection(
    flow_id: str,
    current_step: dict[str, Any],
    consumed_selections: list[str] | None,
    consumed_selection_keys: list[str] | None,
) -> NoReturn:
    """Raise the no-selection error for a menu step.

    Distinguishes a revisited menu whose selections ran dry (name what was
    consumed, show the list syntax under the caller's own key) from a first
    menu given no selection at all (the original guidance).
    """
    menu_options = current_step.get("menu_options", [])
    context = {
        "flow_id": flow_id,
        "step_id": current_step.get("step_id"),
        "menu_options": menu_options,
    }
    if consumed_selections:
        # "<next-selection>" is a placeholder on purpose: cyclic flows
        # routinely need the SAME option again (battery_sim's create flow
        # picks 'no_tariff_info' once per meter), so no concrete option
        # can be suggested without guessing the caller's path.
        example = json.dumps([*consumed_selections, "<next-selection>"])
        example_key = (
            consumed_selection_keys[-1] if consumed_selection_keys else "next_step_id"
        )
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_MISSING_REQUIRED_FIELDS,
                "Flow presented another menu after the supplied "
                f"selection(s) ({', '.join(map(repr, consumed_selections))}) "
                "were consumed. Pass your menu selection key "
                "('next_step_id', 'group_type', or 'menu_option') as a "
                "list of successive selections to drive flows that "
                "revisit menus.",
                suggestions=[
                    f"Available options: {menu_options}",
                    f'Example: {{"{example_key}": {example}}}',
                ],
                context={
                    **context,
                    "consumed_menu_selections": list(consumed_selections),
                },
            )
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.CONFIG_MISSING_REQUIRED_FIELDS,
            "Menu step requires a selection. "
            "Add 'group_type' or 'next_step_id' to your config.",
            suggestions=[
                f"Available options: {menu_options}",
                'Example: {"group_type": "light", "name": "My Group", ...}',
            ],
            context=context,
        )
    )


def _flow_step_budget(config: dict[str, Any]) -> int:
    """Step allowance for one flow walk.

    The historical cap of 10 guarded against runaway flows, but a cyclic
    flow driven by an N-item selection list legitimately costs about two
    steps per selection (the menu plus the branch step it opens) —
    battery_sim's create flow spends 8 of the 10 on a minimal two-meter
    setup, and one fixed-tariff branch more would starve it. Grant every
    caller-supplied selection its two steps on top of the base; the budget
    stays bounded by the caller's own input, so the runaway protection is
    intact.
    """
    selections = 0
    for key in _MENU_SELECTION_KEY_ORDER:
        value = config.get(key)
        if isinstance(value, list):
            selections += len(value)
        elif value is not None:
            selections += 1
    return 10 + 2 * selections
