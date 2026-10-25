# Native Home Assistant contracts

Read this before changing tools that wrap Home Assistant APIs, whether they
read or write. Core owns its request schemas, defaults, conversions and
response fields. HA-MCP owns tool orchestration and safety policies.

Native covers Core, the frontend, Supervisor and the hassio proxy alike. When a
tool has more than one native path (Core REST vs WebSocket, Supervisor vs
Core), a migration keeps every capability the current tool has and adds the
native ones; it never trades one native capability for another.

A tool not yet migrated under #2632 gets no piecemeal changes to its non-native
paths, and that includes bug fixes. The whole tool is redone when it is
migrated, so those changes would be thrown away when it is reworked. Fix a bug
in such a tool by migrating the affected path to the native approach this
document describes, not by patching it so it keeps working the old way.

## Choose the authoritative path

Use native REST/WebSocket APIs first. Preserve native response fields and
forward payload fields instead of selecting a copied allowlist. Fetch metadata
from the API that owns it; a current entity state is not a substitute for
recorder metadata. Use the result or readback of a mutation when computing
hashes, because Core may coerce values, add defaults or generate fields.

Preserve useful presentation behavior when removing copied schemas. Readable
field names and local event timestamps can be retained without duplicate aliases
or filtering out new Core fields. Decode compact omissions according to Core's
semantics, and test both native-data preservation and the user-facing result.

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

`websocket_api/logbook.py` is the same pattern for logbook. The component runs
Core's own `EventProcessor` with the REST view's flags (`timestamp=False` for
ISO `when`, `include_entity_name=True`) plus the `logbook/get_events` filter
set, so names, context fields and the row shape come from Core instead of a
copied `EntityNameCache`. The server prefers the bridge and keeps Core's REST
(`/api/logbook`) and WebSocket (`logbook/get_events`) reads as the explicit
component-absent fallbacks — a `device_id` request needs the WebSocket path
there because REST has no device filter, and a bridge timeout surfaces rather
than re-running the recorder query on another transport.

`ha_restart` forwards Core's restart schema (`safe_mode`) and leaves the
refusals to Core. The embedded server is the exception: it replies before the
restart fires, so a Core refusal would reach nobody. There it reads what Core
would refuse on before replying: the config check, `recorder/info`'s
`migration_in_progress`, and `backup/info`'s `state` (any value but `idle`, the
same test the frontend's restart dialog applies). When the recorder or the
backup manager does not answer, it says so in `warnings` instead of claiming
the check passed.

Some API meanings are not discoverable as schemas: Core declares Energy
statistic references as plain strings. The optional metadata enrichment uses
Core's `stat_*` / `included_in_stat` naming convention; it is not an authoritative
reference catalog for hypothetical differently named future fields. Preserve
the complete native preferences regardless, and do not turn ordinary strings
that happen to match statistic IDs into asserted references.

## Frontend and custom-card semantics

Dashboard configuration has no Core schema. Its meaning lives in the
installed frontend's card implementations and editors, and in each loaded
custom card. The owner of a key such as `card`, `cards` or `styles` is the
card that reads it; `custom:button-card` keeps CSS lists under `styles.card`,
and a stack keeps child cards under `cards`.

- Read card forms, field types and validators from the installed frontend
  (`custom_components/ha_mcp_tools/card_definitions.py`) and card-level
  errors from the loaded custom card itself. Where neither can be inspected
  (no component, a card that needs browser APIs), say that discovery is
  incomplete.
- Nested-card discovery for search and checks is a heuristic over the keys
  HA's own container cards use. Document it as discovery; never present it
  as a frontend contract, and never add per-card exceptions to it.
- A search or save warning may describe only what HA-MCP could not read
  ("not searched as cards") or what the frontend or the card reported.
  Never call a configuration malformed, misshapen or in need of repair
  because it did not fit a handwritten shape rule (issue #2729).
- Test against real frontend and custom-card behavior, including valid
  configurations that reuse container names for ordinary options.

## Preserve independent safeguards

Keep read-only gates, optimistic hashes, duplicate guards, query bounds,
pagination and protected command envelopes. An empty statistics-type list is
rejected by the wrapper because it requests timestamps without any values. Name these as HA-MCP policies,
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
