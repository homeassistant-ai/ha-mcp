# Issue 2201: Null Automation Trace States

## Problem

Home Assistant template-trigger traces can include `from_state` and `to_state`
keys whose values are `null`. This is expected when a template is re-evaluated
by a time dependency rather than an entity state-change event. The detailed
trace formatter currently calls `.get("state")` on those values and turns a
valid trace into an `INTERNAL_ERROR`.

## Design

Keep the existing detailed-trace response shape, including the state keys when
Home Assistant supplied them, but normalize a null state object before reading
its `state` field. Entity-backed template triggers continue to return their
state strings; time-driven template triggers return `null` for both state
fields.

## Regression coverage

- Add a focused unit regression using the trace payload shape observed from
  Home Assistant, with both state keys present and null.
- Add an E2E regression that creates a device-free template-trigger
  automation, lets a `now()` dependency trigger it naturally, then requests
  the detailed trace and verifies both state fields are null.
- Run the focused unit test, Ruff format/check, and mypy locally. The E2E test
  runs only in Linux CI per the maintainer's local-test policy.
