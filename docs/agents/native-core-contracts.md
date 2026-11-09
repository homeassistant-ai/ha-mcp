# Native Home Assistant contracts

Read this before changing tools that wrap Home Assistant APIs, whether they
read or write. Core owns its request schemas, defaults, conversions and
response fields. HA-MCP owns tool orchestration and safety policies.

Native covers Core, the frontend, Supervisor and the hassio proxy alike. A
migration rebuilds the tool on the native path that serves it best; the
transport, parameters and response shape the tool has today are not
constraints. When more than one native path exists (Core REST vs WebSocket,
Supervisor vs Core), compare what each accepts and returns (error text and
codes, contexts, fields) and build on the one that does more, keeping the other
only for a capability the first lacks. Every capability an agent has today
stays reachable; how the old tool reached it does not have to.

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
the same pull request, on both component entry topologies. A path can be
component-only by design when the fallback would be a copy of Core's rules:
the reference check on automation, script and scene writes is one, since
Core's API validates a config's trigger, condition and action lists and serves
the target lookups and registries, but does not collect an unsaved config's
references, and a server-side copy of that part is the hand-kept walker it
replaced. Without the component the write says the
references went unchecked.

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

`ha_call_event` prefers the component's `ha_mcp_tools/call_event` bridge
(capability `call_event`). The bridge reads `hass.bus.async_listeners()` and
calls `hass.bus.async_fire()` in one pass, returning the event's context and
the exact-event and `MATCH_ALL` listener counts. Without the component the tool
fires Core's WebSocket `fire_event`, which returns the same context; listener
counts come from `GET /api/events` before the fire. `state_changed` stays on the
REST `POST /api/events/{event_type}` route because only `APIEventView` rebuilds
its `old_state`/`new_state` dicts into State objects. A WebSocket command that
provably was not sent falls back to that REST route and says so in `warnings`;
a timeout or dropped socket after the send returns `partial` rather than an
error that invites a retry. `context_id` is the event context's `id`; an
automation triggered by the event records it as `parent_id` in its trace.

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

The automation config tools prefer the component's
`ha_mcp_tools/automation_config` bridge (capability `automation_config`).
Its `validate` runs Core's own automation validator in process and answers
Core's validation status and humanized error, so a config Core would refuse
is refused before the write, naming the section Core failed on. Its
`describe` returns Core's registered trigger or condition descriptions;
when Core's error names an unknown trigger or condition key, the closest
live keys and the matched description come from there, never from a
hand-kept list. Its `lookup` returns the automation's entity registry row
(the row `config/entity_registry/get` sends), whether Core has loaded the
automation (`loaded`), the load failure Core recorded in its
`{entity_id}_validation_*` repair issue, and the entities, devices, areas,
floors, labels and blueprint Core says the automation references. References
are None while Core has not loaded the automation (disabled, or it failed to
load), and the get tool's `warnings` says so; the unavailable placeholder
entity Core adds for a failed automation, whose reference readers answer
empty sets, counts as not loaded. Without the component the config goes to
Core's REST config endpoint and its 400 text leads the error; WebSocket
`validate_config` then names the failed section, the closest keys come from
the first event of `trigger_platforms/subscribe` or
`condition_platforms/subscribe`, the registry row from
`config/entity_registry/get` and the load failure from
`repairs/list_issues`. Core serves no references over its API there, so the
get tool carries Core's `search/related` answer as `related` instead and
`warnings` says so: the Related set the frontend shows, which also holds the
devices, areas, floors and integrations of what the automation uses and the
members of groups, scenes and scripts it names, so it stays apart from
`references`. On both paths the
config reaches Core as written and Core's text is never rewritten: no root
key is pluralised, no alias or trigger key is required or renamed by the
tool. Core does its own renaming while it validates a write (Core's
backward-compat validators: `cv.renamed` for the root keys, the service
schema's own rename for `service`; both rename in place and the config view
stores that dict), so a body written with legacy keys is stored and read back
in Core's form; a trigger's `platform` key stays. The stored body comes from
REST `GET /api/config/automation/config/<id>`, which Core reads under its
mutation lock; WebSocket `automation/config`
returns the entity's `raw_config`, which stays stale until the reload after
a write completes. An `automation.x` entity_id resolves to its config id
through the entity registry first, so a registry-disabled automation, which
has no state, still resolves; the state's `id` attribute is the fallback.
`operation` is `created` when Core held no config under the id before the
write and `updated` otherwise, and an `automation.x` that does not resolve
is not found, never a create. Core's DELETE answers 400 "Resource not
found" for a missing id, which the remove tool reports as not found. An
automation the registry resolves but Core's config API answers "Resource not
found" for is defined in YAML or a package, outside `automations.yaml`; the
get, set and remove tools report it as CONFIG_NOT_FOUND. A config id with no
registry row returns the body Core stores under it, with `entity` None; only
an id Core stores nothing under is not found. References in a proposed
automation, script or scene come from the component's
`ha_mcp_tools/config_references` bridge (capability `config_references`):
Core's own validator runs on the config (blueprint inputs applied), Core's
`Script` and trigger/condition helpers collect its entities, devices, areas,
floors and labels as Core's automation entity does, and Core's state machine,
registries and services say which are missing (Core's match sentinels never
are). Each entity target also goes to Core's `async_get_services_for_target` /
`async_get_triggers_for_target` / `async_get_conditions_for_target`, the
lookups behind the editor's target pickers, and an entity Core does not offer
for its item is reported with what Core does offer (`offered`). The server
keeps no copy of these rules: when the component is absent or outdated, the
check fails, or Core refuses the config, `validation.references_unchecked`
says why nothing was checked. The tools' own checks are HA-MCP
policies, not Core rules: the `config_hash` optimistic lock, the
alias-replacement guard, the guard against a config `id` passed without an
entity_id, the config-`id` mismatch guard, write-mode exclusivity, keeping
the `enabled` and `category` parameters out of the stored config, blueprint
empty-list stripping, the best-practice checker and the reference
validator.

An integration's own `<domain>/` WebSocket commands, the API its panel uses,
are listed through the component, which reads Core's registry
(`hass.data["websocket_api"]`) and serializes each command's registered request
schema; a command registered without fields (schema `False`) is described with
Core's `BASE_COMMAND_MESSAGE_SCHEMA`. A call is sent over WebSocket as given:
Core validates it against that schema before the handler runs, so the tool
passes Core's error through instead of validating first.

A config entry's `options` are read through the component's `config_entries`
command, which returns Core's entry row with the stored mapping. Core's REST
and WebSocket entry reads carry no `options` and Core has no read API for
them, so without the component `ha_get_integration` returns an entry that has
an options flow without `options` and says so in `warnings`.
`ha_get_integration` opens an entry's options flow only when the caller asks
for its form (`include_schema`), as the frontend opens it only when the user
chooses Configure: the flow's first step is the integration's own code and may
act (#2987). Only that form marks which fields are passwords, so under
`redact_secrets` a read that does not ask for it withholds every option value
of an entry that has an options flow. That is an HA-MCP policy, not a Core
rule. On an installation without the component, `ha_search` reads a flow
helper's config through its options flow only for Home Assistant's own helper
flows, the `core.helper` domains of `integration/descriptions`; a custom helper
integration's entry is matched on its title, its config is withheld as
`options_withheld: custom_integration` (the component's own marker), and the
result is `partial`.

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
