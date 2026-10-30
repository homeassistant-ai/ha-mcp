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

The component is part of the native contract, not an exception to it. The
`ha_mcp_tools` component runs inside Core, so a path through it reaches Core's
own registries, schemas, collections, flows and processors directly, the way
`ha_search` and the logbook read do. Prefer that path whenever the component
gives in-process access to the Core object a tool wraps, and keep Core's
REST/WebSocket API as the explicit fallback for an installation without the
component. Gate the choice on the component's `capabilities` handshake, and
update the component producer, server consumer, capability gate and fallback in
the same pull request, on both component entry topologies.

"The component" is the `ha_mcp_tools` integration's shared `ha_mcp_tools/*`
command surface, which both of its config-entry types serve: the HA-MCP Server
entry that runs the embedded server, and the File & YAML Tools entry. The
embedded server reaches every bridge through its own Server entry; a bridge
that works only while the File & YAML Tools entry is present is a bug: that
entry exists as the user's opt-in to filesystem and YAML access for the agent,
only the privileged filesystem and YAML services are tools-entry-only, and
nothing else belongs behind it. An installation "without the component" has
neither entry: a standalone, Docker or app server on a Home Assistant where
the integration is not installed.

A change that does not touch the component is held to the same rule. Where a
Core rule or value is copied by hand on the server side, the fix is a component
bridge to the actual Core schema, collection or flow, not a tidier copy. Do not
translate Core's validator into another handwritten validator, and do not keep
one because the WebSocket API cannot express what Core accepts: that is exactly
the case the component exists for. "It works without the component" does not
justify the copy; an installation without the component may get less, and the
fallback says so.

Component and server releases are paired: every release stamps both with one
version and the component pins the server build it runs. A bridge and its
consumer therefore ship together. For an external server that meets another
component version, the capability handshake is the gate for every bridge; only
the filesystem and YAML path adds its `MIN_COMPONENT_VERSION` floor. Version
skew is the gate's job, never a reason to avoid the component.

Both routes are native, in this order: the component bridge when its
capability is advertised, Core's REST/WebSocket API otherwise, and either one
over a copy in every case. Preserve native response fields and forward payload
fields instead of selecting a copied allowlist. Fetch metadata from the API that owns it; a current entity state is
not a substitute for recorder metadata. Use the result or readback of a
mutation when computing hashes, because Core may coerce values, add defaults or
generate fields.

Preserve useful presentation behavior when removing copied schemas. Readable
field names and local event timestamps can be retained without duplicate aliases
or filtering out new Core fields. Decode compact omissions according to Core's
semantics, and test both native-data preservation and the user-facing result.

Native schema serialization is discovery, not validation: callable validators
cannot always be serialized. Label incomplete descriptions and call the original
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

Todo item adds, updates and removals by name prefer the component's
`ha_mcp_tools/todo_item` bridge (capability `todo_item`). It resolves an item
value with Core's own `_find_by_uid_or_summary`, calls Core's todo services in
process (so their schema, feature checks and context apply) and reads the
stored rows back from the list entity. An update goes by the uid it resolved,
and the bridge refuses the update when an item listed before it is named after
that uid, since Core would change that item instead. An update that also moves
the item hands the bridge the uid to put it below: the bridge checks the list's
reorder feature first, writes the update on the order Core resolves it in, then
moves the item by uid as `todo/item/move` does, and a move that fails after the
update says the update was applied. It also refuses a write to an unavailable
list (see below). Only Core's schema and validation errors that left the rows
unchanged come back as a refusal; any other error leaves the outcome unknown. A
restore hands it the snapshot's `due`, and it picks `due_date` or
`due_datetime` with Core's own date validator. Without the component the tools
call Core's todo services through WebSocket `call_service`, so Core's schema
rejections reach the caller with their text (the REST service endpoint answers
them with a bare 400); on that path a move goes to `todo/item/move` before the
update. A move alone takes `todo/item/move` and `remove_completed` takes Core's
`remove_completed_items` on both paths.
`ha_get_todo` filters with `todo.get_items`. Core's services skip an
unavailable list, answering a write with success and `todo.get_items` with no
match, so the tools refuse a write to one and read its items with
`todo/item/list`, which still answers for a loaded list entity. A list that
Core holds only as its `restored` placeholder state (no loaded entity, for
example while its integration is not set up) is refused, as `todo/item/list`
answers it with "Entity not found"; a restore to it is refused as not applied.
With auto-backup on, the tools back up each item an update or a removal
changes, under its uid (its summary when it has none). A todo item backup
restores an edited item with `todo.update_item` under its uid, which the safety
backup and the diff also follow. The snapshot holds no position and Core's
services cannot set a uid, so a removed item comes back through `todo.add_item`
with a new uid, at the place the list gives new items.

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
