# Statistics and Energy Dashboard inspection

`ha_get_history(source="statistics")` reads recorder values and
`recorder/get_statistics_metadata` from the connected Home Assistant.
`unit_of_measurement` is Core's **display unit**, the unit Core uses for the
returned numbers with its default conversion. `statistics_metadata` preserves
Core's response, including `statistics_unit_of_measurement`,
`display_unit_of_measurement`, `unit_class`, and aggregation capabilities.
The stored unit can differ from the output unit.

`unit_source="recorder_metadata"` identifies a resolved unit. Missing or
unavailable metadata leaves the output unit null with `unit_source="unknown"`,
a `unit_reason`, and a warning. Metadata reporting a genuinely unitless
statistic is distinguished by `unit_reason="statistics_are_unitless"`.
Current entity attributes are never silently substituted for recorder metadata.
Metadata is independent of pagination, including empty pages. Core's row fields,
including interval end timestamps, are preserved.

To discover the statistics configured in the Energy Dashboard, call
`ha_manage_energy_prefs(mode="get", include_statistics=True)`. Its
`statistics_metadata` lists the referenced source, device, cost, and rate
statistics and their metadata. Missing references have explicit reasons.
This inspection does not change preferences or their configuration hashes.
Pass a returned statistic ID to `ha_get_history` to retrieve its readings.

Neither path requires the custom component. Both use the running Core's native
recorder API, including Core's handling of statistics without a current entity.
The default follows Core's display conversion. Explicit units are described below.

## Native request options and schema discovery

`include_schema=True` on `ha_get_history`, or on energy `mode="get"`, includes
the running Core's contract when the component supports discovery. Descriptions
are marked complete only when strict serialization succeeds, including nested
validators; unsupported callables produce an explicit incomplete-description warning. Discovery failures preserve the read
result and produce a top-level warning; energy dry runs still invoke the
actual registered save schema. Without the capability, previews explicitly
report `proposal_validation.status="unavailable"` and `partial=True`.

Statistics types go directly to Core, including `last_reset` and future types.
Omitting `statistic_types` uses Core's defaults; the response reports types
observed in the returned rows.

Reset timestamps use a supplementary native query in the stored unit when
conversion is possible: affected Core versions otherwise convert timestamps
along with numeric values. Queries are grouped by native unit class and stored
unit. Core converter metadata handles missing unit classes for both default
and explicit conversions, and avoids extra queries for unchanged classes.
Without that metadata, a query using unchanged default units can recover resets
without inferring a converter. If the timestamp cannot be recovered, `last_reset` is omitted with a
warning; other values remain available.

A reported null display unit is a known unitless result, including conversions
from percent. `core_options` passes additional native recorder options, such as
`{"units": {"energy": "MWh"}}`. It cannot override the tool's controlled
query fields. Explicit-unit labels use Core's converter through the component;
without it, values are preserved and explicitly requested classes remain unknown.
Unrequested classes retain their native display-unit labels. Core parameter
rejections retain Core's message and are reported as invalid parameters, rather
than as recorder service failures.

History rows rename only the compact keys Core actually returns, once:

| Returned field | Core field | Meaning |
|---|---|---|
| `state` | `s` | State value |
| `attributes` | `a` | Attributes, when included |
| `last_updated` | `lu` | Last updated, as a local ISO timestamp |
| `last_changed` | `lc`, otherwise `lu` | Local ISO timestamp; see fallback caveat below |

History event timestamps use Home Assistant's configured timezone, including DST,
with UTC as the fallback when that timezone is unavailable. When Core omits
`lc`, `last_changed` falls back to `last_updated`. This may not be the actual
state-change time: default `significant_changes_only` queries omit `lc` even
for attribute-only updates, and a window-start snapshot uses the query boundary.
A failed timezone lookup produces an explicit response-level warning. Attributes and unknown fields are not rewritten, and compact aliases
are not duplicated. Minimal responses may omit attributes. New or renamed Core
fields pass through under
their native names. If a readable name collides with another native field, both
original fields are preserved under their Core names rather than overwritten.
The `fields` parameter selects keys within `data`; response-level warnings and
timezone metadata are always retained. `include_schema=True` also retains
`core_contract` through field projection.
Energy saves return the normalized Core configuration and hashes calculated
from it. Preferences and per-key hashes retain future top-level fields.
On an unconfigured installation without the component, `config` is empty
instead of fabricated defaults; use its full `config_hash` for the first save.

Energy `current_state_validation` and `post_save_validation` preserve native
semantic validation results. These checks concern persisted state, whereas
proposal validation checks the save schema without writing.
