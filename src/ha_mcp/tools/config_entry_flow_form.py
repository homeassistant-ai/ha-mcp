"""Form-step handling for config-entry flows.

Extracted from ``config_entry_flow.py`` (which holds the public create/update
entry points) when that module crossed the ~1000-line split threshold. Turns a
step's serialized ``data_schema`` plus the caller's config dict into the payload
to submit, and tracks what was consumed so a later step redeclaring a field can
be filled. Flows that edit an existing object pass ``keep_current_values``,
so a field the caller left out goes back as the step presented it instead of
being dropped (issue #2254). Imports the menu selection keys from
``config_entry_flow_menu`` (never submitted as form data);
``config_entry_flow_walker`` imports from here.
"""

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from ..errors import ErrorCode, create_error_response
from ..redaction import carries_sentinel
from .config_entry_flow_menu import _MENU_SELECTION_KEY_ORDER
from .helpers import raise_tool_error

# Membership form of the canonical selection-key order — defined here, in the
# module that checks membership on every form field, and imported onward by
# the walker.
_MENU_SELECTION_KEYS = frozenset(_MENU_SELECTION_KEY_ORDER)

# Reserved key carrying per-step field values: ``{step_id: {field: value}}``.
# The flat config dict is keyed by field NAME alone, so a flow whose steps
# declare the same field twice — a later step redeclaring it, or one revisited
# through a menu loop — had no way to say "this value here, that one there, and
# nothing at all on the third". The walker slices this per form step; a step
# nobody addresses behaves exactly as before. Same shape of reservation as the
# selection keys, including their collision risk: ``group_type`` is also a real
# field on the group integration, and that has been the accepted trade since it
# was introduced. Lives HERE rather than beside those keys because this module
# is its only user — a constant defined in one module and read only from
# another reads as dead to CodeQL's per-module py/unused-global-variable, the
# same reason ``_MENU_SELECTION_KEYS`` itself sits here.
_PER_STEP_VALUES_KEY = "step_values"

# Every key the walker consumes as a DIRECTIVE rather than as form data, so
# none of them is ever submitted to Home Assistant or counted as an ignored
# field the flow failed to declare.
_RESERVED_CONFIG_KEYS = _MENU_SELECTION_KEYS | {_PER_STEP_VALUES_KEY}

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
    step_scoped: set[str] = dc_field(default_factory=set)
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
        if name in self.step_scoped or dotted.split(".", 1)[0] in self.step_scoped:
            # Supplied by ``step_values`` for THIS step only. ``filled`` still
            # takes it so nothing is injected over it here, but it must never
            # reach ``flat``/``scoped``: those survive the whole walk, and a
            # later step that nobody addressed would then reuse a value the
            # caller scoped to one step instead of its own stored one.
            #
            # BOTH the popped key and the ROOT of the declaration path are
            # matched, because an overlay key reaches a leaf two ways. Naming
            # the SECTION gives leaves whose own names are nowhere in
            # ``step_scoped`` (so the root must be checked), while a FLAT key
            # is accepted for a leaf a section declares, which makes the root
            # the section name and equally absent (so the key must be too).
            # Either check alone leaks the other shape (issue #2254).
            return
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
            "via a menu loop). Pass step_values={'<step_id>': {'<field>': "
            "<value>}} to give a step its own value, or to leave it out of "
            "that step entirely; pass a LIST of those objects to supply one "
            "per encounter when the flow presents the step more than once."
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
        if key not in _RESERVED_CONFIG_KEYS
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
        key for key in remaining_config if key not in _RESERVED_CONFIG_KEYS
    }
    if ignored:
        warnings.append(
            "Ignored config keys not declared by the Home Assistant flow "
            f"schema: {', '.join(sorted(ignored))}"
        )
    leftover_steps = remaining_config.get(_PER_STEP_VALUES_KEY)
    if isinstance(leftover_steps, dict) and leftover_steps:
        warnings.append(
            "step_values entries were never applied: "
            f"{', '.join(sorted(str(k) for k in leftover_steps))}. A step_id "
            "the flow never presents applies nothing; a list is consumed one "
            "entry per encounter, so a leftover tail means the flow presented "
            "that step fewer times than the list expects."
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
    keep_current_values: bool = False,
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
                keep_current_values=keep_current_values,
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
            keep_current_values=keep_current_values,
        )
    )
    # Strip before returning, so the caller's "did this section get anything?"
    # check counts real values and a section holding only clears is dropped
    # rather than submitted as sentinels.
    return _strip_cleared(nested_data)


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


# Marks a field the caller cleared. A clear cannot be expressed by simply
# leaving the key out: a REQUIRED section is pre-seeded with the step's own
# values by _required_section_defaults BEFORE anything is consumed, so an
# omitted key just lets that seed survive the merge and resubmits the value
# the caller asked to drop. The sentinel rides through the merge in the key's
# place and is stripped once the section is assembled (issue #2254).
_CLEARED = object()


def _strip_cleared(form_data: dict[str, Any]) -> dict[str, Any]:
    """Drop every key a clear marked, at any depth. Mutates and returns."""
    for name in [k for k, v in form_data.items() if v is _CLEARED]:
        del form_data[name]
    for value in form_data.values():
        if isinstance(value, dict):
            _strip_cleared(value)
    return form_data


def _is_redacted_value(value: Any) -> bool:
    """True when a step-owned value is, or contains, a redaction sentinel.

    ``redact_secrets`` rewrites a deep copy of a schema for error contexts, so
    the live step a walk submits against should never carry a sentinel.
    Backfill is the one place that turns schema data back into submitted data,
    though, so the check is made anyway: writing ``<redacted: set>`` into a
    password field would replace a working secret with a placeholder.
    """
    if isinstance(value, list):
        return any(carries_sentinel(item) for item in value)
    return carries_sentinel(value)


def _current_value_backfill(field: dict[str, Any]) -> tuple[Any, bool]:
    """Resubmit what an edit-mode step carries for a field the caller left out.

    Options, reconfigure and subentry-reconfigure steps arrive pre-filled by
    Home Assistant's ``add_suggested_values_to_schema``, and the UI's save
    posts every box back, so a field nobody named has to be submitted as the
    step presented it or the save rewrites it (issue #2254). Returns
    ``_NO_SUBMISSION`` when the step supplies no value of its own: a bare
    ``"default"`` is the static schema value, which voluptuous fills in for an
    omitted key exactly as it does for the UI's own form.
    """
    step_owned = _step_owned_submission_value(field)
    if step_owned is _MISSING_DEFAULT or _is_redacted_value(step_owned):
        return _NO_SUBMISSION
    return step_owned, False


def _clears_by_omission(field: dict[str, Any]) -> bool:
    """Whether leaving this field OUT is how the caller clears it.

    Only for a field that is neither required nor carrying a ``"default"``:
    there, an absent key simply stays absent. Anything else has a value
    voluptuous substitutes for the omission — the static default, which is a
    different value than the caller asked for and would report success while
    clearing nothing — so the ``None`` is submitted instead and Home Assistant
    decides whether null is meaningful for that field.

    The single source of truth for both sites that answer this question:
    :func:`_consume_leaf_field`, where the caller's key is popped, and
    :func:`_edit_mode_submission`, where a later encounter of the same field
    has to reach the same verdict (Patch76 review, issue #2254).
    """
    return not field.get("required") and "default" not in field


def _edit_mode_submission(
    field: dict[str, Any],
    name: str,
    path_prefix: str,
    dotted: str,
    reuse_state: _ReuseState,
) -> tuple[Any, bool]:
    """Decide an edit-mode field the caller named no key for at THIS step.

    The caller's intent outranks the step's stored value for the whole walk.
    ``begin_step`` clears only ``filled``, so by a later step the caller's key
    is gone from ``remaining_config`` and nothing marks the field as theirs —
    but ``scoped``/``flat`` still hold what they asked for. Backfilling over
    that resubmitted the entry's stored value and undid it: a recorded
    ``None`` is the clear this mode documents, and a recorded value is the one
    the caller asked to write (Patch76 review, issue #2254).

    Only a field the caller never named anywhere falls through to the step's
    own value — as does one whose reused write is already spent, rather than
    letting an omission apply a static default over what is stored.
    """
    recorded = reuse_state.recorded_value(path_prefix, name)
    if recorded is _MISSING_DEFAULT:
        return _current_value_backfill(field)
    if recorded is None:
        # The caller cleared it. Express that the same way the popping site
        # did, or the clear stops holding at its second encounter.
        return _NO_SUBMISSION if _clears_by_omission(field) else (None, True)
    if not reuse_state.claim_write(dotted):
        # The one reused write per (step, path) is spent — a menu loop is
        # revisiting this step. Falling back to omission would let voluptuous
        # substitute the field's STATIC default over the entry's stored value,
        # which is the very wipe this mode exists to stop, so send what the
        # step presented instead (CodeRabbit review, issue #2254). The
        # required-field branch still omits: there, omission raises HA's own
        # loud "required key not provided" rather than losing data silently.
        return _current_value_backfill(field)
    return recorded, True


def _redeclared_field_submission(
    field: dict[str, Any],
    name: str,
    path_prefix: str,
    reuse_state: _ReuseState | None,
    allow_reuse: bool,
    *,
    keep_current_values: bool = False,
) -> tuple[Any, bool]:
    """Decide what to submit for a declared field the caller named no key for here.

    Returns ``(value, from_caller)``, or ``_NO_SUBMISSION`` to omit the key
    entirely. A site the caller's own key already filled earlier in this same
    step is left alone — a section dict and a flat key can name the same leaf,
    and the caller's value for this step outranks anything injected. Otherwise,
    in order:

    1. The field is not required, or reuse is barred for this site because the
       caller named neither it nor the section holding it (``allow_reuse``).
       Under ``keep_current_values`` a value the caller recorded ANYWHERE
       earlier in this walk wins first — resubmitted as theirs, or omitted
       when it was ``None``, which is the clear. Failing that the step's own
       value goes back, per :func:`_current_value_backfill` — it is the step's
       data, not the caller's, so barring reuse does not bar it, and a section
       the backfill itself materializes has to carry what it requires.
       Otherwise omit: injecting into either on a create flow would invent
       data.
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
    reach here. Motivating regressions: issue #2057, an options flow
    (LocalTuya's) that declares the same field on an early step and again on a
    later one; issue #2254, a one-field options patch that reset every field
    the caller did not name back to its static schema default.
    """
    if reuse_state is None:
        return _NO_SUBMISSION
    dotted = _section_path(path_prefix, name)
    if dotted in reuse_state.filled:
        return _NO_SUBMISSION
    if not allow_reuse or not field.get("required"):
        if not keep_current_values:
            return _NO_SUBMISSION
        return _edit_mode_submission(field, name, path_prefix, dotted, reuse_state)
    recorded = reuse_state.recorded_value(path_prefix, name)
    if recorded is None:
        # An explicit clear. The popping site submitted this ``None``
        # verbatim, and so must every later encounter: letting the step's own
        # value or a static default win here silently reinstates the very
        # thing the caller asked to drop. Narrow on purpose — a recorded
        # VALUE still loses to the step's own, which predates this PR and is
        # what issue #2057 settled.
        return None, True
    step_owned = _step_owned_submission_value(field)
    if step_owned is not _MISSING_DEFAULT:
        return step_owned, False
    if "default" in field:
        return _NO_SUBMISSION
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
    keep_current_values: bool = False,
) -> None:
    """Fill ``name`` in ``form_data`` from the caller's config or from the step itself.

    A key the caller supplied here is popped, submitted, and recorded in
    ``reuse_state`` — scoped to this dotted path when it came out of an
    explicitly supplied section dict, keyed by leaf name when it came out of
    the flat caller dict. With no key to pop,
    :func:`_redeclared_field_submission` chooses between omitting the field,
    submitting the step's own value, and resubmitting the recorded one.

    Under ``keep_current_values`` an explicit ``None`` on an optional field
    with no ``"default"`` is a clear rather than a value: the key is consumed
    and recorded, but left out of the payload, because for such a field
    omission is the only way to express a clear — the value simply stays
    absent (issue #2254).

    A field that carries a ``"default"`` is submitted with ``None`` verbatim
    instead. Omitting it would let voluptuous substitute the static schema
    default, which is a different value than the caller asked for and would
    report success while quietly not clearing anything; submitting lets Home
    Assistant decide whether ``None`` is meaningful for that field and say so
    if it is not. A required field submits ``None`` verbatim for the same
    reason.
    """
    if name in remaining_config:
        value = remaining_config.pop(name)
        clearing = keep_current_values and value is None and _clears_by_omission(field)
        form_data[name] = _CLEARED if clearing else value
        _mark_consumed(consumed_config_keys, path_prefix, name)
        if reuse_state is not None:
            reuse_state.record(path_prefix, name, value, scoped_only=explicit_source)
        return

    value, from_caller = _redeclared_field_submission(
        field,
        name,
        path_prefix,
        reuse_state,
        allow_reuse,
        keep_current_values=keep_current_values,
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
    keep_current_values: bool,
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
        keep_current_values=keep_current_values,
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
    keep_current_values: bool = False,
) -> dict[str, Any]:
    """Consume matching config values and shape nested flow sections.

    Mutates ``remaining_config`` by removing every consumed key. Flat child
    values override the same value inside an explicitly supplied section dict.
    Unknown keys inside explicit section dicts are added to
    ``ignored_config_keys`` with their dotted section path. A declared field the
    caller named no key for is filled per
    :func:`_redeclared_field_submission`, which ``keep_current_values`` puts
    into the edit-mode contract described there.
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
                keep_current_values=keep_current_values,
            )
            continue

        if isinstance(name, str) and name not in _RESERVED_CONFIG_KEYS:
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
                keep_current_values=keep_current_values,
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
        if key in _RESERVED_CONFIG_KEYS:
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


def validate_step_values(config: dict[str, Any]) -> None:
    """Reject a malformed ``step_values`` directive before the walk starts.

    The reserved key is consumed as a directive, so a malformed one is invisible
    to ignored-key reporting: the walk would complete, report success, and have
    applied nothing the caller asked for. A caller error is a tool-level
    failure, so it raises rather than warns (CodeRabbit review, issue #2254).

    Accepted: a dict of ``step_id -> entry``, where an entry is a dict of field
    values, or a LIST of such dicts consumed one per encounter of that step.
    """
    # Key PRESENCE is the test, not truthiness: an explicit ``None`` is a
    # caller who meant to pass a directive and got the shape wrong, and the
    # reserved key hides it from ignored-key reporting, so returning early on
    # it would let the walk apply the rest and report a clean success.
    if _PER_STEP_VALUES_KEY not in config:
        return
    directive = config[_PER_STEP_VALUES_KEY]

    example = "{'<step_id>': {'<field>': <value>}}"
    if not isinstance(directive, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"'{_PER_STEP_VALUES_KEY}' must be an object keyed by step id, "
                f"got {type(directive).__name__}",
                suggestions=[f"Pass {_PER_STEP_VALUES_KEY}={example}."],
                # TYPE, never the value. A rejected directive can carry a
                # credential the caller is submitting for the FIRST time, which
                # Home Assistant does not hold yet — so no read-back has
                # harvested it and RedactSecretsMiddleware cannot scrub it even
                # when enabled. raise_tool_error serialises this whole response
                # into the exception message, and @log_tool_usage records that
                # as error_message from inside the tool, where only
                # `parameters` are masked: the value would reach plaintext
                # mcp_usage.jsonl (Patch76 review, issue #2254). The shape is
                # what diagnoses a malformed directive anyway.
                context={"received_type": type(directive).__name__},
            )
        )

    # The step id is a caller-controlled key, and nothing echoed a NESTED one
    # before this directive existed — the walker's own supplied_keys reports
    # only top-level names. These errors reach the usage log unmasked (see the
    # note above), so report the entry's POSITION instead: it is what the
    # caller needs to find the entry in their own directive, and it cannot
    # carry a value they typed (CodeRabbit review, issue #2254).
    for index, entry in enumerate(directive.values(), start=1):
        where = f"{_PER_STEP_VALUES_KEY} entry #{index}"
        entries = entry if isinstance(entry, list) else [entry]
        if any(not isinstance(item, dict) for item in entries):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{where} must be an object of field values, or a list "
                    "of them for a step the flow presents more than once",
                    suggestions=[
                        f"Pass {_PER_STEP_VALUES_KEY}={example}, or a list of "
                        "those objects to supply one per encounter.",
                    ],
                    context={
                        "entry_index": index,
                        "entry_types": [type(item).__name__ for item in entries],
                    },
                )
            )
        # An entry that can apply nothing is a caller mistake in the same way a
        # malformed one is, and it was reported inconsistently: a bare {} left
        # a leftover the warning named, while [] and [{}] were popped at their
        # first encounter and vanished, so the walk reported a clean success
        # for a directive that did nothing (Patch76 review, issue #2254).
        if not entries or not any(item for item in entries):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{where} supplies no field values, so it would apply nothing",
                    suggestions=[
                        "Name at least one field for the step, or drop the entry.",
                    ],
                    context={"entry_index": index, "entry_count": len(entries)},
                )
            )

    if not directive:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"'{_PER_STEP_VALUES_KEY}' is empty, so it would apply nothing",
                suggestions=[f"Name a step: {_PER_STEP_VALUES_KEY}={example}."],
                context={},
            )
        )


@contextmanager
def _step_values_applied(
    remaining_config: dict[str, Any],
    current_step: dict[str, Any],
    ignored_config_keys: set[str] | None,
    reuse_state: _ReuseState | None,
) -> Iterator[None]:
    """Overlay this step's entry from ``step_values`` for the duration.

    The flat config dict is keyed by field name alone, so a field two steps
    declare could only ever carry one value for both. Inside this block the
    step's own entry SHADOWS the flat value, and on the way out the flat value
    is restored so a later step still sees it — addressing one step therefore
    never spends the caller's flat value, and a step nobody addresses behaves
    exactly as it did before.

    An overlaid key the step turns out not to declare is dropped rather than
    left behind: it belongs to this step, so it must not leak into a later
    one or be reported as a field no step declared.
    """
    overlay = remaining_config.get(_PER_STEP_VALUES_KEY)
    step_id = current_step.get("step_id")
    if not isinstance(overlay, dict):
        yield
        return

    raw = overlay.get(step_id)
    # A LIST is consumed one entry per encounter, its un-consumed tail
    # replacing the key until it runs dry — the same shape ``next_step_id``
    # uses for a menu the flow revisits, and the only one that can express a
    # loop. A single dict applies once and is spent, which is why taking the
    # warning's advice used to reach FEWER encounters than the flat key it
    # replaces. The caller's list object is never mutated.
    is_list = isinstance(raw, list)
    head = (raw[0] if raw else None) if is_list else raw
    values = head if isinstance(head, dict) and head else {}
    if not values and not is_list:
        yield
        return

    shadowed = {k: remaining_config[k] for k in values if k in remaining_config}
    if values:
        remaining_config.update(copy.deepcopy(values))
        if reuse_state is not None:
            reuse_state.step_scoped |= set(values)
    try:
        yield
    finally:
        if values and reuse_state is not None:
            reuse_state.step_scoped -= set(values)
        for key in values:
            # Still present = this step's schema never declared it. A field
            # scoped to one step applies nowhere else, so it would otherwise
            # vanish without ever being submitted or reported — report it
            # under its own path before dropping it. Checked BEFORE the
            # shadowed flat values go back, or a restored flat key would look
            # like an unconsumed overlay one.
            if key in remaining_config and ignored_config_keys is not None:
                ignored_config_keys.add(f"{_PER_STEP_VALUES_KEY}.{step_id}.{key}")
            remaining_config.pop(key, None)
        remaining_config.update(shadowed)
        # Spend this encounter. A list leaves its tail for the next visit; a
        # dict is spent outright. What survives the whole walk is reported
        # rather than silently doing nothing, the same way an un-consumed
        # menu selection is.
        tail = raw[1:] if isinstance(raw, list) else []
        if tail:
            overlay[step_id] = tail
        else:
            overlay.pop(step_id, None)
        if not overlay:
            remaining_config.pop(_PER_STEP_VALUES_KEY, None)


def _handle_form_step(
    flow_id: str,
    current_step: dict[str, Any],
    remaining_config: dict[str, Any],
    ignored_config_keys: set[str] | None = None,
    consumed_config_keys: set[str] | None = None,
    reuse_state: _ReuseState | None = None,
    *,
    keep_current_values: bool = False,
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

    ``keep_current_values`` switches that last sentence off for the flows that
    edit an existing object — options, reconfigure, subentry reconfigure. Their
    steps arrive pre-filled with the stored values and the UI's save posts all
    of them back, so a declared field the caller left out is submitted with the
    step's own value wherever the step supplies one (issue #2254); a field the
    caller set to ``None`` is a clear, submitted as an omission only where the
    field has no schema default to be substituted in its place (see
    :func:`_consume_leaf_field`). It takes a ``reuse_state`` to work: the record is what keeps
    a backfill off a path the caller already filled in this same step.

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

    with _step_values_applied(
        remaining_config, current_step, ignored_config_keys, reuse_state
    ):
        return _strip_cleared(
            _consume_form_schema(
                data_schema,
                remaining_config,
                ignored_config_keys,
                consumed_config_keys,
                "",
                reuse_state,
                keep_current_values=keep_current_values,
            )
        )
