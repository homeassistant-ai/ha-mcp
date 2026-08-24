"""Unit tests for the edit-mode form contract (issue #2254).

An options, reconfigure or subentry-reconfigure step arrives pre-filled by
Home Assistant's ``add_suggested_values_to_schema``, and saving the UI form
posts every box back. The walker used to submit only the keys the caller
named, so voluptuous filled each omitted ``vol.Optional(k, default=STATIC)``
with its static default and dropped every no-default optional outright — a
one-field patch through ``ha_set_integration(entry_id=..., config=...)``
silently reset the rest of the entry (repro: core ``workday``, where
``config={"days_offset": 3}`` reset the workday/exclude lists and wiped
``province``).

``keep_current_values`` closes that: a declared field the caller named no key
for is submitted with the value the step itself carries. Create flows — add
integration, create helper, create subentry — keep the old behaviour, since
there is no stored value to preserve and materializing a field would invent
data. The clear gesture is an explicit ``null``, which is consumed and then
omitted, exactly as the UI's empty box is.
"""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.redaction import REDACTED_EMPTY, REDACTED_SET
from ha_mcp.tools.config_entry_flow import update_config_entry_options
from ha_mcp.tools.config_entry_flow_form import (
    _handle_form_step,
    _ignored_keys_warnings,
    _ReuseState,
)
from ha_mcp.tools.config_entry_flow_walker import (
    ReconfigureStatus,
    _handle_config_subentry_flow_steps,
    _handle_flow_steps,
)


def _workday_options_step() -> dict[str, Any]:
    """An options step shaped like core ``workday``'s, as HA serializes it.

    Every stored value rides in ``description.suggested_value``. The list
    fields keep the static ``default`` their voluptuous marker was built with,
    so both keys serialize together; ``province`` has a stored value and no
    default; ``language`` has neither.
    """
    return {
        "type": "form",
        "flow_id": "flow-2254",
        "step_id": "init",
        "data_schema": [
            {
                "name": "workdays",
                "default": ["mon", "tue", "wed", "thu", "fri"],
                "description": {"suggested_value": ["mon", "wed", "fri"]},
            },
            {
                "name": "excludes",
                "default": ["sat", "sun", "holiday"],
                "description": {"suggested_value": ["sun"]},
            },
            {
                "name": "days_offset",
                "default": 0,
                "description": {"suggested_value": 0},
            },
            {"name": "province", "description": {"suggested_value": "BW"}},
            {"name": "add_holidays", "default": []},
            {"name": "language"},
        ],
    }


class TestOptionsStepBackfill:
    """The step's own values go back for every field the caller left out."""

    def test_one_field_patch_keeps_the_rest_of_the_entry(self) -> None:
        remaining: dict[str, Any] = {"days_offset": 3}

        form_data = _handle_form_step(
            "flow-2254",
            _workday_options_step(),
            remaining,
            reuse_state=_ReuseState(),
            keep_current_values=True,
        )

        assert form_data == {
            "days_offset": 3,
            # Stored value wins over the static default that would otherwise
            # be substituted for the omitted key.
            "workdays": ["mon", "wed", "fri"],
            "excludes": ["sun"],
            # No default at all: omitting it wiped the field outright.
            "province": "BW",
        }
        # A bare "default" is the static schema value voluptuous fills in for
        # an omitted key — the same thing it does for the UI's own form — and
        # a field with neither a stored value nor a default has nothing to
        # send. Both stay out of the payload.
        assert "add_holidays" not in form_data
        assert "language" not in form_data
        assert remaining == {}

    def test_create_flow_submits_only_what_the_caller_named(self) -> None:
        """Flag off: byte-for-byte the pre-#2254 payload."""
        remaining: dict[str, Any] = {"days_offset": 3}

        form_data = _handle_form_step(
            "flow-2254",
            _workday_options_step(),
            remaining,
            reuse_state=_ReuseState(),
        )

        assert form_data == {"days_offset": 3}

    def test_constant_field_is_backfilled_with_its_only_legal_value(self) -> None:
        step: dict[str, Any] = {
            "type": "form",
            "step_id": "init",
            "data_schema": [
                {"name": "host", "required": True},
                {"name": "mode", "type": "constant", "value": "LOCKED"},
            ],
        }

        form_data = _handle_form_step(
            "flow-2254",
            step,
            {"host": "10.0.0.5"},
            reuse_state=_ReuseState(),
            keep_current_values=True,
        )

        assert form_data == {"host": "10.0.0.5", "mode": "LOCKED"}

    def test_backfill_is_a_copy_of_the_schema_value(self) -> None:
        """The payload must not alias the step dict a caller may still read."""
        step = _workday_options_step()

        form_data = _handle_form_step(
            "flow-2254",
            step,
            {"days_offset": 3},
            reuse_state=_ReuseState(),
            keep_current_values=True,
        )

        stored = step["data_schema"][0]["description"]["suggested_value"]
        assert form_data["workdays"] == stored
        assert form_data["workdays"] is not stored

    def test_caller_value_is_never_overwritten_by_the_backfill(self) -> None:
        remaining: dict[str, Any] = {"province": "BY", "workdays": ["sat"]}

        form_data = _handle_form_step(
            "flow-2254",
            _workday_options_step(),
            remaining,
            reuse_state=_ReuseState(),
            keep_current_values=True,
        )

        assert form_data["province"] == "BY"
        assert form_data["workdays"] == ["sat"]


class TestExplicitClear:
    """``null`` is the caller asking for the field to be emptied."""

    def test_null_on_an_optional_field_with_no_default_omits_the_key(self) -> None:
        remaining: dict[str, Any] = {"province": None}
        consumed: set[str] = set()

        form_data = _handle_form_step(
            "flow-2254",
            _workday_options_step(),
            remaining,
            None,
            consumed,
            _ReuseState(),
            keep_current_values=True,
        )

        # Omission is how the UI's form clears a box, so the key goes out of
        # the payload rather than in with a null.
        assert "province" not in form_data
        # It is still the caller's key: it counts as consumed, so a config of
        # nothing but clears does not read as "no keys applied".
        assert consumed == {"province"}
        assert remaining == {}

    def test_null_on_a_defaulted_optional_field_is_submitted_verbatim(self) -> None:
        """Omitting it would substitute the static default, not clear it.

        A defaulted field cannot express "empty" by omission — voluptuous
        fills the default in. Submitting the None hands the decision to Home
        Assistant, which either clears the field or rejects the value; both
        beat reporting success after quietly writing the schema default
        (Codex review, #2256).
        """
        remaining: dict[str, Any] = {"workdays": None}
        consumed: set[str] = set()

        form_data = _handle_form_step(
            "flow-2254",
            _workday_options_step(),
            remaining,
            None,
            consumed,
            _ReuseState(),
            keep_current_values=True,
        )

        assert form_data["workdays"] is None
        assert consumed == {"workdays"}
        # The clear is the caller's business; the untouched fields still get
        # their usual backfill.
        assert form_data["province"] == "BW"

    def test_null_on_a_required_field_is_submitted_verbatim(self) -> None:
        """HA, not this walker, decides what a required null means."""
        step: dict[str, Any] = {
            "type": "form",
            "step_id": "init",
            "data_schema": [
                {
                    "name": "host",
                    "required": True,
                    "description": {"suggested_value": "10.0.0.5"},
                },
            ],
        }

        form_data = _handle_form_step(
            "flow-2254",
            step,
            {"host": None},
            reuse_state=_ReuseState(),
            keep_current_values=True,
        )

        assert form_data == {"host": None}

    def test_create_flow_still_submits_an_explicit_null(self) -> None:
        form_data = _handle_form_step(
            "flow-2254",
            _workday_options_step(),
            {"province": None},
            reuse_state=_ReuseState(),
        )

        assert form_data == {"province": None}

    async def test_a_clear_only_config_completes_the_walk(self) -> None:
        """The clear is consumption, so the empty-forms guard stays quiet."""
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-clear"},
        }
        submit_fn = AsyncMock(side_effect=[final_entry])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2254",
            initial_step=_workday_options_step(),
            config={"province": None},
            submit_fn=submit_fn,
            keep_current_values=True,
        )

        assert result["success"] is True
        assert "province" not in submit_fn.await_args_list[0].args[1]


class TestRedactionSentinelIsNeverBackfilled:
    """A sentinel is a placeholder, never data to write back (#2157/#2164)."""

    @pytest.mark.parametrize(
        "stored",
        [
            REDACTED_SET,
            REDACTED_EMPTY,
            "postgres://user:<redacted>@db/main",
            [REDACTED_SET, REDACTED_SET],
        ],
    )
    def test_sentinel_bearing_suggestion_is_omitted(self, stored: Any) -> None:
        step: dict[str, Any] = {
            "type": "form",
            "step_id": "init",
            "data_schema": [
                {"name": "host", "required": True},
                {"name": "api_key", "description": {"suggested_value": stored}},
            ],
        }

        form_data = _handle_form_step(
            "flow-2254",
            step,
            {"host": "10.0.0.5"},
            reuse_state=_ReuseState(),
            keep_current_values=True,
        )

        assert form_data == {"host": "10.0.0.5"}


def _section_step(
    section_required: bool, leaves: list[dict[str, Any]]
) -> dict[str, Any]:
    """A step whose ``advanced`` section holds ``leaves``."""
    return {
        "type": "form",
        "step_id": "init",
        "data_schema": [
            {"name": "host", "required": True},
            {
                "type": "expandable",
                "name": "advanced",
                "required": section_required,
                "schema": leaves,
            },
        ],
    }


class TestSectionBackfill:
    """Sections are boxes on the same form, so the same contract reaches them."""

    def test_leaf_inside_a_required_section_is_backfilled(self) -> None:
        step = _section_step(
            True,
            [
                {
                    "name": "timeout",
                    "default": 10,
                    "description": {"suggested_value": 45},
                },
                {"name": "retries"},
            ],
        )

        form_data = _handle_form_step(
            "flow-2254",
            step,
            {"host": "10.0.0.5"},
            reuse_state=_ReuseState(),
            keep_current_values=True,
        )

        assert form_data == {"host": "10.0.0.5", "advanced": {"timeout": 45}}

    def test_untouched_optional_section_is_restored_not_invented(self) -> None:
        """The stored values are real data, so the section goes back whole.

        Leaving it out is what wiped it: HA re-validates the whole options
        dict on save. A required leaf in the same section gets the step's
        value too, or the section this backfill materializes would arrive
        missing a key HA demands.
        """
        step = _section_step(
            False,
            [
                {"name": "timeout", "description": {"suggested_value": 45}},
                {
                    "name": "scheme",
                    "required": True,
                    "description": {"suggested_value": "https"},
                },
            ],
        )

        form_data = _handle_form_step(
            "flow-2254",
            step,
            {"host": "10.0.0.5"},
            reuse_state=_ReuseState(),
            keep_current_values=True,
        )

        assert form_data == {
            "host": "10.0.0.5",
            "advanced": {"timeout": 45, "scheme": "https"},
        }

    def test_clearing_a_leaf_in_a_required_section_drops_the_prefill(self) -> None:
        """A required section is pre-seeded; a clear has to beat the seed.

        ``_required_section_defaults`` seeds a REQUIRED section with the
        step's own values before anything is consumed, and
        ``_field_default_value`` reads ``description.suggested_value`` first —
        so an optional no-default leaf inside one arrives already holding its
        stored value. Merely leaving the cleared key out of the consumed
        payload lets that seed survive the merge, submitting the old value
        while the tool reports a successful clear (Codex review, #2256).
        """
        step = _section_step(
            True,
            [
                {"name": "timeout", "description": {"suggested_value": 45}},
                {
                    "name": "scheme",
                    "required": True,
                    "description": {"suggested_value": "https"},
                },
            ],
        )

        form_data = _handle_form_step(
            "flow-2254",
            step,
            {"host": "10.0.0.5", "advanced": {"timeout": None}},
            reuse_state=_ReuseState(),
            keep_current_values=True,
        )

        assert "timeout" not in form_data["advanced"], (
            "The cleared leaf came back from the required-section prefill: "
            f"{form_data['advanced']}"
        )
        # The rest of the section still goes back untouched.
        assert form_data["advanced"]["scheme"] == "https"
        assert form_data["host"] == "10.0.0.5"

    def test_create_flow_leaves_an_untouched_optional_section_alone(self) -> None:
        """Flag off keeps the #2013 rule: nothing the caller never named."""
        step = _section_step(
            False,
            [
                {"name": "timeout", "description": {"suggested_value": 45}},
                {
                    "name": "scheme",
                    "required": True,
                    "description": {"suggested_value": "https"},
                },
            ],
        )

        form_data = _handle_form_step(
            "flow-2254",
            step,
            {"host": "10.0.0.5"},
            reuse_state=_ReuseState(),
        )

        assert form_data == {"host": "10.0.0.5"}

    def test_explicit_section_value_outranks_the_backfill(self) -> None:
        step = _section_step(
            False,
            [
                {"name": "timeout", "description": {"suggested_value": 45}},
                {"name": "retries", "description": {"suggested_value": 3}},
            ],
        )

        form_data = _handle_form_step(
            "flow-2254",
            step,
            {"host": "10.0.0.5", "advanced": {"timeout": 90}},
            reuse_state=_ReuseState(),
            keep_current_values=True,
        )

        assert form_data == {
            "host": "10.0.0.5",
            "advanced": {"timeout": 90, "retries": 3},
        }


def _reuse_warning(dotted: str, step_id: str) -> str:
    """The warning a resubmitted caller key adds to the walk's response."""
    return (
        f"Resubmitted '{dotted}' at step '{step_id}': supplied once "
        "but requested by more than one step encounter in this flow "
        "(a later step redeclaring the field, or the same step revisited "
        "via a menu loop). Pass step_values={'<step_id>': {'<field>': "
        "<value>}} to give a step its own value, or to leave it out of "
        "that step entirely."
    )


class TestRequiredFieldPathsUnchanged:
    """The #2057 last-resort reuse is untouched by the new mode."""

    @pytest.mark.parametrize(
        "extra",
        [
            {"description": {"suggested_value": "10.0.0.1"}},
            {"default": "fallback"},
            {},
        ],
        ids=["required-with-suggestion", "required-with-default", "required-bare"],
    )
    def test_a_clear_on_a_required_field_survives_its_second_encounter(
        self, extra: dict[str, Any]
    ) -> None:
        """A required field's clear must not be reinstated by a later step.

        The popping site submits the ``None`` verbatim for a required field
        and lets Home Assistant rule on it. A later encounter used to let the
        step's own suggestion win (reinstating the stored value) or omit the
        key (letting voluptuous substitute the static default), silently
        undoing the clear in two of the three shapes.
        """

        def step(step_id: str) -> dict[str, Any]:
            field: dict[str, Any] = {"name": "host", "required": True}
            field.update(extra)
            return {"type": "form", "step_id": step_id, "data_schema": [field]}

        reuse_state = _ReuseState()
        remaining: dict[str, Any] = {"host": None}

        first = _handle_form_step(
            "flow-2254",
            step("one"),
            remaining,
            None,
            set(),
            reuse_state,
            keep_current_values=True,
        )
        second = _handle_form_step(
            "flow-2254",
            step("two"),
            remaining,
            None,
            set(),
            reuse_state,
            keep_current_values=True,
        )

        assert first == {"host": None}
        assert second == {"host": None}, (
            f"The clear was undone on encounter two: {second}"
        )

    async def test_required_redeclared_field_still_reuses_the_caller_value(
        self,
    ) -> None:
        first_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2254",
            "step_id": "init",
            "data_schema": [
                {"name": "friendly_name", "required": True, "default": ""},
            ],
        }
        later_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2254",
            "step_id": "details",
            "data_schema": [
                {"name": "id", "required": True},
                {"name": "friendly_name", "required": True},
            ],
        }
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-2057"},
        }
        submit_fn = AsyncMock(side_effect=[later_step, final_entry])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2254",
            initial_step=first_step,
            config={"friendly_name": "Device1", "id": 20},
            submit_fn=submit_fn,
            keep_current_values=True,
        )

        assert submit_fn.await_args_list[1].args[1] == {
            "id": 20,
            "friendly_name": "Device1",
        }
        assert result["warnings"] == [_reuse_warning("friendly_name", "details")]

    @staticmethod
    def _redeclaring_step(step_id: str) -> dict[str, Any]:
        """Two steps both declaring the same OPTIONAL field, both pre-filled."""
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": [
                {
                    "name": "province",
                    "required": False,
                    "optional": True,
                    "description": {"suggested_value": "BW"},
                    "selector": {"select": {"options": ["BW", "BY"]}},
                },
            ],
        }

    def test_a_clear_survives_a_later_step_redeclaring_the_field(self) -> None:
        """``null`` must stay a clear for the whole walk, not just one step.

        ``begin_step`` clears only ``filled``, and the caller's key was popped
        from ``remaining_config`` by the first step — so by the second step
        nothing marks the field as the caller's and the backfill used to
        resubmit the stored value, silently undoing the clear the tool
        descriptions promise (Patch76 review, #2256).
        """
        reuse_state = _ReuseState()
        remaining: dict[str, Any] = {"province": None}

        first = _handle_form_step(
            "flow-2254",
            self._redeclaring_step("one"),
            remaining,
            None,
            set(),
            reuse_state,
            keep_current_values=True,
        )
        second = _handle_form_step(
            "flow-2254",
            self._redeclaring_step("two"),
            remaining,
            None,
            set(),
            reuse_state,
            keep_current_values=True,
        )

        assert "province" not in first
        assert "province" not in second, (
            f"The later step resurrected the cleared field: {second}"
        )

    def test_a_callers_value_outranks_a_later_steps_suggestion(self) -> None:
        """The caller asked for BY; the step still suggests the stored BW."""
        reuse_state = _ReuseState()
        remaining: dict[str, Any] = {"province": "BY"}

        first = _handle_form_step(
            "flow-2254",
            self._redeclaring_step("one"),
            remaining,
            None,
            set(),
            reuse_state,
            keep_current_values=True,
        )
        second = _handle_form_step(
            "flow-2254",
            self._redeclaring_step("two"),
            remaining,
            None,
            set(),
            reuse_state,
            keep_current_values=True,
        )

        assert first["province"] == "BY"
        assert second["province"] == "BY", (
            f"The later step overwrote the caller's value: {second}"
        )

    @staticmethod
    def _defaulted_step(step_id: str) -> dict[str, Any]:
        """One OPTIONAL field carrying a static default and no suggestion."""
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": [
                {
                    "name": "workdays",
                    "required": False,
                    "optional": True,
                    "default": ["mon", "tue", "wed", "thu", "fri"],
                },
            ],
        }

    @pytest.mark.parametrize(
        ("first_step", "second_step"),
        [("one", "two"), ("init", "init")],
        ids=["later-step-redeclares", "same-step-revisited"],
    )
    def test_a_clear_on_a_defaulted_field_survives_its_second_encounter(
        self, first_step: str, second_step: str
    ) -> None:
        """What expresses a clear depends on the field's shape, at BOTH sites.

        ``_consume_leaf_field`` submits the ``None`` verbatim for a field
        carrying a ``"default"``, because omitting it there would let
        voluptuous substitute that default. The later encounter has to reach
        the same verdict — omitting it on encounter two hands the default back
        and reports success having cleared nothing (Patch76 review, #2256).
        """
        reuse_state = _ReuseState()
        remaining: dict[str, Any] = {"workdays": None}

        first = _handle_form_step(
            "flow-2254",
            self._defaulted_step(first_step),
            remaining,
            None,
            set(),
            reuse_state,
            keep_current_values=True,
        )
        second = _handle_form_step(
            "flow-2254",
            self._defaulted_step(second_step),
            remaining,
            None,
            set(),
            reuse_state,
            keep_current_values=True,
        )

        assert first == {"workdays": None}
        assert second == {"workdays": None}, (
            "The clear stopped holding at its second encounter; omitting the "
            f"key lets the static default back in. Got: {second}"
        )

    def test_a_redeclared_optional_field_warns_when_it_resubmits(self) -> None:
        """Routing the optional path through claim_write emits its note.

        Pinned rather than asserted-away: the mode treats resubmitting the
        caller's value as the CORRECT outcome here, so the warning rides along
        with it and callers see it (Patch76 review, #2256).
        """
        reuse_state = _ReuseState()
        remaining: dict[str, Any] = {"province": "BY"}

        _handle_form_step(
            "flow-2254",
            self._redeclaring_step("one"),
            remaining,
            None,
            set(),
            reuse_state,
            keep_current_values=True,
        )
        _handle_form_step(
            "flow-2254",
            self._redeclaring_step("two"),
            remaining,
            None,
            set(),
            reuse_state,
            keep_current_values=True,
        )

        assert reuse_state.notes == [_reuse_warning("province", "two")]

    def test_a_revisited_step_keeps_the_stored_value_after_the_write_is_spent(
        self,
    ) -> None:
        """A menu loop must not let the static default win on encounter three.

        ``claim_write`` allows one reused write per (step, path). Once spent,
        omitting the key handed voluptuous the field's STATIC default, which
        overwrote the entry's stored value — the exact wipe this mode exists
        to stop (CodeRabbit review, #2256). The step's own value goes back
        instead.
        """
        step: dict[str, Any] = {
            "type": "form",
            "step_id": "init",
            "data_schema": [
                {
                    "name": "workdays",
                    "required": False,
                    "optional": True,
                    "default": ["mon", "tue", "wed", "thu", "fri"],
                    "description": {"suggested_value": ["mon", "wed", "fri"]},
                },
            ],
        }
        reuse_state = _ReuseState()
        remaining: dict[str, Any] = {"workdays": ["sat"]}
        payloads = [
            _handle_form_step(
                "flow-2254",
                dict(step),
                remaining,
                None,
                set(),
                reuse_state,
                keep_current_values=True,
            )
            for _ in range(3)
        ]

        # The caller's value applies while the budget lasts...
        assert payloads[0]["workdays"] == ["sat"]
        assert payloads[1]["workdays"] == ["sat"]
        # ...and once spent the STORED value goes back, never the static
        # default that an omission would have substituted.
        assert payloads[2]["workdays"] == ["mon", "wed", "fri"], (
            f"Spent write fell back to the schema default: {payloads[2]}"
        )

    def test_create_flow_still_drops_a_redeclared_optional_field(self) -> None:
        """Flag off keeps the pre-#2254 shape: nothing goes back at all."""
        reuse_state = _ReuseState()
        remaining: dict[str, Any] = {"province": "BY"}

        _handle_form_step(
            "flow-2254",
            self._redeclaring_step("one"),
            remaining,
            None,
            set(),
            reuse_state,
        )
        second = _handle_form_step(
            "flow-2254",
            self._redeclaring_step("two"),
            remaining,
            None,
            set(),
            reuse_state,
        )

        assert second == {}

    def test_required_field_with_only_a_static_default_is_still_omitted(self) -> None:
        """Voluptuous fills it, exactly as it does for the UI's own form."""
        step: dict[str, Any] = {
            "type": "form",
            "step_id": "init",
            "data_schema": [
                {"name": "host", "required": True},
                {"name": "port", "required": True, "default": 80},
            ],
        }

        form_data = _handle_form_step(
            "flow-2254",
            step,
            {"host": "10.0.0.5"},
            reuse_state=_ReuseState(),
            keep_current_values=True,
        )

        assert form_data == {"host": "10.0.0.5"}


class TestPerStepValues:
    """``step_values`` addresses a field per step encounter (#2254 review)."""

    @staticmethod
    def _step(step_id: str) -> dict[str, Any]:
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": [
                {
                    "name": "province",
                    "required": False,
                    "optional": True,
                    "description": {"suggested_value": "BW"},
                },
            ],
        }

    def _walk(self, config: dict[str, Any]) -> tuple[Any, Any, dict[str, Any]]:
        reuse_state = _ReuseState()
        remaining = dict(config)
        first = _handle_form_step(
            "flow-2254",
            self._step("one"),
            remaining,
            None,
            set(),
            reuse_state,
            keep_current_values=True,
        )
        second = _handle_form_step(
            "flow-2254",
            self._step("two"),
            remaining,
            None,
            set(),
            reuse_state,
            keep_current_values=True,
        )
        return first, second, remaining

    def test_a_step_can_carry_its_own_value(self) -> None:
        first, second, _ = self._walk(
            {"province": "BY", "step_values": {"two": {"province": "TX"}}}
        )
        assert first == {"province": "BY"}
        assert second == {"province": "TX"}

    def test_addressing_one_step_leaves_the_others_to_the_stored_value(self) -> None:
        """No flat value at all: the unaddressed step keeps what is stored."""
        first, second, _ = self._walk({"step_values": {"two": {"province": "TX"}}})
        assert first == {"province": "BW"}
        assert second == {"province": "TX"}

    def test_a_step_can_clear_a_field_the_other_sets(self) -> None:
        first, second, _ = self._walk(
            {"province": "BY", "step_values": {"two": {"province": None}}}
        )
        assert first == {"province": "BY"}
        assert "province" not in second

    def test_an_unaddressed_flow_is_unchanged(self) -> None:
        first, second, remaining = self._walk({"province": "BY"})
        assert first == {"province": "BY"}
        assert second == {"province": "BY"}
        assert remaining == {}

    def test_the_reserved_key_is_never_submitted_or_reported_ignored(self) -> None:
        ignored: set[str] = set()
        remaining: dict[str, Any] = {"step_values": {"one": {"province": "TX"}}}
        payload = _handle_form_step(
            "flow-2254",
            self._step("one"),
            remaining,
            ignored,
            set(),
            _ReuseState(),
            keep_current_values=True,
        )
        assert payload == {"province": "TX"}
        assert "step_values" not in payload
        assert ignored == set()

    def test_an_unvisited_step_id_is_reported_not_silently_dropped(self) -> None:
        """A typo'd step_id must not quietly do nothing."""
        remaining: dict[str, Any] = {
            "province": "BY",
            "step_values": {"typo_step": {"province": "TX"}},
        }
        _handle_form_step(
            "flow-2254",
            self._step("one"),
            remaining,
            None,
            set(),
            _ReuseState(),
            keep_current_values=True,
        )
        warnings = _ignored_keys_warnings(set(), remaining)
        assert any("never presented" in w and "typo_step" in w for w in warnings), (
            f"An unapplied step_values entry went unreported: {warnings}"
        )

    def test_a_consumed_step_entry_is_not_reported(self) -> None:
        remaining: dict[str, Any] = {"step_values": {"one": {"province": "TX"}}}
        _handle_form_step(
            "flow-2254",
            self._step("one"),
            remaining,
            None,
            set(),
            _ReuseState(),
            keep_current_values=True,
        )
        assert _ignored_keys_warnings(set(), remaining) == []

    def test_a_schemaless_step_does_not_submit_the_reserved_key(self) -> None:
        """The legacy no-data_schema path dumps every key; not this one."""
        remaining: dict[str, Any] = {
            "host": "10.0.0.5",
            "step_values": {"one": {"province": "TX"}},
        }
        payload = _handle_form_step(
            "flow-2254",
            {"type": "form", "step_id": "one"},
            remaining,
            None,
            set(),
            _ReuseState(),
            keep_current_values=True,
        )
        assert payload == {"host": "10.0.0.5"}


class TestBackfillIsNotCallerConsumption:
    """Schema data must not make a config of typos look partially applied."""

    async def test_backfill_alone_still_raises_the_empty_forms_error(self) -> None:
        submit_fn = AsyncMock(
            side_effect=[{"type": "create_entry", "result": {"entry_id": "e"}}]
        )

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=None,
                flow_id="flow-2254",
                initial_step=_workday_options_step(),
                config={"days_ofset": 3},
                submit_fn=submit_fn,
                keep_current_values=True,
            )

        assert "without consuming any of the supplied" in str(exc_info.value)
        # The step still went out fully populated — the failure is about what
        # the caller asked for, not about what was submitted.
        assert submit_fn.await_args_list[0].args[1]["province"] == "BW"

    async def test_reconfigure_backfill_does_not_trip_the_incomplete_guard(
        self,
    ) -> None:
        """Backfilled keys are HA's, so "consumed EVERY key" is unaffected."""
        step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2254",
            "step_id": "reconfigure",
            "data_schema": [
                {
                    "name": "host",
                    "required": True,
                    "description": {"suggested_value": "10.0.0.1"},
                },
                {
                    "name": "port",
                    "default": 80,
                    "description": {"suggested_value": 8123},
                },
                {"name": "verify_ssl", "description": {"suggested_value": False}},
            ],
        }
        abort_step: dict[str, Any] = {
            "type": "abort",
            "reason": "reconfigure_successful",
        }
        submit_fn = AsyncMock(side_effect=[abort_step])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2254",
            initial_step=step,
            config={"host": "10.0.0.9"},
            submit_fn=submit_fn,
            is_reconfigure=True,
            keep_current_values=True,
        )

        assert submit_fn.await_args_list[0].args[1] == {
            "host": "10.0.0.9",
            "port": 8123,
            "verify_ssl": False,
        }
        assert result["operation"] == "reconfigured"
        assert "warnings" not in result

    async def test_reconfigure_still_reports_a_key_no_step_declared(self) -> None:
        step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2254",
            "step_id": "reconfigure",
            "data_schema": [
                {"name": "host", "required": True},
                {"name": "port", "description": {"suggested_value": 8123}},
            ],
        }
        submit_fn = AsyncMock(
            side_effect=[{"type": "abort", "reason": "reconfigure_successful"}]
        )

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=None,
                flow_id="flow-2254",
                initial_step=step,
                config={"host": "10.0.0.9", "portt": 1},
                submit_fn=submit_fn,
                is_reconfigure=True,
                keep_current_values=True,
            )

        assert ReconfigureStatus.APPLIED_BUT_INCOMPLETE in str(exc_info.value)


class TestSubentryWalker:
    """``ha_config_set_helper(helper_type='config_subentry')`` gets both halves."""

    @staticmethod
    def _step() -> dict[str, Any]:
        return {
            "type": "form",
            "flow_id": "flow-sub",
            "step_id": "reconfigure",
            "data_schema": [
                {"name": "model", "required": True},
                {
                    "name": "prompt",
                    "description": {"suggested_value": "You are helpful."},
                },
                {"name": "temperature", "default": 1.0},
            ],
        }

    async def test_reconfigure_keeps_the_fields_the_caller_did_not_name(self) -> None:
        client = AsyncMock()
        client.submit_config_subentry_flow_step = AsyncMock(
            side_effect=[{"type": "abort", "reason": "reconfigure_successful"}]
        )

        result = await _handle_config_subentry_flow_steps(
            client,
            "flow-sub",
            self._step(),
            {"model": "gemma3:27b"},
            is_reconfigure=True,
            keep_current_values=True,
        )

        submitted = client.submit_config_subentry_flow_step.await_args_list[0].args[1]
        assert submitted == {
            "model": "gemma3:27b",
            "prompt": "You are helpful.",
        }
        assert result["operation"] == "reconfigured"

    async def test_create_submits_only_the_caller_keys(self) -> None:
        client = AsyncMock()
        client.submit_config_subentry_flow_step = AsyncMock(
            side_effect=[{"type": "create_entry", "result": {"entry_id": "sub-1"}}]
        )

        await _handle_config_subentry_flow_steps(
            client,
            "flow-sub",
            self._step(),
            {"model": "gemma3:27b"},
            is_reconfigure=False,
        )

        submitted = client.submit_config_subentry_flow_step.await_args_list[0].args[1]
        assert submitted == {"model": "gemma3:27b"}


class TestOptionsEntryPointWiring:
    """The live repro: ``ha_set_integration(entry_id=..., config=...)``."""

    async def test_options_update_posts_back_the_untouched_fields(self) -> None:
        client = AsyncMock()
        client.get_config_entry.return_value = {"domain": "workday"}
        client.start_options_flow.return_value = _workday_options_step()
        client.submit_options_flow_step = AsyncMock(
            return_value={
                "type": "create_entry",
                "result": {"entry_id": "entry-workday", "title": "Workday Sensor"},
            }
        )

        result = await update_config_entry_options(
            client, "entry-workday", {"days_offset": 3}
        )

        submitted = client.submit_options_flow_step.await_args_list[0].args[1]
        assert submitted == {
            "days_offset": 3,
            "workdays": ["mon", "wed", "fri"],
            "excludes": ["sun"],
            "province": "BW",
        }
        assert result["updated"] is True
        assert "warnings" not in result
