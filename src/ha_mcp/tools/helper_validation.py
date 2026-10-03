"""Input validation for ha_config_set_helper parameters."""

from typing import Any

from ..errors import ErrorCode, create_error_response
from .config_entry_flow import FLOW_HELPER_TYPES
from .helper_flow import _flow_helper_error_context
from .helper_schemas import (
    _ALL_TYPED_PARAMS,
    _TYPE_TYPED_PARAMS,
    SIMPLE_HELPER_TYPES,
    _simple_helper_error_context,
)
from .helpers import raise_tool_error, validate_identifier_not_empty

# Bug 6 (issue #1150): valid mode values per helper type. The CREATE and
# UPDATE branches both validate against this; an invalid value is rejected
# instead of silently coerced to HA's default.
_MODE_BY_TYPE: dict[str, tuple[str, ...]] = {
    "input_number": ("box", "slider"),
    "input_text": ("text", "password"),
}


def _validate_mode(helper_type: str, mode: str | None) -> None:
    """Reject an invalid `mode` value for the chosen helper_type (Bug 6)."""
    if mode is None:
        return
    allowed = _MODE_BY_TYPE.get(helper_type)
    if allowed is None or mode in allowed:
        return
    options = " or ".join(repr(m) for m in allowed)
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"mode={mode!r} is not valid for {helper_type}. Use {options}.",
            context=_simple_helper_error_context(helper_type, mode=mode),
            suggestions=[f"Pass mode={allowed[0]!r} or mode={allowed[1]!r}"],
        )
    )


def _validate_applicable_params(
    helper_type: str,
    passed: dict[str, Any],
) -> None:
    """Reject typed parameters that don't apply to the chosen helper_type.

    Bug 4b/7c/10/14 (issue #1150): the function signature accepts ~30 typed
    parameters, but each helper_type only legitimately uses 5-10 of them.
    Previously, inapplicable params were silently ignored. Now we raise
    VALIDATION_INVALID_PARAMETER so the caller sees their request was not
    handled, instead of getting `success: true` with the param dropped.

    `passed` is a dict of param_name -> value as the caller provided. None
    values are treated as "not passed" and skipped.
    """
    inapplicable: list[str] = []

    if helper_type in FLOW_HELPER_TYPES:
        # Flow types accept `config` (handled before this call) plus
        # cross-cutting params (name/helper_id/area_id/labels/category/icon/wait).
        # `icon` is applied to the resulting entity(ies) via the entity registry
        # (like area_id/labels), so it is allowed even though the config-flow
        # form has no icon field. Any other simple-helper-typed param passed
        # here is inapplicable.
        inapplicable.extend(
            param_name
            for param_name in _ALL_TYPED_PARAMS
            if param_name != "icon" and passed.get(param_name) is not None
        )
    else:
        applicable = _TYPE_TYPED_PARAMS.get(helper_type, frozenset())
        for param_name, value in passed.items():
            if value is None:
                continue
            if param_name in applicable:
                continue
            inapplicable.append(param_name)

    if not inapplicable:
        return

    inapplicable.sort()
    if helper_type in FLOW_HELPER_TYPES:
        applicable_msg = (
            "config (see data_schema on a validation error for the field set), "
            "name, helper_id, area_id, labels, category, icon, wait"
        )
    else:
        type_specific = sorted(_TYPE_TYPED_PARAMS.get(helper_type, frozenset()))
        type_specific_str = (
            ", ".join(type_specific) if type_specific else "(only name/icon)"
        )
        applicable_msg = (
            f"{type_specific_str}; plus name, helper_id, area_id, labels, "
            f"category, wait"
        )

    suggestions = [
        f"Remove these params for helper_type='{helper_type}': "
        f"{', '.join(inapplicable)}",
    ]
    if helper_type == "person" and "icon" in inapplicable:
        suggestions.append("Person entities use 'picture' (a URL), not 'icon'.")
    if helper_type == "tag" and "icon" in inapplicable:
        suggestions.append("Tags do not support icons.")
    if helper_type in FLOW_HELPER_TYPES:
        suggestions.append(
            f"For flow-based helpers like {helper_type!r}, type-specific config "
            "goes inside the `config` dict; submit with the wrong shape once "
            "and the validation error returns the `data_schema` for that helper."
        )

    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"The following parameters are not applicable for "
            f"helper_type='{helper_type}': {', '.join(inapplicable)}. "
            f"Applicable parameters: {applicable_msg}.",
            context={
                "helper_type": helper_type,
                "inapplicable_params": inapplicable,
            },
            suggestions=suggestions,
        )
    )


def _validate_numeric_range(
    helper_type: str,
    min_value: float | None,
    max_value: float | None,
    step: float | None,
) -> None:
    """Pre-validate min/max/step ranges for numeric simple helpers.

    Bug 13 (issue #1150): HA rejects several edge cases with cryptic messages
    (or, in the slider-step-too-large case, silently produces a broken
    slider). Surface clear, type-aware errors to the caller before the WS
    round-trip.

    Applies to: input_number (float), counter (int), input_text (length).
    For input_text, min/max are character lengths; values must be in [0, 255]
    and follow the standard min<max strict ordering.
    """
    if helper_type == "input_text":
        if min_value is not None and min_value < 0:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"input_text min_value (length) must be >= 0, got {min_value}.",
                    context=_simple_helper_error_context(
                        helper_type,
                        min_value=min_value,
                    ),
                )
            )
        if max_value is not None and max_value > 255:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"input_text max_value (length) must be <= 255, got {max_value}.",
                    context=_simple_helper_error_context(
                        helper_type,
                        max_value=max_value,
                    ),
                )
            )

    if min_value is not None and max_value is not None:
        if min_value > max_value:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"min_value ({min_value}) cannot be greater than max_value ({max_value}).",
                    context=_simple_helper_error_context(
                        helper_type,
                        min_value=min_value,
                        max_value=max_value,
                    ),
                )
            )
        if min_value == max_value:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"min_value and max_value must differ (both were {min_value}). "
                    f"Pick a non-empty range so the helper has more than one valid value.",
                    context=_simple_helper_error_context(
                        helper_type,
                        min_value=min_value,
                        max_value=max_value,
                    ),
                )
            )

    # Step validation only applies to numeric types (not input_text).
    if helper_type in ("input_number", "counter") and step is not None:
        if step <= 0:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"step must be > 0 for {helper_type} (got {step}).",
                    context=_simple_helper_error_context(helper_type, step=step),
                )
            )
        if (
            min_value is not None
            and max_value is not None
            and (max_value - min_value) > 0
            and step > (max_value - min_value)
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"step ({step}) is larger than the range "
                    f"(max_value - min_value = {max_value - min_value}). "
                    f"HA does not reject this, but the resulting slider/control "
                    f"is unusable. Reduce step or widen the range.",
                    context=_simple_helper_error_context(
                        helper_type,
                        min_value=min_value,
                        max_value=max_value,
                        step=step,
                    ),
                )
            )


def _validate_initial_in_options(
    options: Any, initial: Any, helper_type: str = "input_select"
) -> None:
    """Reject ``initial`` values not in ``options``.

    Called from both create and update branches with the resolved values —
    caller-supplied on create, merged with the existing config on update.
    ``initial=None`` is the unset case and passes through. The
    ``isinstance(options, list)`` early-return mirrors the defensive shape
    check in ``_validate_input_select_options`` below — both validators are
    invariant gates, not type contracts; a future non-list caller is
    silently skipped rather than raising a confusing ``TypeError`` on
    ``initial not in options``.
    """
    if not isinstance(options, list) or initial is None:
        return
    if initial not in options:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"initial={initial!r} must be one of options "
                f"{options!r} for {helper_type}.",
                context=_simple_helper_error_context(
                    helper_type,
                    initial=initial,
                    options=options,
                ),
                suggestions=[
                    "Pick an `initial` value that's in `options`.",
                    "Or omit `initial` to use the default or existing value.",
                ],
            )
        )


def _validate_datetime_has_date_or_time(
    has_date: bool | None, has_time: bool | None
) -> None:
    """Reject ``input_datetime`` payloads where both components are False.

    Treats ``None`` as "not constrained" — only the explicit (False, False)
    case is flagged, since that's what reaches HA as the broken-entity
    payload. Both the create and update branches call this with the
    resolved-after-merge ``has_date`` / ``has_time`` pair.
    """
    if has_date is False and has_time is False:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "At least one of has_date or has_time must be True for input_datetime",
                context=_simple_helper_error_context(
                    "input_datetime",
                    has_date=has_date,
                    has_time=has_time,
                ),
                suggestions=[
                    "Set has_date=True to keep the date component.",
                    "Set has_time=True to keep the time component.",
                ],
            )
        )


def _validate_input_select_options(options: Any) -> None:
    """Reject input_select option lists containing duplicates (Bug 17, issue #1150).

    HA rejects duplicates with "Duplicate options are not allowed", but the
    error path it takes is generic enough that callers tend to misread it.
    Pre-validate so the message is unambiguous.
    """
    if not isinstance(options, list):
        return
    seen: set[Any] = set()
    duplicates: list[Any] = []
    for opt in options:
        if opt in seen and opt not in duplicates:
            duplicates.append(opt)
        else:
            seen.add(opt)
    if duplicates:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"input_select options must be unique. Duplicate option(s): "
                f"{', '.join(repr(d) for d in duplicates)}.",
                context=_simple_helper_error_context(
                    "input_select",
                    duplicates=duplicates,
                ),
                suggestions=["Remove duplicate entries from the options list."],
            )
        )


def _parse_hms(value: Any) -> tuple[int, int, int] | None:
    """Parse 'HH:MM' or 'HH:MM:SS' to a (h, m, s) tuple. Returns None if unparsable."""
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
        s = int(parts[2]) if len(parts) == 3 else 0
    except ValueError:
        return None
    return h, m, s


def _validate_schedule_days(
    monday: list | None,
    tuesday: list | None,
    wednesday: list | None,
    thursday: list | None,
    friday: list | None,
    saturday: list | None,
    sunday: list | None,
) -> None:
    """Pre-validate schedule day-range structure (Bug 17, issue #1150).

    Each range must include 'from' and 'to'; ranges within a single day must
    not overlap. HA reports per-day errors; surface a single clear message
    upfront with the offending day named.
    """
    day_params = {
        "monday": monday,
        "tuesday": tuesday,
        "wednesday": wednesday,
        "thursday": thursday,
        "friday": friday,
        "saturday": saturday,
        "sunday": sunday,
    }
    for day_name, day_schedule in day_params.items():
        if day_schedule is None:
            continue
        if not isinstance(day_schedule, list):
            continue  # let HA report shape errors
        intervals: list[tuple[int, int]] = []  # (from_secs, to_secs)
        for idx, time_range in enumerate(day_schedule):
            if not isinstance(time_range, dict):
                continue
            if "from" not in time_range or "to" not in time_range:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"schedule {day_name}[{idx}] must include both 'from' "
                        f"and 'to' keys, got: {sorted(time_range.keys())}.",
                        context=_simple_helper_error_context(
                            "schedule",
                            day=day_name,
                        ),
                    )
                )
            from_parsed = _parse_hms(time_range["from"])
            to_parsed = _parse_hms(time_range["to"])
            if from_parsed is None or to_parsed is None:
                continue  # let HA report format errors
            from_secs = from_parsed[0] * 3600 + from_parsed[1] * 60 + from_parsed[2]
            to_secs = to_parsed[0] * 3600 + to_parsed[1] * 60 + to_parsed[2]
            intervals.append((from_secs, to_secs))

        # Check overlap by sorting and walking. HA rejects overlap regardless
        # of caller order — we sort here so the error message points at a
        # canonical pair.
        sorted_intervals = sorted(intervals, key=lambda iv: iv[0])
        for i in range(1, len(sorted_intervals)):
            prev_from, prev_to = sorted_intervals[i - 1]
            cur_from, cur_to = sorted_intervals[i]
            if cur_from < prev_to:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"schedule {day_name} has overlapping time ranges "
                        f"({prev_from // 3600:02d}:{(prev_from % 3600) // 60:02d}-"
                        f"{prev_to // 3600:02d}:{(prev_to % 3600) // 60:02d} and "
                        f"{cur_from // 3600:02d}:{(cur_from % 3600) // 60:02d}-"
                        f"{cur_to // 3600:02d}:{(cur_to % 3600) // 60:02d}). "
                        f"HA requires non-overlapping ranges per day.",
                        context=_simple_helper_error_context(
                            "schedule",
                            day=day_name,
                        ),
                    )
                )


async def _validate_set_helper_action(
    client: Any,
    action: str | None,
    helper_id: str | None,
    helper_type: str,
) -> str:
    """Validate and resolve the action for ha_config_set_helper."""
    if action is not None:
        if action == "create" and helper_id is not None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"action='create' was passed together with helper_id={helper_id!r}. "
                    "These are contradictory: create makes a new helper, while helper_id "
                    "targets an existing one.",
                    context=(
                        _simple_helper_error_context(
                            helper_type, action=action, helper_id=helper_id
                        )
                        if helper_type in SIMPLE_HELPER_TYPES
                        else await _flow_helper_error_context(
                            client, helper_type, action=action, helper_id=helper_id
                        )
                    ),
                    suggestions=[
                        "Omit helper_id to create a new helper",
                        "Or pass action='update' to modify the existing helper at helper_id",
                    ],
                )
            )
        if action == "update" and helper_id is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "action='update' requires helper_id to identify which helper to modify.",
                    context=(
                        _simple_helper_error_context(helper_type, action=action)
                        if helper_type in SIMPLE_HELPER_TYPES
                        else await _flow_helper_error_context(
                            client, helper_type, action=action
                        )
                    ),
                    suggestions=[
                        'Pass "helper_id": "my_helper" to identify the helper',
                        "Or pass action='create' (or omit action) to create a new helper",
                    ],
                )
            )
        if action == "update" and helper_id is not None:
            validate_identifier_not_empty(
                helper_id,
                "helper_id",
                suggestions=[
                    "Pass a valid helper_id to identify the helper to update",
                    "Or omit helper_id and pass action='create' to create a new helper",
                ],
                context={"helper_type": helper_type, "action": action},
            )
        return action
    # Implicit discriminator (back-compat).
    if helper_id is not None:
        validate_identifier_not_empty(
            helper_id,
            "helper_id",
            suggestions=[
                "Omit helper_id entirely to create a new helper",
                "Pass a valid helper_id to update an existing helper",
                "Or pass action='create' / action='update' explicitly to declare intent",
            ],
            context={"helper_type": helper_type},
        )
    return "update" if helper_id else "create"


def _validate_pre_dispatch_params(
    helper_type: str,
    min_value: float | None,
    max_value: float | None,
    step: float | None,
    options: list[str] | None,
    monday: list | None,
    tuesday: list | None,
    wednesday: list | None,
    thursday: list | None,
    friday: list | None,
    saturday: list | None,
    sunday: list | None,
) -> None:
    """Run per-type schema validation before dispatching create or update."""
    if helper_type in ("input_number", "counter", "input_text"):
        _validate_numeric_range(helper_type, min_value, max_value, step)
    if helper_type == "input_select":
        _validate_input_select_options(options)
    if helper_type == "schedule":
        _validate_schedule_days(
            monday, tuesday, wednesday, thursday, friday, saturday, sunday
        )
