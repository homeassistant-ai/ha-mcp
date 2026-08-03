"""Form-step handling for config-entry flows.

Extracted from ``config_entry_flow.py`` (which holds the public create/update
entry points) when that module crossed the ~1000-line split threshold. Turns a
step's serialized ``data_schema`` plus the caller's config dict into the payload
to submit, and tracks what was consumed so a later step redeclaring a field can
be filled. Imports the menu selection keys from ``config_entry_flow_menu``
(never submitted as form data); ``config_entry_flow_walker`` imports from here.
"""

import copy
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from ..errors import ErrorCode, create_error_response
from .config_entry_flow_menu import _MENU_SELECTION_KEY_ORDER
from .helpers import raise_tool_error

# Membership form of the canonical selection-key order — defined here, in the
# module that checks membership on every form field, and imported onward by
# the walker.
_MENU_SELECTION_KEYS = frozenset(_MENU_SELECTION_KEY_ORDER)

_MISSING_DEFAULT = object()
# "Submit nothing for this field" — see _redeclared_field_submission.
_NO_SUBMISSION: tuple[Any, bool] = (_MISSING_DEFAULT, False)


def iter_schema_fields(data_schema: Any) -> Iterator[dict[str, Any]]:
    """Yield leaf field definitions from a flow schema, descending into sections."""
    if not isinstance(data_schema, list):
        return
    for field in data_schema:
        if not isinstance(field, dict):
            continue
        nested_schema = field.get("schema")
        if isinstance(nested_schema, list):
            yield from iter_schema_fields(nested_schema)
            continue
        yield field


def _section_path(path_prefix: str, name: Any) -> str:
    """Return the dotted config path for a named or anonymous section."""
    if not isinstance(name, str):
        return path_prefix
    return f"{path_prefix}.{name}" if path_prefix else name


@dataclass
class _ReuseState:
    """Caller-supplied form values a flow's steps have already consumed.

    A config key applies to every form step that declares it, not only the
    first step to pop it out of ``remaining_config`` — see
    :func:`_redeclared_field_submission` for when a recorded value is
    resubmitted. The record is split by how the caller wrote the value:

    - ``scoped`` maps a dotted declaration path to a value that came out of an
      explicitly supplied section dict. The caller named that section, so the
      value belongs to that path and nowhere else.
    - ``flat`` maps a leaf name to a value that came out of the flat caller
      dict. The caller named no section, so the value is position-agnostic and
      fills that leaf wherever a later step declares it.

    ``filled`` holds the dotted paths the current step already filled from the
    caller's own keys, so nothing is injected over a value the caller wrote for
    this very step; it is cleared per step. ``fired`` bounds resubmission to
    one write per (step, path) for the whole flow, ``notes`` collects the
    warning each of those writes emits, and ``step_id`` names the step being
    consumed.
    """

    scoped: dict[str, Any] = dc_field(default_factory=dict)
    flat: dict[str, Any] = dc_field(default_factory=dict)
    filled: set[str] = dc_field(default_factory=set)
    fired: set[str] = dc_field(default_factory=set)
    notes: list[str] = dc_field(default_factory=list)
    step_id: str | None = None

    def begin_step(self, step_id: Any) -> None:
        """Start recording for a new form step."""
        self.step_id = step_id if isinstance(step_id, str) else None
        self.filled.clear()

    def record(
        self, path_prefix: str, name: str, value: Any, *, scoped_only: bool
    ) -> None:
        """Snapshot a value the caller supplied at this declaration site.

        A flat value that lands on a path already recorded from an explicit
        section dict also replaces that scoped entry: the flat key is the one
        actually submitted for the path (flat overrides explicit), so the
        record must carry the effective value or a later redeclaration of the
        path would resurrect the overridden one.
        """
        dotted = _section_path(path_prefix, name)
        self.filled.add(dotted)
        if scoped_only:
            self.scoped[dotted] = copy.deepcopy(value)
        else:
            self.flat[name] = copy.deepcopy(value)
            if dotted in self.scoped:
                self.scoped[dotted] = copy.deepcopy(value)

    def recorded_value(self, path_prefix: str, name: str) -> Any:
        """Return the value recorded for this declaration site, else _MISSING_DEFAULT."""
        dotted = _section_path(path_prefix, name)
        if dotted in self.scoped:
            return copy.deepcopy(self.scoped[dotted])
        if name in self.flat:
            return copy.deepcopy(self.flat[name])
        return _MISSING_DEFAULT

    def claim_write(self, dotted: str) -> bool:
        """Spend the single resubmission allowed for this (step, path), if unspent.

        A flow that re-presents the same step gets one reused write and then
        HA's own loud "required key not provided" naming the field, rather than
        an unbounded run of silent rewrites.
        """
        key = f"{self.step_id}:{dotted}"
        if key in self.fired:
            return False
        self.fired.add(key)
        self.notes.append(
            f"Resubmitted '{dotted}' at step '{self.step_id}': supplied once "
            "but requested by more than one step encounter in this flow "
            "(a later step redeclaring the field, or the same step revisited "
            "via a menu loop — per-visit values cannot be expressed)"
        )
        return True


def _record_ignored_section_keys(
    ignored_config_keys: set[str] | None,
    remaining_config: dict[str, Any],
    section_path: str,
) -> None:
    """Record undeclared keys remaining inside an explicit section dict."""
    if ignored_config_keys is None:
        return
    ignored_config_keys.update(
        f"{section_path}.{key}" if section_path else key
        for key in remaining_config
        if key not in _MENU_SELECTION_KEYS
    )


def _field_default_value(field: dict[str, Any]) -> Any:
    """Return a serialized schema field's default/suggested value, if present."""
    description = field.get("description")
    if isinstance(description, dict) and description.get("suggested_value") is not None:
        return copy.deepcopy(description["suggested_value"])
    if field.get("suggested_value") is not None:
        return copy.deepcopy(field["suggested_value"])
    if "default" in field:
        return copy.deepcopy(field["default"])
    return _MISSING_DEFAULT


def _schema_default_values(data_schema: list[Any]) -> dict[str, Any]:
    """Build default form data from serialized schema suggestions/defaults."""
    defaults: dict[str, Any] = {}
    for field in data_schema:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        nested_schema = field.get("schema")
        if not isinstance(name, str):
            continue
        if isinstance(nested_schema, list):
            nested_defaults = _schema_default_values(nested_schema)
            if nested_defaults:
                defaults[name] = nested_defaults
            continue
        default = _field_default_value(field)
        if default is not _MISSING_DEFAULT:
            defaults[name] = default
    return defaults


def _required_section_defaults(field: dict[str, Any]) -> dict[str, Any]:
    """Return default data for a required section, otherwise an empty dict."""
    nested_schema = field.get("schema")
    if not field.get("required") or not isinstance(nested_schema, list):
        return {}
    return _schema_default_values(nested_schema)


def _ignored_keys_warnings(
    ignored_config_keys: set[str], remaining_config: dict[str, Any]
) -> list[str]:
    """Build warnings for caller-supplied config keys no flow step consumed."""
    warnings: list[str] = []
    ignored = ignored_config_keys | {
        key for key in remaining_config if key not in _MENU_SELECTION_KEYS
    }
    if ignored:
        warnings.append(
            "Ignored config keys not declared by the Home Assistant flow "
            f"schema: {', '.join(sorted(ignored))}"
        )
    leftover_menu_keys = _MENU_SELECTION_KEYS & remaining_config.keys()
    if leftover_menu_keys:
        # Name the values, not just the keys: an un-consumed selection means
        # that branch was never configured, which a success response would
        # otherwise hide (the scalar era carried no information beyond the
        # key name; a list does).
        details = ", ".join(
            f"{key}={remaining_config[key]!r}" for key in sorted(leftover_menu_keys)
        )
        warnings.append(
            f"Ignored menu selection key(s) with no matching menu step: {details}"
        )
    return warnings


def _success_warnings(
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> list[str]:
    """Build a success response's ``warnings`` list (empty when there is nothing to say).

    Merges the keys no step declared with the resubmissions the walk performed,
    keeping ``warnings`` a flat ``list[str]`` per the response contract in
    ``tests/src/unit/test_helper_response_shape.py``.
    """
    return _ignored_keys_warnings(ignored_config_keys, remaining_config) + list(
        reuse_state.notes
    )


def _consume_section_schema(
    field: dict[str, Any],
    explicit_section: dict[str, Any] | None,
    remaining_config: dict[str, Any],
    ignored_config_keys: set[str] | None,
    consumed_config_keys: set[str] | None,
    path_prefix: str,
    reuse_state: _ReuseState | None = None,
    *,
    allow_reuse: bool = True,
    explicit_source: bool = False,
) -> dict[str, Any]:
    """Consume config values for a nested flow section.

    Values inside an explicitly supplied section dict are consumed first, then
    flat caller keys, which is what lets a flat child override the same key
    written inside the section dict.
    """
    nested_schema = field.get("schema")
    if not isinstance(nested_schema, list):
        return {}

    name = field.get("name")
    section_path = _section_path(path_prefix, name)
    nested_data = _required_section_defaults(field)

    if explicit_section is not None:
        explicit_remaining = dict(explicit_section)
        nested_data.update(
            _consume_form_schema(
                nested_schema,
                explicit_remaining,
                ignored_config_keys,
                consumed_config_keys,
                section_path,
                reuse_state,
                allow_reuse=allow_reuse,
                explicit_source=True,
            )
        )
        _record_ignored_section_keys(
            ignored_config_keys,
            explicit_remaining,
            section_path,
        )

    nested_data.update(
        _consume_form_schema(
            nested_schema,
            remaining_config,
            ignored_config_keys,
            consumed_config_keys,
            section_path,
            reuse_state,
            allow_reuse=allow_reuse,
            explicit_source=explicit_source,
        )
    )
    return nested_data


def _mark_consumed(
    consumed_config_keys: set[str] | None,
    path_prefix: str,
    name: str,
) -> None:
    """Record that a caller-supplied value was used, fresh or resubmitted.

    Values the step's own schema supplies — section defaults, suggestions,
    constants — are never marked: they are HA's data, and marking them would
    let a config of nothing but misspelled keys look partially applied to
    :func:`_finish_flow_entry`.
    """
    if consumed_config_keys is not None:
        consumed_config_keys.add(_section_path(path_prefix, name))


def _auto_confirm_form_payload(current_step: dict[str, Any]) -> dict[str, Any] | None:
    """Return payload for HA preview/confirmation-only forms we can safely advance."""
    if "preview" not in current_step:
        return None
    data_schema = current_step.get("data_schema")
    if not isinstance(data_schema, list) or len(data_schema) != 1:
        return None
    field = data_schema[0]
    if not isinstance(field, dict) or not field.get("required"):
        return None
    name = field.get("name")
    if not isinstance(name, str) or name in _MENU_SELECTION_KEYS:
        return None
    default = _field_default_value(field)
    if default is not False:
        return None
    selector = field.get("selector")
    if isinstance(selector, dict) and selector and "boolean" not in selector:
        return None
    return {name: True}


def _step_owned_submission_value(field: dict[str, Any]) -> Any:
    """Return the value a step's own schema supplies for ``field``, else _MISSING_DEFAULT.

    Deliberately distinct from :func:`_field_default_value`, which answers
    "what would the UI show in this box" and is what seeds a form. This answers
    "what does the step itself say to submit", which is a different question:

    - ``voluptuous_serialize`` emits ``"default"`` only for an actual
      voluptuous default. HA's edit-style pre-fill
      (``add_suggested_values_to_schema``) copies the marker and overwrites
      only its description, so a marker that already carried a default
      serializes with both keys; the suggestion is the stored current value
      and outranks the static default.
    - A constant field serializes as ``{"type": "constant", "value": X}`` and
      ``X`` is the only value it accepts.

    The bare top-level ``suggested_value`` shape is not something
    ``voluptuous_serialize`` produces; it is read defensively alongside the
    nested one.
    """
    description = field.get("description")
    if isinstance(description, dict) and description.get("suggested_value") is not None:
        return copy.deepcopy(description["suggested_value"])
    if field.get("suggested_value") is not None:
        return copy.deepcopy(field["suggested_value"])
    if field.get("type") == "constant" and field.get("value") is not None:
        return copy.deepcopy(field["value"])
    return _MISSING_DEFAULT


def _redeclared_field_submission(
    field: dict[str, Any],
    name: str,
    path_prefix: str,
    reuse_state: _ReuseState | None,
    allow_reuse: bool,
) -> tuple[Any, bool]:
    """Decide what to submit for a declared field the caller named no key for here.

    Returns ``(value, from_caller)``, or ``_NO_SUBMISSION`` to omit the key
    entirely. A site the caller's own key already filled earlier in this same
    step is left alone — a section dict and a flat key can name the same leaf,
    and the caller's value for this step outranks anything injected. Otherwise,
    in order:

    1. The field is not required: omit it. Nothing is ever injected into an
       optional field, or into a section that is neither required nor named by
       the caller (``allow_reuse``) — materializing either would invent data.
    2. The step's own schema supplies a value — a suggestion or a constant's
       only legal value, per :func:`_step_owned_submission_value`: submit that.
       It is schema data rather than a caller key, so it is neither marked
       consumed nor warned about. A suggestion outranks a coexisting
       ``"default"``: both keys can serialize together, and the suggestion is
       the stored current value while the default is the static schema one —
       omitting would let voluptuous substitute the static value over it.
    3. The field carries a ``"default"`` key (and no value of its own): omit it
       and let voluptuous fill the default in. Key presence is the test, so
       ``default: None`` is a default too.
    4. Otherwise the field is required, has no default and has no value of its
       own, which makes omitting it a guaranteed "required key not provided":
       resubmit the value the caller supplied for an earlier step, warn, and
       spend the one write allowed per (step, path).

    Mutates ``reuse_state`` on the fourth branch. Menu selection keys never
    reach here. Motivating regression (issue #2057): an options flow —
    LocalTuya's — that declares the same field on an early step and again on a
    later one.
    """
    if reuse_state is None or not allow_reuse:
        return _NO_SUBMISSION
    dotted = _section_path(path_prefix, name)
    if dotted in reuse_state.filled:
        return _NO_SUBMISSION
    if not field.get("required"):
        return _NO_SUBMISSION
    step_owned = _step_owned_submission_value(field)
    if step_owned is not _MISSING_DEFAULT:
        return step_owned, False
    if "default" in field:
        return _NO_SUBMISSION
    recorded = reuse_state.recorded_value(path_prefix, name)
    if recorded is _MISSING_DEFAULT:
        return _NO_SUBMISSION
    if not reuse_state.claim_write(dotted):
        return _NO_SUBMISSION
    return recorded, True


def _consume_leaf_field(
    field: dict[str, Any],
    name: str,
    form_data: dict[str, Any],
    remaining_config: dict[str, Any],
    consumed_config_keys: set[str] | None,
    reuse_state: _ReuseState | None,
    path_prefix: str,
    *,
    allow_reuse: bool = True,
    explicit_source: bool = False,
) -> None:
    """Fill ``name`` in ``form_data`` from the caller's config or from the step itself.

    A key the caller supplied here is popped, submitted, and recorded in
    ``reuse_state`` — scoped to this dotted path when it came out of an
    explicitly supplied section dict, keyed by leaf name when it came out of
    the flat caller dict. With no key to pop,
    :func:`_redeclared_field_submission` chooses between omitting the field,
    submitting the step's own value, and resubmitting the recorded one.
    """
    if name in remaining_config:
        value = remaining_config.pop(name)
        form_data[name] = value
        _mark_consumed(consumed_config_keys, path_prefix, name)
        if reuse_state is not None:
            reuse_state.record(path_prefix, name, value, scoped_only=explicit_source)
        return

    value, from_caller = _redeclared_field_submission(
        field, name, path_prefix, reuse_state, allow_reuse
    )
    if value is _MISSING_DEFAULT:
        return
    form_data[name] = value
    if from_caller:
        _mark_consumed(consumed_config_keys, path_prefix, name)


def _consume_declared_section(
    field: dict[str, Any],
    form_data: dict[str, Any],
    remaining_config: dict[str, Any],
    ignored_config_keys: set[str] | None,
    consumed_config_keys: set[str] | None,
    path_prefix: str,
    reuse_state: _ReuseState | None,
    *,
    allow_reuse: bool,
    explicit_source: bool,
) -> None:
    """Merge one section field's data into ``form_data``.

    A caller who supplies the section as a non-dict value gets it submitted
    verbatim — HA, not this walker, decides what that means. Reuse is allowed
    inside the section only when HA marks it required or the caller named it,
    so an untouched optional section is never brought into existence.
    """
    name = field.get("name")
    section_name = name if isinstance(name, str) else None
    explicit_section: dict[str, Any] | None = None
    if section_name is not None and section_name in remaining_config:
        explicit_value = remaining_config.pop(section_name)
        if not isinstance(explicit_value, dict):
            form_data[section_name] = explicit_value
            _mark_consumed(consumed_config_keys, path_prefix, section_name)
            return
        explicit_section = explicit_value

    nested_data = _consume_section_schema(
        field,
        explicit_section,
        remaining_config,
        ignored_config_keys,
        consumed_config_keys,
        path_prefix,
        reuse_state,
        allow_reuse=allow_reuse
        and (bool(field.get("required")) or explicit_section is not None),
        explicit_source=explicit_source,
    )
    if not nested_data:
        return
    if section_name is not None:
        form_data[section_name] = nested_data
    else:
        form_data.update(nested_data)


def _consume_form_schema(
    data_schema: list[Any],
    remaining_config: dict[str, Any],
    ignored_config_keys: set[str] | None = None,
    consumed_config_keys: set[str] | None = None,
    path_prefix: str = "",
    reuse_state: _ReuseState | None = None,
    *,
    allow_reuse: bool = True,
    explicit_source: bool = False,
) -> dict[str, Any]:
    """Consume matching config values and shape nested flow sections.

    Mutates ``remaining_config`` by removing every consumed key. Flat child
    values override the same value inside an explicitly supplied section dict.
    Unknown keys inside explicit section dicts are added to
    ``ignored_config_keys`` with their dotted section path. A declared field the
    caller named no key for is filled per
    :func:`_redeclared_field_submission`.
    """
    form_data: dict[str, Any] = {}

    for field in data_schema:
        if not isinstance(field, dict):
            continue

        name = field.get("name")
        if isinstance(field.get("schema"), list):
            _consume_declared_section(
                field,
                form_data,
                remaining_config,
                ignored_config_keys,
                consumed_config_keys,
                path_prefix,
                reuse_state,
                allow_reuse=allow_reuse,
                explicit_source=explicit_source,
            )
            continue

        if isinstance(name, str) and name not in _MENU_SELECTION_KEYS:
            _consume_leaf_field(
                field,
                name,
                form_data,
                remaining_config,
                consumed_config_keys,
                reuse_state,
                path_prefix,
                allow_reuse=allow_reuse,
                explicit_source=explicit_source,
            )

    return form_data


def _extract_schema_field_names(data_schema: Any) -> set[str] | None:
    """Extract the set of field names declared by a step's data_schema.

    HA returns data_schema as a list of {name, selector, required, ...} dicts.
    Nested leaf names are included; section-container names are omitted.
    Returns ``None`` when the schema is absent or not a list (signalling
    the caller to fall back to legacy submit-all behaviour). Returns a
    (possibly empty) set when the schema is present and parseable.
    """
    if not isinstance(data_schema, list):
        return None
    names: set[str] = set()
    for field in iter_schema_fields(data_schema):
        name = field.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def _consume_all_remaining_keys(
    remaining_config: dict[str, Any],
    consumed_config_keys: set[str] | None,
    reuse_state: _ReuseState | None,
) -> dict[str, Any]:
    """Submit every non-menu key, for a step whose schema HA did not send.

    Without field names there is nothing to filter on, so the whole config is
    dumped into this one submit and cleared out of ``remaining_config``. Each
    consumed key is still recorded by leaf name, so a later step that *does*
    arrive with a schema declaring one of them can be filled from the record
    rather than submitted without it.

    In a cyclic-menu flow the config legitimately carries values destined
    for several branches, so a schemaless step mid-loop sweeping them all up
    is far likelier to misfire ("extra keys not allowed" attributed to the
    wrong step) — a warning is surfaced when this fires while menu
    selections are still queued, so the sweep is at least visible.
    """
    form_data: dict[str, Any] = {}
    for key in list(remaining_config.keys()):
        if key in _MENU_SELECTION_KEYS:
            continue
        value = remaining_config.pop(key)
        form_data[key] = value
        _mark_consumed(consumed_config_keys, "", key)
        if reuse_state is not None:
            reuse_state.record("", key, value, scoped_only=False)
    queued = {
        key: remaining_config[key]
        for key in _MENU_SELECTION_KEY_ORDER
        if key in remaining_config
    }
    if form_data and queued and reuse_state is not None:
        reuse_state.notes.append(
            "A step without a schema consumed every remaining config key "
            f"({', '.join(sorted(form_data))}) while menu selection(s) "
            f"{queued!r} were still queued — values intended for later "
            "branches may have been submitted to this step instead"
        )
    return form_data


def _handle_form_step(
    flow_id: str,
    current_step: dict[str, Any],
    remaining_config: dict[str, Any],
    ignored_config_keys: set[str] | None = None,
    consumed_config_keys: set[str] | None = None,
    reuse_state: _ReuseState | None = None,
) -> dict[str, Any]:
    """Validate a form step and return form data to submit.

    When the step's ``data_schema`` is provided, pops ONLY the keys declared
    in that schema from ``remaining_config`` (mutating it) so any unconsumed
    keys remain available for subsequent steps. Menu selection keys are never
    submitted. Fields declared inside a section are grouped under the section
    key; callers may provide them flat or inside an explicit section dict.

    Caller-supplied values are recorded in ``reuse_state``. A field this step
    declares but the caller named no key for here is filled per
    :func:`_redeclared_field_submission`: omitted when the schema carries a
    ``"default"`` or the field is optional, submitted from the step's own
    suggestion or constant, and otherwise — required, no default, no value of
    its own — resubmitted once from an earlier step's caller value with a
    warning. Nothing is injected into an optional field, or into a section
    neither marked required nor named by the caller.

    When ``data_schema`` is absent (HA didn't tell us field names), falls
    back to legacy behaviour: submit all non-menu keys and clear them. This
    keeps single-step flows working when HA omits the schema.

    Raises ToolError on validation errors.
    """
    if current_step.get("errors"):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Form validation failed",
                suggestions=["Fix the field errors and retry with corrected values"],
                context={
                    "flow_id": flow_id,
                    "step_id": current_step.get("step_id"),
                    "errors": current_step["errors"],
                    "data_schema": current_step.get("data_schema"),
                },
            )
        )

    if reuse_state is not None:
        reuse_state.begin_step(current_step.get("step_id"))

    data_schema = current_step.get("data_schema")
    if not isinstance(data_schema, list):
        return _consume_all_remaining_keys(
            remaining_config, consumed_config_keys, reuse_state
        )

    return _consume_form_schema(
        data_schema,
        remaining_config,
        ignored_config_keys,
        consumed_config_keys,
        "",
        reuse_state,
    )
