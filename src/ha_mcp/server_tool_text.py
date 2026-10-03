"""Static text the server publishes about its tools.

Holds the search tool description, the BM25 keyword boosts, the lite
docstring variants with their destinations, and the skill keyword block.
``HomeAssistantSmartMCPServer`` exposes each one as a class attribute.
"""

from __future__ import annotations

# Description for the unified search tool
SEARCH_TOOL_DESCRIPTION = (
    "Search ALL Home Assistant tools by ENGLISH keyword. Returns matching "
    "tools with descriptions, parameters, and annotations "
    "(read/write/delete). Categories: entities, states, automations, "
    "scripts, dashboards, helpers, HACS, calendar, zones, labels, groups, "
    "areas, floors, history, statistics, devices, integrations, services, "
    "backups, todo, camera, blueprints, system, and more.\n\n"
    "Tools already in your tool list are callable directly \u2014 no search "
    "needed. If one matches a search, it comes back as a name-only stub "
    "(pinned: true); use the schema you already have.\n\n"
    "WORKFLOW:\n"
    "1. ha_search_tools(query='...') \u2014 find tools (this tool). Query "
    "in English keywords naming the operation ('get entity state'); "
    "translate other languages first, keep entity/area names as-is.\n"
    "2. Execute: call the tool DIRECTLY by name (preferred), or use "
    "a proxy for permission gating:\n"
    "   - ha_call_read_tool \u2014 readOnlyHint tools (safe, no side effects)\n"
    "   - ha_call_write_tool \u2014 destructiveHint tools that create/update\n"
    "   - ha_call_delete_tool \u2014 destructiveHint tools that remove/delete\n"
    "Once you know a tool name, call it directly \u2014 no need to search "
    "again.\n\n"
    "If using proxies, call with TWO top-level params:\n"
    '   ha_call_read_tool(name="ha_search", arguments={"query": "..."})\n'
    "   Do NOT nest name/arguments inside the arguments param.\n"
    "   Call proxy tools SEQUENTIALLY, not in parallel.\n\n"
    "ALWAYS search before assuming a capability is unavailable. "
    "Most tools are discoverable only through this search."
)

# Extra keywords appended to tool descriptions for BM25 ranking.
# Applied unconditionally via SearchKeywordsTransform so they also
# improve retrieval for Claude's native deferred-tool search on
# claude.ai, which indexes tool names and descriptions with BM25
# (no semantic matching). Original tool docstrings stay unchanged;
# these keywords are appended by the transform at list-tools time.
SEARCH_KEYWORDS: dict[str, str] = {
    # s02: "find entities or configs" → ha_search outranks more specific tools
    "ha_search": (
        "find entities configs lookup discover search lights sensors switches "
        "covers climate fans media_player binary_sensor device_tracker "
        "person weather automation script helper input_boolean input_number "
        "automations scripts scenes helpers dashboards"
    ),
    # s07: "get/read automation" → ha_config_get_automation should outrank set
    "ha_config_get_automation": (
        "read inspect fetch view existing automation config triggers "
        "conditions actions get show detail"
    ),
    # s09: "create helper" → ha_config_set_helper should outrank remove_helper
    # Covers all 29 helper types (12 simple + 17 flow-based, unified in #967).
    "ha_config_set_helper": (
        "create update new add helper "
        "input_boolean input_button input_number input_text input_datetime "
        "input_select counter timer schedule zone person tag "
        "template group utility_meter derivative min_max threshold "
        "integration statistics trend random filter tod "
        "generic_thermostat switch_as_x generic_hygrostat "
        "history_stats mold_indicator"
    ),
    # Boost tools that compete with ha_search for common queries
    "ha_config_get_script": (
        "read inspect fetch view existing script config sequence "
        "actions get show detail"
    ),
    "ha_config_list_helpers": (
        "list all helpers input_boolean input_number input_text "
        "counter timer input_datetime input_select"
    ),
    "ha_get_entity": ("get entity state attributes details single specific entity_id"),
    # #2576: BM25 has no stemming, so "lights" never matched "light"
    "ha_get_state": (
        "get current state value single entity check status bulk multiple states "
        "which lights are on off"
    ),
    "ha_config_set_automation": (
        "create update modify edit automation triggers conditions actions "
        "new automation write save take control blueprint detach "
        "unlink standalone convert enable disable turn on off run trigger now"
    ),
    "ha_config_set_script": (
        "create update modify edit script sequence actions new script write "
        "save take control blueprint detach unlink standalone convert "
        "run start stop execute"
    ),
    "ha_config_set_scene": (
        "create update modify edit scene entities snapshot activate apply turn on"
    ),
    "ha_manage_updates": (
        "update updates install skip firmware core os repair repairs issue "
        "ignore dismiss unignore"
    ),
    "ha_set_integration": (
        "integration config entry enable disable add options reconfigure "
        "log level debug logging"
    ),
    "ha_config_set_yaml": (
        "edit yaml configuration.yaml packages template sensor "
        "binary_sensor command_line rest mqtt knx platform yaml-only "
        "config file modify add remove replace"
    ),
    "ha_manage_app": (
        "manage app apps addon add-on configure settings options port network boot "
        "watchdog auto_update supervisor ingress proxy websocket api rest "
        "esphome nodered node-red frigate mosquitto mqtt zigbee2mqtt zigbee "
        "z-wave zwave appdaemon hacs studio code server file editor terminal "
        "ssh samba grafana influxdb deconz motioneye compile validate upload "
        "deploy firmware ota flash yaml device logs flows events stats"
    ),
    # #2322: the Energy Dashboard's tariffs live in .storage/energy, not
    # in the state machine, so an agent hunting for electricity prices
    # searches entities, finds none, and invents input_number helpers.
    # The docstring says "cost tariffs" but never "price", "peak" or
    # "kWh" — the words agents actually query with — and the title reads
    # write-only ("Manage ..."), so lead the boost with the read verbs.
    "ha_manage_energy_prefs": (
        "read get inspect energy dashboard preferences prefs "
        "electricity price prices pricing tariff tariffs rate rates "
        "cost costs kwh peak off-peak offpeak contract utility bill "
        "grid solar battery gas water consumption "
        "number_energy_price entity_energy_price stat_energy_from"
    ),
    # Old tool names from before the #2329 consolidation, plus the verbs
    # the merged tool gained. An agent that still knows ha_get_blueprint /
    # ha_import_blueprint routes to the replacement instead of failing
    # tool lookup.
    "ha_manage_blueprints": (
        "blueprint blueprints import delete remove unused substitute "
        "take-control list ha_get_blueprint ha_import_blueprint"
    ),
    # Old tool names from before #1134 consolidation. BM25 retrieval
    # on agents that still know the previous catalog ("call
    # ha_list_resources", "use ha_get_skill_home_assistant_best_practices")
    # routes them to the replacement instead of failing tool lookup.
    "ha_get_skill_guide": (
        "best practices skill skills guide guides reference references "
        "documentation docs help tutorial automation script scene helper "
        "dashboard "
        "ha_list_resources ha_read_resource list_resources read_resource "
        "ha_get_skill_home_assistant_best_practices "
        "ha_get_skill_home_assistant home_assistant_best_practices"
    ),
}

# Lite docstrings — beta opt-in (enable_lite_docstrings, #1062).
# Each entry replaces the full docstring on a heavy tool with a
# shorter variant that defers schema/example detail to
# ha_get_skill_guide. Every entry preserves
# a pointer to that skill so the LLM still has a path to the full
# guidance from inside the trimmed description. The trade-off
# (LLMs that skip the skill tool get less guidance) is surfaced in
# the dev-addon toggle, docs/beta.md, and a startup WARNING.
LITE_DOCSTRINGS: dict[str, str] = {
    "ha_config_get_automation": (
        "Get a Home Assistant automation configuration by "
        "entity_id or unique_id. Returns the full config "
        "(trigger, condition, action, mode) plus a stable "
        "config_hash for use with python_transform on "
        "ha_config_set_automation.\n\n"
        "For schema and field-level details, see "
        "ha_get_skill_guide."
    ),
    "ha_config_set_automation": (
        "Create or update a Home Assistant automation.\n\n"
        "Supports three modes: full `config` replacement, surgical "
        "`python_transform` on an existing automation (requires "
        "`identifier` and `config_hash` from "
        "ha_config_get_automation), or `take_control_of_blueprint` "
        "to convert a blueprint-backed automation into an editable "
        "standalone one (the UI's Take control action). Omit "
        "`identifier` to create a new automation. Reusing an identifier targets "
        "the same automation; changing its alias requires config_hash from a prior read. "
        "`enabled` turns it on or off and `run_actions` runs it now, with a "
        "write or alone with `identifier`.\n\n"
        "For schema details, examples, and native-vs-template "
        "guidance, see ha_get_skill_guide or your locally "
        "installed skills."
    ),
    "ha_config_get_script": (
        "Get a Home Assistant script configuration by "
        "script_id or entity_id. Returns the full config (sequence, "
        "mode, fields) plus a stable config_hash for use with "
        "python_transform on ha_config_set_script.\n\n"
        "For schema details, see "
        "ha_get_skill_guide."
    ),
    "ha_config_set_script": (
        "Create or update a Home Assistant script.\n\n"
        "Supports three modes: full `config` replacement, surgical "
        "`python_transform` on an existing script (requires "
        "`config_hash` from ha_config_get_script), or "
        "`take_control_of_blueprint` to convert a blueprint-backed "
        "script into an editable standalone one. `script_id` names "
        "the script in every mode. `run` ('start' / 'stop'), used alone "
        "with `script_id`, starts or stops the script.\n\n"
        "For schema details and examples, see "
        "ha_get_skill_guide or your locally installed skills."
    ),
    "ha_config_get_scene": (
        "Get a Home Assistant scene configuration by scene_id or entity_id, "
        "or omit scene_id to list/search scenes. Use query for names/IDs "
        "and search_in_config=True for full stored attribute values. "
        "Pass a returned scene_id to get the full config plus a "
        "stable config_hash for use with python_transform on "
        "ha_config_set_scene. Integration-managed scenes have no editable "
        "storage config; partial content searches are not exhaustive.\n\n"
        "For schema details, see "
        "ha_get_skill_guide."
    ),
    "ha_config_set_scene": (
        "Create or update a Home Assistant scene.\n\n"
        "Supports two modes: full `config` replacement, or surgical "
        "`python_transform` on an existing scene (requires "
        "`config_hash`). `scene_id` names the scene in both modes. "
        "`activate` activates the scene, with a write or alone.\n\n"
        "For schema details and examples, see "
        "ha_get_skill_guide or your locally installed skills."
    ),
    "ha_config_list_helpers": (
        "List Home Assistant helpers of a given type, one page per "
        "call (`limit`/`offset`; `total_count` and `has_more` "
        "describe the full set). The 12 storage-backed types "
        "(input_button, input_boolean, input_select, input_number, "
        "input_text, input_datetime, counter, timer, schedule, "
        "zone, person, tag) are listed on every install. Flow-based "
        "types (template, group, utility_meter, derivative, and the "
        "rest) and `helper_type='all'` are served only through the "
        "ha_mcp_tools custom component.\n\n"
        "For per-type schemas and decision guidance, see "
        "ha_get_skill_guide."
    ),
    "ha_config_set_helper": (
        "Create or update a Home Assistant helper. Supports all "
        "supported helper types: the simple types (input_*, "
        "counter, timer, schedule, zone, person, tag) and the "
        "flow-based types (template, group, utility_meter, "
        "derivative, statistics, trend, threshold, filter, "
        "switch_as_x, and others).\n\n"
        "Field set is delivered as `data_schema` on the first "
        "validation error — submit once and self-correct. For "
        "decision matrix and worked examples (which helper type "
        "for which use case), see ha_get_skill_guide or your "
        "locally installed skills."
    ),
    "ha_config_get_dashboard": (
        "Get Home Assistant dashboard info (list mode, search "
        "mode, or full config).\n\n"
        "Three modes: (1) list — `list_only=True` returns all "
        "storage-mode dashboards with metadata. (2) search — pass "
        "any of `entity_id`, `card_type`, `heading` to find cards "
        "(including nested ones, with a `python_path`) inside a "
        "specific dashboard; the "
        "result includes a `config_hash` you can pair with "
        "ha_config_set_dashboard(python_transform=...) to edit "
        "matched cards surgically. (3) get — no search params "
        "returns the full Lovelace config plus a stable "
        "`config_hash`. Use `url_path='default'` for the main "
        "dashboard. For known JSON Pointer paths, use "
        "ha_config_set_dashboard(patch=..., config_hash=...).\n\n"
        "For card-type taxonomy and search workflow examples, see "
        "ha_get_skill_guide."
    ),
    "ha_config_set_dashboard": (
        "Create or update a Home Assistant dashboard.\n\n"
        "Three modes: full `config` replacement for new dashboards or "
        "restructures; `patch` for known JSON Pointer paths with literal "
        "values (add/remove/replace/test, up to 100 operations; use `-` "
        "to append to arrays); `python_transform` for loops and pattern-based "
        "edits. Both edit modes require `config_hash` from "
        "ha_config_get_dashboard. Choose one mode for content changes; "
        "omit all three for metadata-only calls. With patch, change "
        "sidebar metadata in a separate call. Use `url_path` of 'default' "
        "or 'lovelace' for the built-in dashboard.\n\n"
        "For card types, layout patterns, and python_transform "
        "security rules, see "
        "ha_get_skill_guide or your locally installed skills."
    ),
    "ha_call_service": (
        "Call any Home Assistant service or one-shot WebSocket command: "
        "the catch-all escape hatch. Prefer a dedicated tool when one "
        "covers the job (automations, scripts, scenes, apps, updates and "
        "repairs, integration log levels). Calls `<domain>.<service>` "
        "(e.g., light.turn_on, climate.set_temperature). Use "
        "ha_search to find entity IDs and ha_get_state "
        "to read current values before changing them.\n\n"
        "For service-parameter details and per-domain guidance, "
        "see ha_get_skill_guide."
    ),
    "ha_config_set_yaml": (
        "Update raw YAML in configuration.yaml, packages/*.yaml or "
        "themes/*.yaml via add / replace / remove on a single top-level key "
        "(LAST RESORT). By default the first call returns a diff "
        "preview plus confirm_token; repeat with confirm_token to "
        "apply.\n\n"
        "Dedicated tools (ha_config_set_automation, "
        "ha_config_set_script, ha_config_set_scene, "
        "ha_config_set_helper) cover almost every use case and "
        "should be preferred. Use this only for YAML-only "
        "integrations (command_line, rest, shell_command, notify), "
        "YAML-heavy integrations like knx (in packages/*.yaml), "
        "registering YAML-mode dashboards via "
        "`lovelace.dashboards.<url_path>`, or editing theme files in "
        "themes/*.yaml (reloaded automatically). Most edits require a "
        "full HA restart; template, mqtt, and group support "
        "reload.\n\n"
        "For routing guidance and the full allowlist, see "
        "ha_get_skill_guide or your locally installed skills."
    ),
    "ha_search": (
        "Search Home Assistant for entities (by name, domain, or area) AND "
        "inside automation/script/scene/helper/dashboard configs, in one "
        "call. Returns tagged buckets — `entities` plus per-config-type "
        "lists — paginated per-surface and combined "
        "(`has_more`/`next_offset`).\n\n"
        "Config-body search is skipped when domain/area/state filters signal "
        "entity-only intent; a warning names the skip — pass `search_types` "
        "to force it.\n\n"
        "`partial: True` means results are NOT exhaustive: a surface failed "
        "or the config branch lost data. Empty buckets with `partial: True` "
        'mean "search failed", not "no results" — see `partial_reason` '
        "(also mirrored into `warnings`). Do not treat a partial response as "
        "complete.\n\n"
        "For what to do with what you find — native-first patterns, "
        "entity_id over device_id, impact analysis before renaming — see "
        "ha_get_skill_guide."
    ),
    # ha_manage_backup: the full description is 4571 dedented chars at
    # BACKUP_HINT=normal (4672 strong / 4598 weak) and the lite value is
    # a little over a quarter of that. No exact pair is quoted here on
    # purpose — three successive edits left a stale figure in this
    # comment, so the ratio is pinned by
    # test_backup_lite_description_stays_substantially_smaller instead,
    # which measures both sides at test time.
    #
    # It was the largest full description in the GATEWAY catalog this was
    # measured against, not in the repo: ha_get_system_health (5811) is
    # larger and unmapped.
    # The reduction is smaller than pure compression would give because
    # the safety content below is kept inline. The routing matrix STAYS —
    # the `action` parameter's own Field description says "Valid (scope,
    # action) combinations are listed in the tool description", so
    # trimming it away would leave that pointer aimed at nothing.
    #
    # The deferral target now exists: homeassistant-ai/skills#76 landed
    # references/backups.md, and the submodule pin in this commit
    # includes it, so test_every_lite_destination_resolves enforces it
    # rather than the entry sitting on "self-contained". What defers is
    # the recovery-layer judgment — which of HA's two paths fits which
    # failure, what an archive actually contains, encryption keys, and
    # what HA does and does not protect on delete. The eleven worked
    # examples stay dropped: they were call syntax, which the input
    # schema already carries.
    #
    # destructiveHint is set on this tool, so the lite text keeps every
    # irreversibility marker inline rather than deferring it: the
    # restart, the confirm, the human-only enable_snapshot_delete, and
    # each individual delete guard. It also keeps the two diagnostics
    # that have no fallback anywhere else — the enable_auto_backup
    # empty-list ambiguity, and the {backup_hint_text} timing sentence
    # (resolved per-instance; see _lite_docstring_tokens).
    "ha_manage_backup": (
        "Manage Home Assistant backups: full HA snapshots "
        "(`scope='snapshot'`) and per-entity auto-backups of agent edits "
        "(`scope='edits'`). Pick the scope first — the wrong one routes "
        "through the wrong code path.\n\n"
        "`scope='snapshot'` actions: `create` (slow on a large "
        "instance — progress heartbeats are sent while waiting, so a "
        "long wait is not a hang; retrying starts a SECOND backup), "
        "`list`, `restore` (**restarts HA**), `delete` (needs "
        "`confirm=True`; disabled until a human sets "
        "`enable_snapshot_delete` — an agent cannot enable it. Even then "
        "a delete is refused for scheduled/automatic backups, for "
        "anything younger than `snapshot_delete_min_age_days` (default "
        "7; 0 disables the floor), and for the single newest snapshot "
        "remaining).\n\n"
        "`scope='edits'` actions: `create` (needs `domain` + "
        "`entity_id`), `list`, `view`, `diff`, `restore` (takes a fresh "
        "safety snapshot first; no HA restart), `delete`. Automatic "
        "capture on write is gated by `enable_auto_backup`, so an empty "
        '`list` means "nothing saved" OR "the toggle is off" — check it '
        "before concluding there is nothing to restore. Either way "
        "`(edits, create)` still works: it bypasses the toggle because "
        "the request is explicit.\n\n"
        "Use `edits` to undo a recent agent edit to an "
        "automation/script/scene/dashboard/helper; use `snapshot` for "
        "system-wide recovery and before irreversible operations. "
        "{backup_hint_text}\n\n"
        "For which recovery path fits which failure, what an archive "
        "actually contains, and the encryption key a restore needs, see "
        "ha_get_skill_guide (`references/backups.md`)."
    ),
    # ha_report_issue: the lite value is about two thirds of the full
    # docstring. As for ha_manage_backup, no exact pair is quoted.
    #
    # Unlike every other entry here, the deferral target is the tool's
    # OWN RESPONSE, not the skill guide: `instructions` (see
    # tools_bug_report.py) independently re-derives the duplicate check,
    # the report type choice, the missing-tool pre-check, and the mandatory
    # anonymisation step. Issue reporting is ha-mcp product meta — it
    # cannot go in the skill pack, whose CONTRIBUTING forbids coupling
    # skill content to specific MCP tool names. The lite text says so
    # outright so a compliant agent doesn't spend a call finding out.
    "ha_report_issue": (
        "Get diagnostic information plus a finished GitHub issue. Covers "
        "two kinds of report: a runtime bug (ha-mcp errored or behaved "
        "unexpectedly) and agent-behaviour feedback (the AI used the wrong "
        "tool or worked inefficiently). Pass the report text in the call; "
        "the server builds the issue title, body and a pre-filled link.\n\n"
        "The response carries the full workflow in its `instructions` "
        "field (duplicate check, the mandatory anonymisation step, and "
        "how to file the issue), plus "
        '`missing_tool_hint` for the "a tool I expected is missing" '
        "case, which is usually a stale client tool list rather than a "
        "bug. Read `instructions` before presenting anything to the "
        "user; ha_get_skill_guide does not cover issue reporting."
    ),
}

# Where each _LITE_DOCSTRINGS entry's "see ..." pointer actually lands.
#
# The pointer is the whole bargain of lite mode: the trimmed text is only
# acceptable because the detail is reachable. Enforcing that every entry
# merely CONTAINS the string "ha_get_skill_guide" (the original
# invariant) checks the pointer and never the destination — which is how
# an entry deferring to guide content that does not exist could pass
# tests (#2153 review). This map names the destination so
# tests/src/unit/test_lite_docstrings.py can resolve it against the
# vendored skill pack.
#
# Three legal value forms, each with a matching check in the tests:
#
#   "references/<file>.md" / "SKILL.md"
#       A path inside the home-assistant-best-practices skill. Must
#       resolve against the VENDORED pack, and the lite text must carry
#       a ha_get_skill_guide pointer to reach it.
#   "tool-response:<field>"
#       The guidance ships in the tool's own response instead. The field
#       must actually be returned, and the lite text must name it.
#   "self-contained"
#       The entry defers nothing. The lite text must carry NO
#       ha_get_skill_guide pointer and name no reference file, so an
#       entry cannot quietly re-acquire a pointer to content that isn't
#       vendored — which is what "self-contained" exists to prevent.
LITE_DOCSTRING_DESTINATIONS: dict[str, str] = {
    "ha_config_get_automation": "references/triggers-and-conditions.md",
    "ha_config_set_automation": "references/triggers-and-conditions.md",
    "ha_config_get_script": "references/automation-actions.md",
    "ha_config_set_script": "references/automation-actions.md",
    "ha_config_get_scene": "references/scenes.md",
    "ha_config_set_scene": "references/scenes.md",
    "ha_config_list_helpers": "references/helper-selection.md",
    "ha_config_set_helper": "references/helper-selection.md",
    "ha_config_get_dashboard": "references/dashboard-cards.md",
    "ha_config_set_dashboard": "references/dashboard-guide.md",
    "ha_call_service": "references/domain-docs.md",
    "ha_config_set_yaml": "references/yaml-only-integrations.md",
    # The skill pack has no search reference, so this lands on the
    # decision workflow. The lite text was reworded to promise what
    # SKILL.md actually holds (what to do with the results) instead of
    # "parameters, schema, and examples", which it never had — the
    # parameters ship in the input schema regardless.
    "ha_search": "SKILL.md",
    "ha_manage_backup": "references/backups.md",
    "ha_report_issue": "tool-response:instructions",
}

# Shared action-phrased keyword block for retrieval. Some MCP clients
# (Claude Code, others) rank candidate tools by token-overlap between
# the user's natural-language query and each tool's `description`
# field; symptom-framed SKILL.md descriptions don't overlap with
# task-phrased queries like "create automation" or "writing trigger".
# This block lists the workflow positions where consulting the
# bundled skill matters, so retrieval surfaces ha_get_skill_guide
# when an agent is about to write config.
SKILL_USE_BEFORE_KEYWORDS: str = (
    "Use BEFORE: creating or editing automations, scripts, scenes, "
    "helpers, or dashboards; writing triggers, conditions, actions, "
    "wait_template, or service calls; renaming entities or migrating "
    "device_id to entity_id; calling ha_config_set_automation, "
    "ha_config_set_script, ha_config_set_helper, ha_config_set_dashboard, "
    "or ha_set_entity."
)
