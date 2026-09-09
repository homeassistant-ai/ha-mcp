# Native dashboard edits

Approved scope: add a component-backed edit path and structured dashboard edits,
preserve existing tools and component-less behavior, prove on GitHub runners
before opening a draft PR. Existing workflows, runner configuration and shared
E2E fixtures must remain unchanged. Issue #2367 has not been reproduced; this is
an enhancement, not a claimed fix. All execution is remote.

## Contract

- Add `patch` to `ha_config_set_dashboard`: JSON Patch subset add/remove/replace/test,
  RFC 6901 pointers (including escaping), max 100 operations. Require config_hash.
  Mutually exclusive with config and python_transform. Preserve Python transforms.
- Pure patch evaluator returns a deep copy; failing operations never mutate the
  input. Require a dict result. Reject malformed paths, missing replace/remove/test
  targets, invalid array indexes and unsupported operations. Preserve literal strings.
- A new additive `dashboard_edit` capability/command accepts canonical url_path,
  expected_hash, and exactly one of replacement config or patch. Applies only to
  storage dashboards, using Core's default-dashboard lookup and async_load/async_save.
- Normalize Core config through its JSON encoder before hashing/copying. Hash stays
  byte-compatible with ha_mcp.utils.config_hash.compute_config_hash.
- Preload, then hash check, copied patch/replacement validation and invoking async_save
  have no yielding await between compare and Core's cached-config replacement. Never
  mutate Core's live config while preparing an edit. Preserve strategy restrictions.
- Saves use Core's API to preserve cache invalidation and lovelace_updated. API save
  completion is not a guarantee of durable storage (Core logs some disk failures).
- Capability absence or definitive unknown_command permits legacy fallback. A sent
  write timeout, malformed response or save exception never triggers a second write.
  Return an explicit unknown outcome for ambiguous transport/save failure.
- Python transforms continue executing in HA-MCP, with the component command doing
  the final hash compare, save and readback in one call. No Python runs on Core's loop.
- Existing config/metadata/create/delete/strategy/default/YAML semantics, BPS,
  backups, screenshots and response fields remain. Structured edits also work through
  the legacy fetch/apply/save/readback route when the component is absent.
- Keep component version aligned with release-cycle policy; both entry types expose
  the capability without requiring privileged File & YAML services.

## Validation

Unit tests cover actual patch results and rollback, component save behavior, hash
conflicts, default resolution, strategy handling, independent update versions and
no-retry on ambiguous writes. Existing dashboard unit/E2E tests must pass. Add
cross-topology E2E edits and stale-hash tests; use the reporter's exact dashboard and
transforms for measurements. Existing manual pr.yml and haos-e2e-tests.yml dispatches
validate the branch before PR creation. No workflow or runner modifications.
