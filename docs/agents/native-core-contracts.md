# Native Home Assistant contracts

Read this before changing tools that wrap Home Assistant APIs, whether they
read or write. Core owns its request schemas, defaults, conversions and
response fields. HA-MCP owns tool orchestration and safety policies.

## Choose the authoritative path

Use native REST/WebSocket APIs first. Preserve native response fields and
forward payload fields instead of selecting a copied allowlist. Fetch metadata
from the API that owns it; a current entity state is not a substitute for
recorder metadata. Use the result or readback of a mutation when computing
hashes, because Core may coerce values, add defaults or generate fields.

When the required contract is only available in process, add a narrow component
bridge to the actual Core schema, collection or flow. Update its capability,
consumer and explicit fallback together, on both component entry topologies.
Do not translate Core's validator into another handwritten validator. Native
schema serialization is discovery, not validation: callable validators cannot
always be serialized. Label incomplete descriptions and call the original
validator when validating a proposal.

`core_contract.py` demonstrates this for energy and recorder requests. Its
command allowlist is a bridge permission boundary, not a field/type schema.
It looks up the running command's registered validator and never executes the
handler during validation. Actual operations retain their native endpoints and
permission checks. The serializer adapter reveals the live alternatives in
Core's opaque discriminated unions; it supplies no energy types or fields.
Unknown serializer constructs remain explicitly incomplete.

## Preserve independent safeguards

Keep read-only gates, optimistic hashes, duplicate guards, query bounds,
pagination and protected command envelopes. Name these as HA-MCP policies,
not Core validation rules. Generic native options must not override the entity,
time or response-size inputs that the wrapper has already bounded.

Recorder scan estimation is a safety policy: Core currently performs calendar
alignment inside its query implementation and exposes no independent estimator.
Keep this conservative model explicit, fail closed for periods it cannot
estimate, and compare it against live Core queries. Do not disguise it as a
complete list of valid Core periods. Disabling this operator-controlled guard
still leaves Core responsible for request validity.

Without a component capability, retain native endpoints and disclose what
cannot be discovered or prevalidated. An energy dry run without the bridge is
an unvalidated preview, never “shape OK.” `energy/validate` examines persisted
state; it does not establish the semantic validity of a proposed configuration.
Core has no conditional energy save API, so snapshot hashes provide optimistic
conflict detection, not transaction isolation from native writers.

## Tests that catch drift

Exercise real Core through the MCP tool in E2E, including rejection paths and
fields that previously disappeared. Include component, server-entry-only and
absent-component behavior. Compare normalized save results with readback.

Unit tests should prove that future fields/types reach Core and native response
fields survive. Fake validators may prove delegation; do not reproduce the
energy schema in fixtures and then use those fixtures to claim Core parity.
When touching a wrapper, audit its whole request, validation, response and
metadata path. Correct related duplicated contracts in the same change.
