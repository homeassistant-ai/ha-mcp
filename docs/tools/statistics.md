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
The history tool currently follows Core's default display conversion; it does
not offer an explicit output-unit override.
