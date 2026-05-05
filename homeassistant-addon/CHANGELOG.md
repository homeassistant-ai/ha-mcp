# CHANGELOG

<!-- version list -->


## v7.4.1 (2026-05-02)

### Fixed

- **addon**: Propagate `BUILD_VERSION` to runtime so the production add-on
  reports the correct version in startup logs instead of the stale value
  from `pyproject.toml` at build time
  ([#1090](https://github.com/homeassistant-ai/ha-mcp/pull/1090)).
- **ci**: Unbreak `hotfix-release.yml` semantic-release run; the previous
  detached-HEAD checkout silently no-op'd every hotfix merge
  ([#1091](https://github.com/homeassistant-ai/ha-mcp/pull/1091)).


## v7.4.0 (2026-04-29)

### Added

- Introduce ha_delete_helpers_integrations to consolidate helper/config-entry deletion (#1007)
  ([#1056](https://github.com/homeassistant-ai/ha-mcp/pull/1056))
- UAT runner ergonomics + demote fastmcp tool-failure tracebacks
  ([#1051](https://github.com/homeassistant-ai/ha-mcp/pull/1051))
- Add ha_manage_energy_prefs tool for Energy Dashboard CRUD
  ([#1048](https://github.com/homeassistant-ai/ha-mcp/pull/1048))
- Combine ha_config_list_floors and ha_config_list_areas into ha_list_floors_areas
  ([#1016](https://github.com/homeassistant-ai/ha-mcp/pull/1016))
- Unify ha_config_set_helper to cover all 27 helper types
  ([#1012](https://github.com/homeassistant-ai/ha-mcp/pull/1012))
- Rename ha_call_addon_api to ha_manage_addon, add Supervisor config mode
  ([#978](https://github.com/homeassistant-ai/ha-mcp/pull/978))

### Changed

- Update contributors list [contributors-updated]
  ([`a0805c7`](https://github.com/homeassistant-ai/ha-mcp/commit/a0805c7b64821c7ebac40844602e526a8adf5286))
- **groups**: Point agents from service-based groups to flow-based helper (#1015)
  ([#1044](https://github.com/homeassistant-ai/ha-mcp/pull/1044))
- Recommend RFC 2119 terms for tool docstring constraints
  ([#947](https://github.com/homeassistant-ai/ha-mcp/pull/947))
- Fix broken resolve-thread example in AGENTS.md
  ([#949](https://github.com/homeassistant-ai/ha-mcp/pull/949))

### Fixed

- **site**: Allow text selection in Tool Explorer tool names
  ([#1060](https://github.com/homeassistant-ai/ha-mcp/pull/1060))
- Make version visible in logs and stop odd-week :stable republishes
  ([#1042](https://github.com/homeassistant-ai/ha-mcp/pull/1042))
- Validate entity & service references in automation/script configs
  ([#959](https://github.com/homeassistant-ai/ha-mcp/pull/959))
- Apply SearchKeywordsTransform unconditionally for claude.ai BM25 retrieval
  ([#955](https://github.com/homeassistant-ai/ha-mcp/pull/955))
- Persist auto-generated secret_path to addon options (#941)
  ([#952](https://github.com/homeassistant-ai/ha-mcp/pull/952))
- Classify Supervisor schema errors as VALIDATION_FAILED
  ([#1008](https://github.com/homeassistant-ai/ha-mcp/pull/1008))
- Default enable_skills_as_tools to true in the add-on (follow-up to #806)
  ([#948](https://github.com/homeassistant-ai/ha-mcp/pull/948))
- Route supervisor add-on log fetches through HA Core REST proxy
  ([#951](https://github.com/homeassistant-ai/ha-mcp/pull/951))
- Normalize statistic_types in query_params (#990)
  ([#999](https://github.com/homeassistant-ai/ha-mcp/pull/999))

### Refactoring

- **search**: Make ha_search_entities query optional, clarify docs
  ([#1004](https://github.com/homeassistant-ai/ha-mcp/pull/1004))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Improve error guidance for small-model failure modes
  ([#1055](https://github.com/homeassistant-ai/ha-mcp/pull/1055))

### Build System

- **deps**: Bump postcss from 8.5.6 to 8.5.10 in /site
  ([#1052](https://github.com/homeassistant-ai/ha-mcp/pull/1052))
- **deps**: Bump astro from 5.18.1 to 6.1.6 in /site
  ([#1038](https://github.com/homeassistant-ai/ha-mcp/pull/1038))

### Chores

- **addon**: Publish dev addon version 7.3.0.dev239 [skip ci]
  ([`f7ab4ef`](https://github.com/homeassistant-ai/ha-mcp/commit/f7ab4ef4b6060a609059e4ad7b4c75b870dfc090))
- **addon**: Publish dev addon version 7.3.0.dev238 [skip ci]
  ([`a4d54c6`](https://github.com/homeassistant-ai/ha-mcp/commit/a4d54c67dd90fa26f46681151e3a757c6121d37e))
- **addon**: Publish dev addon version 7.3.0.dev237 [skip ci]
  ([`0950652`](https://github.com/homeassistant-ai/ha-mcp/commit/0950652a2b51689f5667e42dd86b4ef8c16d1e14))
- Sync tool docs after merge [skip ci]
  ([`791279c`](https://github.com/homeassistant-ai/ha-mcp/commit/791279c90cd9a5945679f6cc0ae33346b714dbf4))
- **addon**: Publish dev addon version 7.3.0.dev236 [skip ci]
  ([`cd387a9`](https://github.com/homeassistant-ai/ha-mcp/commit/cd387a993ae3bd064658316c41c0f696463c5646))
- **addon**: Publish dev addon version 7.3.0.dev235 [skip ci]
  ([`c824b1a`](https://github.com/homeassistant-ai/ha-mcp/commit/c824b1ad976b606eb468bf7f37c64817f01674f8))
- **addon**: Publish dev addon version 7.3.0.dev234 [skip ci]
  ([`c1c007d`](https://github.com/homeassistant-ai/ha-mcp/commit/c1c007dc629a6f037a17db147fc1472203f55b5b))
- **addon**: Publish dev addon version 7.3.0.dev233 [skip ci]
  ([`a09b2b4`](https://github.com/homeassistant-ai/ha-mcp/commit/a09b2b4c41713a8371907c531879024e8a9da589))
- **addon**: Publish dev addon version 7.3.0.dev232 [skip ci]
  ([`30fd6de`](https://github.com/homeassistant-ai/ha-mcp/commit/30fd6deb4af7e2c6c75af396d3fa6c0509554b75))
- Sync tool docs after merge [skip ci]
  ([`dbcde02`](https://github.com/homeassistant-ai/ha-mcp/commit/dbcde023cf8f3c9cab712d1ec5bd61a2307270ca))
- **addon**: Publish dev addon version 7.3.0.dev231 [skip ci]
  ([`76692c5`](https://github.com/homeassistant-ai/ha-mcp/commit/76692c5e122b8a47ec4ecbe25742a3feade75ee9))
- Sync tool docs after merge [skip ci]
  ([`994cfd6`](https://github.com/homeassistant-ai/ha-mcp/commit/994cfd6ca9a9ed37ae63ec5eac59ab0ccb3f905b))
- **addon**: Publish dev addon version 7.3.0.dev230 [skip ci]
  ([`523ac00`](https://github.com/homeassistant-ai/ha-mcp/commit/523ac00160232f429b287c6620dc5a919cdc6266))
- Sync tool docs after merge [skip ci]
  ([`e96ddd4`](https://github.com/homeassistant-ai/ha-mcp/commit/e96ddd4fb516c42e9e17f15477c67a543fb0c18e))
- **addon**: Publish dev addon version 7.3.0.dev229 [skip ci]
  ([`bc5b47a`](https://github.com/homeassistant-ai/ha-mcp/commit/bc5b47ac2124c37a91cc282b956794760e1eea4b))
- Sync tool docs after merge [skip ci]
  ([`73712b7`](https://github.com/homeassistant-ai/ha-mcp/commit/73712b7644d8ee97ec6ea7cb4a6a02c05e0b75b5))
- **addon**: Publish dev addon version 7.3.0.dev228 [skip ci]
  ([`a3a1c90`](https://github.com/homeassistant-ai/ha-mcp/commit/a3a1c90e9da441add262ba174f00af7bc8c5d7b8))
- **addon**: Publish dev addon version 7.3.0.dev227 [skip ci]
  ([`be4995a`](https://github.com/homeassistant-ai/ha-mcp/commit/be4995af06b85b03904af91b918958b03e1363d7))
- **addon**: Publish dev addon version 7.3.0.dev226 [skip ci]
  ([`b7a9230`](https://github.com/homeassistant-ai/ha-mcp/commit/b7a9230b0b72f456e7e5520e8084f553f451357d))
- Sync tool docs after merge [skip ci]
  ([`b3dec59`](https://github.com/homeassistant-ai/ha-mcp/commit/b3dec59a3cda79ef127f0335f615683d4a67d148))
- **addon**: Publish dev addon version 7.3.0.dev225 [skip ci]
  ([`9063f13`](https://github.com/homeassistant-ai/ha-mcp/commit/9063f135271767d701c3ac4cd859ae6bad9bd99d))
- **addon**: Publish dev addon version 7.3.0.dev224 [skip ci]
  ([`4da6713`](https://github.com/homeassistant-ai/ha-mcp/commit/4da6713ea73c338f754b8d5d83fb98ced9bc6843))
- **addon**: Publish dev addon version 7.3.0.dev223 [skip ci]
  ([`b2b7d2a`](https://github.com/homeassistant-ai/ha-mcp/commit/b2b7d2ab05d83fd127b2d935651bf58600b932ea))
- **addon**: Publish dev addon version 7.3.0.dev222 [skip ci]
  ([`23eee88`](https://github.com/homeassistant-ai/ha-mcp/commit/23eee889b28a2993eb45622d9939e1669b29e7e6))
- **addon**: Publish dev addon version 7.3.0.dev221 [skip ci]
  ([`611f52a`](https://github.com/homeassistant-ai/ha-mcp/commit/611f52ae112ab3eeb6a0adf10aa9492ccc69e53f))
- Sync tool docs after merge [skip ci]
  ([`13143a2`](https://github.com/homeassistant-ai/ha-mcp/commit/13143a2465f736ba2ec2c4c47a5dca3007dd2beb))
- **addon**: Publish version 7.3.0 [skip ci]
  ([`5e53405`](https://github.com/homeassistant-ai/ha-mcp/commit/5e534053903d68a0a512127ee7f587439908a136))

### Continuous Integration

- **deps**: Bump renovatebot/github-action in the github-actions group
  ([#1064](https://github.com/homeassistant-ai/ha-mcp/pull/1064))
- **deps**: Bump the github-actions group with 2 updates
  ([#1021](https://github.com/homeassistant-ai/ha-mcp/pull/1021))

### Performance Improvements

- Inline-mode openai agent for BAT runner
  ([#1017](https://github.com/homeassistant-ai/ha-mcp/pull/1017))

### Refactoring

- Reduce C901 complexity in 5 non-tool files
  ([#1000](https://github.com/homeassistant-ai/ha-mcp/pull/1000))

### Testing

- **e2e**: Add A2 negative-input tests for optional-id list-or-detail tools
  ([#1058](https://github.com/homeassistant-ai/ha-mcp/pull/1058))
- **uat**: Give find-automations story a unique id
  ([#1054](https://github.com/homeassistant-ai/ha-mcp/pull/1054))
- **e2e**: Close A7 negative-input gaps for remove_area/remove_floor + harden remove_category
  ([#1047](https://github.com/homeassistant-ai/ha-mcp/pull/1047))
- **e2e**: Enable HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION for HACS tests
  ([#1005](https://github.com/homeassistant-ai/ha-mcp/pull/1005))
- **e2e**: Add negative-input tests for A7 destructive tools
  ([#987](https://github.com/homeassistant-ai/ha-mcp/pull/987))
- **e2e**: Add A5 negative-input tests for ha_config_set_automation and ha_config_set_helper
  ([#982](https://github.com/homeassistant-ai/ha-mcp/pull/982))
- **e2e**: Add A6 config_hash optimistic-locking tests for ha_config_set_automation
  ([#983](https://github.com/homeassistant-ai/ha-mcp/pull/983))
</details>


## v7.3.0 (2026-04-15)

### Added

- Harden ha_config_set_yaml description and require justification
  ([#942](https://github.com/homeassistant-ai/ha-mcp/pull/942))
- Add python_transform support to automations and scripts
  ([#968](https://github.com/homeassistant-ai/ha-mcp/pull/968))
- **history**: Add offset pagination for history and statistics sources
  ([#964](https://github.com/homeassistant-ai/ha-mcp/pull/964))
- **site**: Redesign documentation site with professional visual identity
  ([#938](https://github.com/homeassistant-ai/ha-mcp/pull/938))
- Replace SequenceMatcher fuzzy search with BM25 scoring
  ([#932](https://github.com/homeassistant-ai/ha-mcp/pull/932))
- Consolidate ha_get_statistics into ha_get_history via source parameter
  ([#911](https://github.com/homeassistant-ai/ha-mcp/pull/911))
- **site**: Add Copilot CLI support to installation wizard
  ([#909](https://github.com/homeassistant-ai/ha-mcp/pull/909))
- Add ha_remove_entity tool (closes #874)
  ([#876](https://github.com/homeassistant-ai/ha-mcp/pull/876))
- Add pagination and detail_level to ha_list_services, ha_get_device, ha_get_integration
  ([#870](https://github.com/homeassistant-ai/ha-mcp/pull/870))
- Preserve YAML comments and HA tags in ha_config_set_yaml
  ([#869](https://github.com/homeassistant-ai/ha-mcp/pull/869))
- Expose category on automation, script, and helper config tools
  ([#850](https://github.com/homeassistant-ai/ha-mcp/pull/850))
- Add system/error logs, repairs, and ZHA radio metrics to existing tools (replaces #675)
  ([#836](https://github.com/homeassistant-ai/ha-mcp/pull/836))
- Reduce ha_get_overview context window usage
  ([#728](https://github.com/homeassistant-ai/ha-mcp/pull/728))

### Changed

- Tone down beta caveats, remove stale banner, update contributors [contributors-updated]
  ([#989](https://github.com/homeassistant-ai/ha-mcp/pull/989))
- Add Tests only type to PR template
  ([#953](https://github.com/homeassistant-ai/ha-mcp/pull/953))
- Document webhook proxy addon in README, setup site, and FAQ
  ([#931](https://github.com/homeassistant-ai/ha-mcp/pull/931))
- Add Windows pywin32 FAQ entry
  ([#933](https://github.com/homeassistant-ai/ha-mcp/pull/933))
- Clarify tool consolidation guidelines with anti-patterns
  ([#927](https://github.com/homeassistant-ai/ha-mcp/pull/927))
- **security**: Add scope, out-of-scope, and OAuth beta warning
  ([#917](https://github.com/homeassistant-ai/ha-mcp/pull/917))
- Trim AGENTS.md to stay under 40k char Claude Code limit
  ([#922](https://github.com/homeassistant-ai/ha-mcp/pull/922))
- Clarify ha_config_set_yaml comment preservation scope
  ([#920](https://github.com/homeassistant-ai/ha-mcp/pull/920))
- Add MCP tool docstring guidelines to AGENTS.md and styleguide
  ([#907](https://github.com/homeassistant-ai/ha-mcp/pull/907))
- Update contributors list [contributors-updated]
  ([`934f573`](https://github.com/homeassistant-ai/ha-mcp/commit/934f5738bd89c1743df8fb9963d1caf5b304c363))
- Add macOS troubleshooting for local network connection issues
  ([#897](https://github.com/homeassistant-ai/ha-mcp/pull/897))
- Document sync-tool-docs.yml workflow in AGENTS.md
  ([#898](https://github.com/homeassistant-ai/ha-mcp/pull/898))
- Add custom component documentation and HACS install badge
  ([#877](https://github.com/homeassistant-ai/ha-mcp/pull/877))

### Fixed

- Raise ToolError for statistic_types=[] in _fetch_statistics
  ([#979](https://github.com/homeassistant-ai/ha-mcp/pull/979))
- **history**: Add query_params echo to _fetch_statistics response
  ([#976](https://github.com/homeassistant-ai/ha-mcp/pull/976))
- **history**: Add "year" to valid statistics periods
  ([#975](https://github.com/homeassistant-ai/ha-mcp/pull/975))
- **search**: Validate limit and offset parameters in ha_deep_search
  ([#954](https://github.com/homeassistant-ai/ha-mcp/pull/954))
- **search**: Validate limit parameter with min_value=1 in ha_search_entities
  ([#946](https://github.com/homeassistant-ai/ha-mcp/pull/946))
- Persist input helper config changes via storage API
  ([#884](https://github.com/homeassistant-ai/ha-mcp/pull/884))
- **addon**: Use unique version for dev add-on so HA detects updates
  ([#918](https://github.com/homeassistant-ai/ha-mcp/pull/918))
- Enforce Python 3.13 in install scripts and at runtime
  ([#904](https://github.com/homeassistant-ai/ha-mcp/pull/904))
- **site**: Replace placeholder logo SVGs with real brand icons
  ([#910](https://github.com/homeassistant-ai/ha-mcp/pull/910))
- Fully stateless OAuth tokens, drop HOMEASSISTANT_TOKEN requirement
  ([#893](https://github.com/homeassistant-ai/ha-mcp/pull/893))
- Parallelize deep_search Tier 3 config fetches (closes #879)
  ([#882](https://github.com/homeassistant-ai/ha-mcp/pull/882))
- Add ast-grep rule and fix hand-built error dicts
  ([#895](https://github.com/homeassistant-ai/ha-mcp/pull/895))
- Fetch addon stats from /addons/{slug}/stats endpoint
  ([#865](https://github.com/homeassistant-ai/ha-mcp/pull/865))
- **docs**: Sync homeassistant-addon/DOCS.md via extract_tools.py
  ([#883](https://github.com/homeassistant-ai/ha-mcp/pull/883))
- Add missing get_entity_state mock to group unit tests
  ([#878](https://github.com/homeassistant-ai/ha-mcp/pull/878))
- Enable e2e filesystem tests and fix ha_mcp_tools integration
  ([#868](https://github.com/homeassistant-ai/ha-mcp/pull/868))
- Add post-operation verification to group config tools
  ([#853](https://github.com/homeassistant-ai/ha-mcp/pull/853))
- Init submodules and use portable path in /wt skill
  ([#859](https://github.com/homeassistant-ai/ha-mcp/pull/859))
- Block registry-disable on automation/script entities (#794)
  ([#796](https://github.com/homeassistant-ai/ha-mcp/pull/796))

### Performance Improvements

- Optimize e2e test execution time
  ([#872](https://github.com/homeassistant-ai/ha-mcp/pull/872))

### Refactoring

- Migrate 7 tool files to class-based pattern (batch 3)
  ([#944](https://github.com/homeassistant-ai/ha-mcp/pull/944))
- Migrate 12 tool files to class-based pattern (batch 2)
  ([#937](https://github.com/homeassistant-ai/ha-mcp/pull/937))
- Migrate 5 tool files to class-based pattern (batch 1)
  ([#935](https://github.com/homeassistant-ai/ha-mcp/pull/935))
- Enable C901 complexity checking and fix violations
  ([#923](https://github.com/homeassistant-ai/ha-mcp/pull/923))
- Merge ha_dashboard_find_card into ha_config_get_dashboard
  ([#905](https://github.com/homeassistant-ai/ha-mcp/pull/905))
- Consolidate 3 overlapping tool pairs
  ([#873](https://github.com/homeassistant-ai/ha-mcp/pull/873))
- Consolidate HACS read tools from 4 to 2
  ([#871](https://github.com/homeassistant-ai/ha-mcp/pull/871))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Replace BAT blind sleep with deterministic HA readiness checks
  ([#939](https://github.com/homeassistant-ai/ha-mcp/pull/939))

### Chores

- **addon**: Publish dev addon version 7.2.0.dev219 [skip ci]
  ([`6aa9566`](https://github.com/homeassistant-ai/ha-mcp/commit/6aa9566823ad896d182631abed211dcc703e2316))
- **addon**: Publish dev addon version 7.2.0.dev218 [skip ci]
  ([`2086ac2`](https://github.com/homeassistant-ai/ha-mcp/commit/2086ac2d7d61a1adcf3c413c780f2a150d3cb3f0))
- **addon**: Publish dev addon version 7.2.0.dev217 [skip ci]
  ([`235a0a4`](https://github.com/homeassistant-ai/ha-mcp/commit/235a0a415b1432b0ae05f4cf3e952a13ccd2788e))
- **addon**: Publish dev addon version 7.2.0.dev216 [skip ci]
  ([`cb5e4b4`](https://github.com/homeassistant-ai/ha-mcp/commit/cb5e4b41628c74425b47cb4f0d1b48cd0edf3fb6))
- **addon**: Publish dev addon version 7.2.0.dev215 [skip ci]
  ([`af4c14b`](https://github.com/homeassistant-ai/ha-mcp/commit/af4c14b4a1e4741b2f5008a62412564d5c8eadfc))
- Sync tool docs after merge [skip ci]
  ([`39fd83e`](https://github.com/homeassistant-ai/ha-mcp/commit/39fd83e8b25185c075238f5f8c312e4f40c26212))
- **addon**: Publish dev addon version 7.2.0.dev214 [skip ci]
  ([`12cbb2b`](https://github.com/homeassistant-ai/ha-mcp/commit/12cbb2b692d05ab68afc6a3c03cad19a400447b2))
- **addon**: Publish dev addon version 7.2.0.dev213 [skip ci]
  ([`f200742`](https://github.com/homeassistant-ai/ha-mcp/commit/f2007420e7e7ada2bb8425192f2a10098277590c))
- **addon**: Publish dev addon version 7.2.0.dev212 [skip ci]
  ([`9377017`](https://github.com/homeassistant-ai/ha-mcp/commit/937701712001a76fe75c9a676f4b4d8b4d0bf791))
- Sync tool docs after merge [skip ci]
  ([`cdd59ca`](https://github.com/homeassistant-ai/ha-mcp/commit/cdd59cae96828aee36404e51208f8eb35f5f648b))
- **addon**: Publish dev addon version 7.2.0.dev211 [skip ci]
  ([`d60f4da`](https://github.com/homeassistant-ai/ha-mcp/commit/d60f4da78ae3c5cff6b375bb3bf330165131f3af))
- **addon**: Publish dev addon version 7.2.0.dev210 [skip ci]
  ([`9552141`](https://github.com/homeassistant-ai/ha-mcp/commit/95521418819f89060ccfa6a53830d149d1a1aa96))
- Sync tool docs after merge [skip ci]
  ([`3378442`](https://github.com/homeassistant-ai/ha-mcp/commit/337844239428ab56d0e823e32f2b86425d39a022))
- **addon**: Publish dev addon version 7.2.0.dev209 [skip ci]
  ([`9ef7db9`](https://github.com/homeassistant-ai/ha-mcp/commit/9ef7db91e06c4e004284496ad94f8729d30839f3))
- **addon**: Publish dev addon version 7.2.0.dev208 [skip ci]
  ([`2c620eb`](https://github.com/homeassistant-ai/ha-mcp/commit/2c620eb24035dbf38324ca4de4e98b2a112e5408))
- **addon**: Publish dev addon version 7.2.0.dev207 [skip ci]
  ([`b6198d5`](https://github.com/homeassistant-ai/ha-mcp/commit/b6198d5b0826caf57a5a6445f8ed400d73f260a0))
- **addon**: Publish dev addon version 7.2.0.dev206 [skip ci]
  ([`4a5be2a`](https://github.com/homeassistant-ai/ha-mcp/commit/4a5be2ad26d1cf1f4fdf41efcb075ff9d3b830cc))
- Sync tool docs after merge [skip ci]
  ([`9930a8f`](https://github.com/homeassistant-ai/ha-mcp/commit/9930a8fb1c370729708da843e600b2e5b52778c1))
- **addon**: Publish dev addon version 7.2.0.dev205 [skip ci]
  ([`c5e0570`](https://github.com/homeassistant-ai/ha-mcp/commit/c5e0570aaf790477e6504e83d891516e49a99fd1))
- **addon**: Publish dev addon version 7.2.0.dev204 [skip ci]
  ([`ca2fda2`](https://github.com/homeassistant-ai/ha-mcp/commit/ca2fda21c8a6692cb50ba5342cb07268d3b62d63))
- Sync tool docs after merge [skip ci]
  ([`9d27c81`](https://github.com/homeassistant-ai/ha-mcp/commit/9d27c8102c8101f9b25a031b2248f513d852a4f4))
- Sync tool docs after merge [skip ci]
  ([`314fbea`](https://github.com/homeassistant-ai/ha-mcp/commit/314fbea7656cd390ae67d7c17f388d73d84ffd25))
- Sync tool docs after merge [skip ci]
  ([`09f4b69`](https://github.com/homeassistant-ai/ha-mcp/commit/09f4b697bb585ef12184ab0914b3f070c1c0686b))
- Bump HA test image to 2026.4.1 and improve test stabilization
  ([#908](https://github.com/homeassistant-ai/ha-mcp/pull/908))
- **deps**: Bump vite from 6.4.1 to 6.4.2 in /site
  ([#906](https://github.com/homeassistant-ai/ha-mcp/pull/906))
- Sync tool docs after merge [skip ci]
  ([`370f462`](https://github.com/homeassistant-ai/ha-mcp/commit/370f4624d6f4218af408579c60e4e42b0b180e55))
- Sync tool docs after merge [skip ci]
  ([`57497c0`](https://github.com/homeassistant-ai/ha-mcp/commit/57497c01af9e740a70912f90fe57dc6ca6459908))
- Sync tool docs after merge [skip ci]
  ([`1f783dd`](https://github.com/homeassistant-ai/ha-mcp/commit/1f783dd83a0363479638a4098117892927754eb4))
- Sync tool docs after merge [skip ci]
  ([`2c79011`](https://github.com/homeassistant-ai/ha-mcp/commit/2c7901123ed024d249b54c8749bb6f59b99f7ccd))
- Sync tool docs after merge [skip ci]
  ([`596a673`](https://github.com/homeassistant-ai/ha-mcp/commit/596a6736d73fe99fb6bfeed6e1800d21f8a840e5))
- **deps**: Bump defu from 6.1.4 to 6.1.6 in /site
  ([#860](https://github.com/homeassistant-ai/ha-mcp/pull/860))
- Sync tool docs after merge [skip ci]
  ([`1b6138d`](https://github.com/homeassistant-ai/ha-mcp/commit/1b6138dbd1e648640bdb5f3bfc0d598426547fa6))
- Sync tool docs after merge [skip ci]
  ([`c8afd28`](https://github.com/homeassistant-ai/ha-mcp/commit/c8afd28bafaee7a04a40b18b6df82a1d2521473e))
- **addon**: Publish version 7.2.0 [skip ci]
  ([`4b0be35`](https://github.com/homeassistant-ai/ha-mcp/commit/4b0be35e1dd1e74a8e6acb4e0ba0aba210a6a5b5))

### Continuous Integration

- **deps**: Bump the github-actions group with 3 updates
  ([#969](https://github.com/homeassistant-ai/ha-mcp/pull/969))
- **deps**: Bump the github-actions group with 2 updates
  ([#887](https://github.com/homeassistant-ai/ha-mcp/pull/887))
- Auto-sync tools.json on merge instead of failing PRs
  ([#849](https://github.com/homeassistant-ai/ha-mcp/pull/849))

### Refactoring

- Eliminate redundant file reads in check_sync
  ([#888](https://github.com/homeassistant-ai/ha-mcp/pull/888))

### Testing

- **registry**: Improve assertion messages for domain mismatch and invalid format
  ([#974](https://github.com/homeassistant-ai/ha-mcp/pull/974))
- **entity**: Add negative-input tests for ha_set_entity
  ([#961](https://github.com/homeassistant-ai/ha-mcp/pull/961))
- **e2e**: Add negative-input tests for ha_get_history and ha_get_automation_traces
  ([#945](https://github.com/homeassistant-ai/ha-mcp/pull/945))
- **e2e**: Add negative-input test for ha_get_zone with nonexistent zone_id
  ([#957](https://github.com/homeassistant-ai/ha-mcp/pull/957))
- **e2e**: Add negative-input test for ha_config_get_label with nonexistent label_id
  ([#958](https://github.com/homeassistant-ai/ha-mcp/pull/958))
</details>


## v7.2.0 (2026-04-01)

### Added

- Add managed YAML config editing tool (ha_config_set_yaml)
  ([#827](https://github.com/homeassistant-ai/ha-mcp/pull/827))
- Tool explorer with taxonomy, auto-generated docs, and design mode
  ([#839](https://github.com/homeassistant-ai/ha-mcp/pull/839))
- Add generic add-on API proxy tool (ha_call_addon_api)
  ([#641](https://github.com/homeassistant-ai/ha-mcp/pull/641))
- Add support for automation/script/scene categories
  ([#677](https://github.com/homeassistant-ai/ha-mcp/pull/677))
- Convert doc tools to MCP resources and skill references
  ([#806](https://github.com/homeassistant-ai/ha-mcp/pull/806))
- Add Python 3.14 support
  ([#700](https://github.com/homeassistant-ai/ha-mcp/pull/700))
- Search-based tool discovery with categorized call proxies
  ([#727](https://github.com/homeassistant-ai/ha-mcp/pull/727))
- **uat**: Add --mcp-env flag and tokens_first_input metric
  ([#791](https://github.com/homeassistant-ai/ha-mcp/pull/791))

### Changed

- Credit @teh-hippo, @smenzer, @The-Greg-O; update @cj-elevate
  ([`66b3bb8`](https://github.com/homeassistant-ai/ha-mcp/commit/66b3bb803fe0fcb4ac7172cce0dcf9f8cfb8979d))
- Document OAuth v7.0.0 breaking change (HOMEASSISTANT_URL required)
  ([#829](https://github.com/homeassistant-ai/ha-mcp/pull/829))
- Replace hardcoded path with <repo-root> placeholder
  ([#797](https://github.com/homeassistant-ai/ha-mcp/pull/797))

### Fixed

- Reduce context exhaustion and improve trace detail for debugging
  ([#822](https://github.com/homeassistant-ai/ha-mcp/pull/822))
- Add ast-grep rules to catch silent error handling bugs
  ([#838](https://github.com/homeassistant-ai/ha-mcp/pull/838))
- Add exact_match to all search tools, badge search, and dashboard deep search
  ([#814](https://github.com/homeassistant-ai/ha-mcp/pull/814))
- Surface connection errors in ha_get_overview instead of returning empty data
  ([#812](https://github.com/homeassistant-ai/ha-mcp/pull/812))
- OAuth token refresh broken and state lost on container restart
  ([#790](https://github.com/homeassistant-ai/ha-mcp/pull/790))
- **addon**: Reject corrupt or URL-valued secret paths
  ([#792](https://github.com/homeassistant-ai/ha-mcp/pull/792))

### Refactoring

- Consolidate 5 redundant tools (merge after #806)
  ([#813](https://github.com/homeassistant-ai/ha-mcp/pull/813))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Prevent issue triage timeout on complex issues
  ([#832](https://github.com/homeassistant-ai/ha-mcp/pull/832))

### Chores

- Credit @transportrefer for integration options schema support
  ([#689](https://github.com/homeassistant-ai/ha-mcp/pull/689))
- Credit @adraguidev for menu-based config entry flow fix
  ([#647](https://github.com/homeassistant-ai/ha-mcp/pull/647))
- Credit @saphid for config entry options flow design
  ([#590](https://github.com/homeassistant-ai/ha-mcp/pull/590))
- **deps**: Bump astro from 5.16.11 to 5.18.1 in /site
  ([#826](https://github.com/homeassistant-ai/ha-mcp/pull/826))
- **deps**: Bump picomatch in /site
  ([#821](https://github.com/homeassistant-ai/ha-mcp/pull/821))
- **deps**: Bump yaml from 2.8.2 to 2.8.3 in /site
  ([#820](https://github.com/homeassistant-ai/ha-mcp/pull/820))
- **deps**: Bump smol-toml from 1.6.0 to 1.6.1 in /site
  ([#818](https://github.com/homeassistant-ai/ha-mcp/pull/818))
- **ci**: Bump uv in PR workflow from 0.9.30 to 0.11.0 and add Renovate annotations
  ([#817](https://github.com/homeassistant-ai/ha-mcp/pull/817))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.0
  ([#816](https://github.com/homeassistant-ai/ha-mcp/pull/816))
- Migrate from pre-commit to lefthook for parallel hook execution
  ([#802](https://github.com/homeassistant-ai/ha-mcp/pull/802))
- Remove hardcoded assignee from issue templates
  ([#800](https://github.com/homeassistant-ai/ha-mcp/pull/800))
- Extend type checking and tests to all Python dirs
  ([#793](https://github.com/homeassistant-ai/ha-mcp/pull/793))
- **deps**: Bump h3 from 1.15.8 to 1.15.9 in /site
  ([#795](https://github.com/homeassistant-ai/ha-mcp/pull/795))
- **deps**: Bump h3 from 1.15.5 to 1.15.8 in /site
  ([#786](https://github.com/homeassistant-ai/ha-mcp/pull/786))
- **addon**: Publish version 7.1.0 [skip ci]
  ([`a8ffaf6`](https://github.com/homeassistant-ai/ha-mcp/commit/a8ffaf65c49305f8a6753cea68743752998c352b))

### Continuous Integration

- **deps**: Bump the github-actions group with 3 updates
  ([#842](https://github.com/homeassistant-ai/ha-mcp/pull/842))
- **deps**: Bump renovatebot/github-action in the github-actions group
  ([#807](https://github.com/homeassistant-ai/ha-mcp/pull/807))
</details>


## v7.1.0 (2026-03-18)

### Added

- Reactive best-practice warnings on write tool calls
  ([#695](https://github.com/homeassistant-ai/ha-mcp/pull/695))
- Add menu_option to ha_get_helper_schema for template helper schema introspection
  ([#759](https://github.com/homeassistant-ai/ha-mcp/pull/759))
- Consolidate zone CRUD tools into set/remove pattern
  ([#643](https://github.com/homeassistant-ai/ha-mcp/pull/643))
- Config entry flow — fix resource leak, menu flows, schema inspection, upsert
  ([`d804c1a`](https://github.com/homeassistant-ai/ha-mcp/commit/d804c1a1ebb652fa4adf34d10a5b0f0ea7d44826))

### Changed

- Update contributors list [contributors-updated]
  ([`69494ed`](https://github.com/homeassistant-ai/ha-mcp/commit/69494edfeda6c70e64874d27989ce30013f77d73))
- Add breaking change notice for v7.0.0 OAuth HOMEASSISTANT_URL requirement
  ([`60a6bfc`](https://github.com/homeassistant-ai/ha-mcp/commit/60a6bfc1ef8372a99dba944856da394bee5196e0))

### Fixed

- Ha_mcp_tools availability check always fails due to wrong services format
  ([#763](https://github.com/homeassistant-ai/ha-mcp/pull/763))
- Use REST API for ha_delete_config_entry
  ([#756](https://github.com/homeassistant-ai/ha-mcp/pull/756))
- Ensure skills are bundled in Docker builds, add guidance tools for claude.ai
  ([#732](https://github.com/homeassistant-ai/ha-mcp/pull/732))
- Clarify ha_search_entities vs ha_deep_search descriptions to prevent tool misuse
  ([#761](https://github.com/homeassistant-ai/ha-mcp/pull/761))
- Return empty success instead of RESOURCE_NOT_FOUND for empty logbook
  ([#710](https://github.com/homeassistant-ai/ha-mcp/pull/710))
- Prevent false success and duplicate creation in ha_config_set_automation
  ([#708](https://github.com/homeassistant-ai/ha-mcp/pull/708))
- Use package version for MCP server version instead of hardcoded 0.1.0
  ([#744](https://github.com/homeassistant-ai/ha-mcp/pull/744))
- Replace deprecated color_temp/kelvin with color_temp_kelvin for HA 2026.3
  ([#711](https://github.com/homeassistant-ai/ha-mcp/pull/711))
- Add blueprint/save step to ha_import_blueprint (#685)
  ([#751](https://github.com/homeassistant-ai/ha-mcp/pull/751))

---
<details>
<summary>Internal Changes</summary>


### Added

- Add summary output to contrib-pr-review skill
  ([`f063734`](https://github.com/homeassistant-ai/ha-mcp/commit/f06373452701402606cfcbfa8a85fec3a0bc6731))

### Fixed

- Reject non-Name/Attribute call targets in python_sandbox
  ([#772](https://github.com/homeassistant-ai/ha-mcp/pull/772))

### Chores

- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.10.11
  ([#778](https://github.com/homeassistant-ai/ha-mcp/pull/778))
- **deps**: Update fastmcp from 3.1.0 to 3.1.1
  ([#764](https://github.com/homeassistant-ai/ha-mcp/pull/764))
- **deps**: Bump devalue from 5.6.3 to 5.6.4 in /site
  ([#754](https://github.com/homeassistant-ai/ha-mcp/pull/754))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.10.9
  ([#742](https://github.com/homeassistant-ai/ha-mcp/pull/742))
- **addon**: Publish version 7.0.0 [skip ci]
  ([`8917644`](https://github.com/homeassistant-ai/ha-mcp/commit/8917644dc4e8cd5a4b8bf4afdac155a7c20f240d))

### Continuous Integration

- **deps**: Bump the github-actions group with 2 updates
  ([`f84511b`](https://github.com/homeassistant-ai/ha-mcp/commit/f84511b75d4bfe0c212d2162e3de7335f581172f))
</details>


## v7.0.0 (2026-03-11)

### Added

- Fix SSRF and XSS in OAuth consent form (breaking)
  ([#748](https://github.com/homeassistant-ai/ha-mcp/pull/748))
- **uat**: Add ha_checks post-run verification and openai agent improvements
  ([#713](https://github.com/homeassistant-ai/ha-mcp/pull/713))
- Add ha_check_update_notes tool for pre-update impact review
  ([#595](https://github.com/homeassistant-ai/ha-mcp/pull/595))
- Include persistent notifications in ha_get_overview
  ([#642](https://github.com/homeassistant-ai/ha-mcp/pull/642))
- Add Nabu Casa and other generic remote access via webhook proxy
  ([#554](https://github.com/homeassistant-ai/ha-mcp/pull/554))
- Serve bundled HA skills as MCP resources
  ([#679](https://github.com/homeassistant-ai/ha-mcp/pull/679))

### Changed

- Always create PRs as draft, mark ready only on user request
  ([#723](https://github.com/homeassistant-ai/ha-mcp/pull/723))
- Restore detailed maintainer descriptions lost in revert
  ([`01d744a`](https://github.com/homeassistant-ai/ha-mcp/commit/01d744a07114861d0bc908b26ee7c8947cc1633b))
- Always create PRs as draft, mark ready only on user request
  ([`63d57ae`](https://github.com/homeassistant-ai/ha-mcp/commit/63d57ae7e4b96335b17fc7aaa5e9dcba3c20c51d))
- Clarify that the MCP URL appears in the add-on logs, not HA logs
  ([#714](https://github.com/homeassistant-ai/ha-mcp/pull/714))
- Add Home Assistant OS add-on to Quick Install section
  ([#715](https://github.com/homeassistant-ai/ha-mcp/pull/715))

### Fixed

- **types**: Add mypy type checking and fix 47 type errors
  ([#716](https://github.com/homeassistant-ai/ha-mcp/pull/716))
- Resolve entity areas through device registry in get_system_overview
  ([#729](https://github.com/homeassistant-ai/ha-mcp/pull/729))
- Use per-client credentials for WebSocket in OAuth mode
  ([#704](https://github.com/homeassistant-ai/ha-mcp/pull/704))
- Resolve script storage key from entity registry (#463)
  ([#593](https://github.com/homeassistant-ai/ha-mcp/pull/593))
- Webhook proxy Dockerfile COPY paths for Supervisor builds
  ([#725](https://github.com/homeassistant-ai/ha-mcp/pull/725))

---
<details>
<summary>Internal Changes</summary>


### Added

- **ci**: Add automatic label classification to issue triage bot
  ([#745](https://github.com/homeassistant-ai/ha-mcp/pull/745))

### Fixed

- **ci**: Inject GITHUB_TOKEN into HACS config for reliable E2E tests
  ([#718](https://github.com/homeassistant-ai/ha-mcp/pull/718))
- **ci**: Fix changelog extraction producing empty release notes
  ([#707](https://github.com/homeassistant-ai/ha-mcp/pull/707))

### Chores

- **ci**: Group GitHub Actions dependabot updates into a single PR
  ([#739](https://github.com/homeassistant-ai/ha-mcp/pull/739))
- **deps**: Update fastmcp from 3.0.2 to 3.1.0
  ([#717](https://github.com/homeassistant-ai/ha-mcp/pull/717))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.10.7
  ([#697](https://github.com/homeassistant-ai/ha-mcp/pull/697))
- **deps**: Bump svgo from 4.0.0 to 4.0.1 in /site
  ([#703](https://github.com/homeassistant-ai/ha-mcp/pull/703))
- **addon**: Publish version 6.7.2 [skip ci]
  ([`0f92d3a`](https://github.com/homeassistant-ai/ha-mcp/commit/0f92d3abf3e916d08330e016b09bac3ebc6f1c40))

### Continuous Integration

- **deps**: Bump the github-actions group with 5 updates
  ([#740](https://github.com/homeassistant-ai/ha-mcp/pull/740))
- **deps**: Bump actions/upload-artifact from 6 to 7
  ([#692](https://github.com/homeassistant-ai/ha-mcp/pull/692))
- **deps**: Bump actions/download-artifact from 7 to 8
  ([#693](https://github.com/homeassistant-ai/ha-mcp/pull/693))
- **deps**: Bump renovatebot/github-action from 46.1.2 to 46.1.3
  ([#691](https://github.com/homeassistant-ai/ha-mcp/pull/691))
</details>


## v6.7.2 (2026-03-04)

### Changed

- Update contributors - simplify maintainer descriptions, add bigeric08
  ([`400ac23`](https://github.com/homeassistant-ai/ha-mcp/commit/400ac23e28b86a0686ad6f6a25d42adf3060e4be))
- Trim AGENTS.md to stay under 40k char limit
  ([#638](https://github.com/homeassistant-ai/ha-mcp/pull/638))

### Fixed

- Eliminate race condition in addon version updates
  ([#602](https://github.com/homeassistant-ai/ha-mcp/pull/602))
- Route person/zone/tag updates to config store APIs
  ([#622](https://github.com/homeassistant-ai/ha-mcp/pull/622))
- Standardize error handling patterns across all tool modules (#521)
  ([#678](https://github.com/homeassistant-ai/ha-mcp/pull/678))
- Return RESOURCE_NOT_FOUND instead of false success on dashboard deletion
  ([#680](https://github.com/homeassistant-ai/ha-mcp/pull/680))
- Upgrade to FastMCP v3.0.0
  ([#657](https://github.com/homeassistant-ai/ha-mcp/pull/657))

### Refactoring

- Consolidate redundant dashboard tools (3 tools removed)
  ([#660](https://github.com/homeassistant-ai/ha-mcp/pull/660))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Fix UAT framework bugs
  ([#665](https://github.com/homeassistant-ai/ha-mcp/pull/665))

### Chores

- **deps**: Bump rollup from 4.53.3 to 4.59.0 in /site
  ([#681](https://github.com/homeassistant-ai/ha-mcp/pull/681))
- **deps**: Bump devalue from 5.6.2 to 5.6.3 in /site
  ([#655](https://github.com/homeassistant-ai/ha-mcp/pull/655))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.10.5
  ([#673](https://github.com/homeassistant-ai/ha-mcp/pull/673))

### Continuous Integration

- Add uv.lock sync validation to CI and pre-commit
  ([#663](https://github.com/homeassistant-ai/ha-mcp/pull/663))
- **deps**: Bump renovatebot/github-action from 46.1.1 to 46.1.2
  ([#666](https://github.com/homeassistant-ai/ha-mcp/pull/666))
- Change stable release cadence from weekly to biweekly Wednesday
  ([#664](https://github.com/homeassistant-ai/ha-mcp/pull/664))
</details>


## v6.7.1 (2026-02-20)

### Fixed

- Sync uv.lock with pyproject.toml changes
  ([`0bf6f53`](https://github.com/homeassistant-ai/ha-mcp/commit/0bf6f537bffdd181416681b5152b6515efe87597))
- Pin fastmcp<3.0.0 to prevent silent server crashes
  ([#650](https://github.com/homeassistant-ai/ha-mcp/pull/650))
- Sync Docker runtime Python with builder and harden Renovate config
  ([#628](https://github.com/homeassistant-ai/ha-mcp/pull/628))


## v6.7.0 (2026-02-17)

### Added

- Add user acceptance stories for BAT framework
  ([#583](https://github.com/homeassistant-ai/ha-mcp/pull/583))
- Add ha_get_states tool for bulk entity state retrieval
  ([#588](https://github.com/homeassistant-ai/ha-mcp/pull/588))
- Add offset pagination to ha_search_entities and ha_hacs_search (#605)
  ([#619](https://github.com/homeassistant-ai/ha-mcp/pull/619))
- Add wait parameter to config and service call tools (#381)
  ([#564](https://github.com/homeassistant-ai/ha-mcp/pull/564))

### Changed

- Classify BAT metrics as primary vs secondary
  ([#639](https://github.com/homeassistant-ai/ha-mcp/pull/639))
- Update safety annotations with correct MCP spec definitions
  ([`59787a2`](https://github.com/homeassistant-ai/ha-mcp/commit/59787a261a60d41dc9e314dd3a851bb4a55d0f14))
- Add @maxperron as contributor for beta testing
  ([`0220708`](https://github.com/homeassistant-ai/ha-mcp/commit/0220708325aeca55c78349cb118423f9bad802ef))
- Update contributors - promote sergeykad and kingpanther13 to maintainers, add airlabno and ryphez
  ([`44f42b9`](https://github.com/homeassistant-ai/ha-mcp/commit/44f42b92de72b5a9e59279c19c8664c0a02b3f2a))
- Add Codex Desktop UI MCP quick setup
  ([#615](https://github.com/homeassistant-ai/ha-mcp/pull/615))

### Fixed

- Enable stateless_http in add-on and fix runtime Python version
  ([#626](https://github.com/homeassistant-ai/ha-mcp/pull/626))
- Treat 504 proxy error as expected during ha_restart
  ([#621](https://github.com/homeassistant-ai/ha-mcp/pull/621))
- Remove internal info leaks from error responses (#517)
  ([#586](https://github.com/homeassistant-ai/ha-mcp/pull/586))
- Reduce per-call token usage by slimming search responses and deep_search defaults
  ([#579](https://github.com/homeassistant-ai/ha-mcp/pull/579))
- Prevent ha_deep_search timeout on large HA instances
  ([#575](https://github.com/homeassistant-ai/ha-mcp/pull/575))
- Detect correct PR number when multiple PR refs exist in commit message
  ([#613](https://github.com/homeassistant-ai/ha-mcp/pull/613))
- Allow editing default dashboard without hyphen in url_path (#591)
  ([#592](https://github.com/homeassistant-ai/ha-mcp/pull/592))
- **tests**: Poll for entity registration in deep search E2E tests
  ([#589](https://github.com/homeassistant-ai/ha-mcp/pull/589))

### Refactoring

- Improve ruff linter config and fix violations
  ([#624](https://github.com/homeassistant-ai/ha-mcp/pull/624))
- **__main__**: Fix security issues, bugs, and reduce duplication
  ([#609](https://github.com/homeassistant-ai/ha-mcp/pull/609))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Add ruff pre-commit hook and CI lint job
  ([#604](https://github.com/homeassistant-ai/ha-mcp/pull/604))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.9.30
  ([#597](https://github.com/homeassistant-ai/ha-mcp/pull/597))
- **deps**: Update python docker tag to v3.14
  ([#598](https://github.com/homeassistant-ai/ha-mcp/pull/598))
- Enforce LF line endings via .gitattributes
  ([#596](https://github.com/homeassistant-ai/ha-mcp/pull/596))

### Continuous Integration

- **deps**: Bump actions/cache from 4 to 5
  ([#632](https://github.com/homeassistant-ai/ha-mcp/pull/632))
- **deps**: Bump renovatebot/github-action from 46.0.2 to 46.1.1
  ([#631](https://github.com/homeassistant-ai/ha-mcp/pull/631))
- Add unit tests to PR pipeline and pre-commit hook
  ([#620](https://github.com/homeassistant-ai/ha-mcp/pull/620))
- **deps**: Bump renovatebot/github-action from 46.0.1 to 46.0.2
  ([#584](https://github.com/homeassistant-ai/ha-mcp/pull/584))
</details>


## v6.6.1 (2026-02-10)

### Fixed

- Sync uv.lock with v6.6.0 version bump (#594)
  ([#599](https://github.com/homeassistant-ai/ha-mcp/pull/599))


## v6.6.0 (2026-02-10)

### Added

- Add human-readable timestamps to logs, apply ruff fixes (#574)
  ([#580](https://github.com/homeassistant-ai/ha-mcp/pull/580))
- Add Gemini Code Assist configuration and update documentation
  ([#582](https://github.com/homeassistant-ai/ha-mcp/pull/582))
- Add contrib-pr-review skill for external contribution review
  ([`0618bf9`](https://github.com/homeassistant-ai/ha-mcp/commit/0618bf9270b9db944b4a0a52ca2ae28e7af61e1d))
- Add aggregate stats to BAT summary for branch comparison
  ([`8fe8ab8`](https://github.com/homeassistant-ai/ha-mcp/commit/8fe8ab815ae7a62ce0418d81860f5f5fc8f1b479))
- Add /bat skill for bot acceptance testing
  ([`906e22f`](https://github.com/homeassistant-ai/ha-mcp/commit/906e22f076ed0b310e2d06343b08296a3ee65cd1))
- Add UAT framework for agent-driven acceptance testing
  ([`b561ad4`](https://github.com/homeassistant-ai/ha-mcp/commit/b561ad447cb3b780715899bac8ae9ea6220e57ad))
- Add domain filter and options support to ha_get_integration
  ([#542](https://github.com/homeassistant-ai/ha-mcp/pull/542))

### Changed

- Add comment formatting guidelines to contrib-pr-review
  ([`c014e8a`](https://github.com/homeassistant-ai/ha-mcp/commit/c014e8a08be26421d55e00299648b68f7689d1fb))
- Add contrib-pr-review skill to AGENTS.md
  ([`4aa29c3`](https://github.com/homeassistant-ai/ha-mcp/commit/4aa29c3662942c005336288613a207177091b2c7))
- Add warning to review PRs sequentially, not in parallel
  ([`d69c576`](https://github.com/homeassistant-ai/ha-mcp/commit/d69c576c09b2214a6c5fbf6112bfccfb3d7bd4ae))

### Fixed

- Address review comments on UAT runner
  ([`6a2bf04`](https://github.com/homeassistant-ai/ha-mcp/commit/6a2bf0430261e6a07b0738e3a5e98532bccfb636))
- Handle service call timeouts gracefully and add missing @log_tool usage (fixes #550)
  ([#555](https://github.com/homeassistant-ai/ha-mcp/pull/555))
- Optimize Dockerfiles with multi-stage builds
  ([#546](https://github.com/homeassistant-ai/ha-mcp/pull/546))

### Performance Improvements

- Run agents sequentially instead of in parallel
  ([`b3032f4`](https://github.com/homeassistant-ai/ha-mcp/commit/b3032f4fb745516e184ee2278cd900b440afd964))

### Refactoring

- Rename pr-checker to my-pr-checker for clarity
  ([`a02533c`](https://github.com/homeassistant-ai/ha-mcp/commit/a02533c16a1b7ea8f4f3f0f51cd949f0b1bc01a3))
- Rename UAT to BAT and add progressive disclosure output
  ([`8a6d43e`](https://github.com/homeassistant-ai/ha-mcp/commit/8a6d43e9cd2e20a3d7ca6fbd1be5b986901bd8cf))

---
<details>
<summary>Internal Changes</summary>


### Changed

- Clarify worktree workflow and symlink convention in AGENTS.md
  ([`9946be5`](https://github.com/homeassistant-ai/ha-mcp/commit/9946be57ee69a267054a7ac31ffb6b408cc3a99b))
- Restructure worktree workflow and documentation
  ([#547](https://github.com/homeassistant-ai/ha-mcp/pull/547))

### Build System

- **deps**: Bump astral-sh/uv
  ([#535](https://github.com/homeassistant-ai/ha-mcp/pull/535))

### Chores

- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026
  ([#508](https://github.com/homeassistant-ai/ha-mcp/pull/508))

### Continuous Integration

- **deps**: Bump renovatebot/github-action from 44.2.6 to 46.0.1
  ([#536](https://github.com/homeassistant-ai/ha-mcp/pull/536))
</details>


## v6.5.0 (2026-02-03)

### Added

- Remove encryption from OAuth tokens for truly stateless implementation
  ([#534](https://github.com/homeassistant-ai/ha-mcp/pull/534))
- **oauth**: Auto-persist encryption key and auto-detect url
  ([#532](https://github.com/homeassistant-ai/ha-mcp/pull/532))

### Changed

- Add agent skills section to README
  ([#541](https://github.com/homeassistant-ai/ha-mcp/pull/541))

### Fixed

- Add workaround for ChatGPT's non-standard /token/.well-known/openid-configuration request
  ([#533](https://github.com/homeassistant-ai/ha-mcp/pull/533))
- **oauth**: Add OpenID Configuration endpoint for ChatGPT compatibility
  ([#531](https://github.com/homeassistant-ai/ha-mcp/pull/531))
- **traces**: Support flat trace structure in ha_get_automation_traces
  ([#529](https://github.com/homeassistant-ai/ha-mcp/pull/529))
- Fix YAML frontmatter parsing in agent files
  ([#519](https://github.com/homeassistant-ai/ha-mcp/pull/519))

---
<details>
<summary>Internal Changes</summary>


### Chores

- **config**: Migrate config renovate.json
  ([#509](https://github.com/homeassistant-ai/ha-mcp/pull/509))
- Add Anthropic's MCP builder skill via plugin marketplace
  ([#520](https://github.com/homeassistant-ai/ha-mcp/pull/520))
</details>


## v6.4.0 (2026-01-27)

### Added

- Add python_transform for cross-platform dashboard updates
  ([#496](https://github.com/homeassistant-ai/ha-mcp/pull/496))
- Enable stateless_http mode for restart resilience
  ([#495](https://github.com/homeassistant-ai/ha-mcp/pull/495))
- **workflow**: Clarify Gemini triage is read-only, add diff format for fixes
  ([`3e89988`](https://github.com/homeassistant-ai/ha-mcp/commit/3e899888269135ce36307365ab2d4c9923bcdc31))
- **workflow**: Skip automated triage for julienld's issues
  ([`2b74ee9`](https://github.com/homeassistant-ai/ha-mcp/commit/2b74ee9cff460403c3e8ed1475e1841e112c5a44))
- Add AI-powered issue triage workflow and simplified YAML templates
  ([`69e2fd0`](https://github.com/homeassistant-ai/ha-mcp/commit/69e2fd0de44bdcf037e9e8926f22b9f425233b2c))
- **entity**: Add ha_update_entity tool for entity registry updates
  ([#469](https://github.com/homeassistant-ai/ha-mcp/pull/469))
- Improve ha_report_issue with title, duplicate check, and markdown formatting
  ([#484](https://github.com/homeassistant-ai/ha-mcp/pull/484))
- Add ha-mcp-dev executable with automatic DEBUG logging
  ([`79a1456`](https://github.com/homeassistant-ai/ha-mcp/commit/79a145680eb24d093bbc0293a7129b814e832c43))
- Publish dev builds to separate ha-mcp-dev package
  ([`f768dd2`](https://github.com/homeassistant-ai/ha-mcp/commit/f768dd21303a4f7b4acf44572ddcdf6328c62926))
- Publish dev builds to PyPI for --pre flag support
  ([`e1e73e1`](https://github.com/homeassistant-ai/ha-mcp/commit/e1e73e1423e118615246ce25f990860a6d8fe587))

### Changed

- Add guidance to resolve review threads with comments
  ([`03ad555`](https://github.com/homeassistant-ai/ha-mcp/commit/03ad5553d9ca8d449f96df2eb77b0b0fd2d79c7a))
- **workflow**: Clarify only gh issue list/view commands available
  ([`2501fde`](https://github.com/homeassistant-ai/ha-mcp/commit/2501fdec4870c7313c18bb762e9dbf17bda8162d))
- Update contributors section with recent contributions
  ([#492](https://github.com/homeassistant-ai/ha-mcp/pull/492))
- Add MCP tool authoring guide to AGENTS.md
  ([#461](https://github.com/homeassistant-ai/ha-mcp/pull/461))
- Move OAuth to separate guide, position as beta alternative
  ([#487](https://github.com/homeassistant-ai/ha-mcp/pull/487))
- Add comprehensive dev channel documentation
  ([#476](https://github.com/homeassistant-ai/ha-mcp/pull/476))
- Add uvx cache troubleshooting to FAQ
  ([`f21c431`](https://github.com/homeassistant-ai/ha-mcp/commit/f21c4310235130f77c54bb48e341adcae69ed935))

### Fixed

- Update ha_report_issue URLs and improve workflow PR extraction
  ([#505](https://github.com/homeassistant-ai/ha-mcp/pull/505))
- **workflow**: Restrict Gemini to read-only gh commands
  ([`5e27889`](https://github.com/homeassistant-ai/ha-mcp/commit/5e27889c41546f2f2e2b1171e6cdf411fa3b64e5))
- Validate label IDs in ha_manage_entity_labels to prevent silent failures
  ([#486](https://github.com/homeassistant-ai/ha-mcp/pull/486))
- Update package name reference in version lookup for ha-mcp-dev
  ([`97df158`](https://github.com/homeassistant-ai/ha-mcp/commit/97df1582cf81a61337b78801ea40c19d56045a03))
- Pin httpx to <1.0 to prevent incompatible prerelease versions
  ([#483](https://github.com/homeassistant-ai/ha-mcp/pull/483))
- Validate operations in ha_bulk_control and report errors (#385)
  ([#473](https://github.com/homeassistant-ai/ha-mcp/pull/473))
- Remove redundant asyncio.sleep calls in E2E helper tests
  ([#470](https://github.com/homeassistant-ai/ha-mcp/pull/470))

### Refactoring

- Standardize MCP tool error handling and fix test compatibility
  ([#494](https://github.com/homeassistant-ai/ha-mcp/pull/494))
- **agents**: Rebrand level2-triage to issue-analysis workflow
  ([`d2748ab`](https://github.com/homeassistant-ai/ha-mcp/commit/d2748abb8a304e6f8683305f36164b8438b92b00))
- **agents**: Convert triage agent to level2-triaged workflow
  ([`407da6a`](https://github.com/homeassistant-ai/ha-mcp/commit/407da6acfc8ecb65f1f6afedc858006273e2795e))

---
<details>
<summary>Internal Changes</summary>


### Added

- **ci**: Add workflow to notify PRs/issues when merged to dev
  ([#489](https://github.com/homeassistant-ai/ha-mcp/pull/489))

### Fixed

- **ci**: Support squash merge format in notify workflow
  ([#491](https://github.com/homeassistant-ai/ha-mcp/pull/491))

### Continuous Integration

- **deps**: Bump renovatebot/github-action from 44.2.4 to 44.2.6
  ([#499](https://github.com/homeassistant-ai/ha-mcp/pull/499))
</details>


## v6.3.1 (2026-01-20)

### Changed

- Add @kingpanther13 and @Raygooo to contributors
  ([`590d0b7`](https://github.com/homeassistant-ai/ha-mcp/commit/590d0b78b3d4b04a260b26bf738e51d97c91b6cf))
- **agents**: Add "Leave the Campground Cleaner" principle
  ([`e11d766`](https://github.com/homeassistant-ai/ha-mcp/commit/e11d766b68d63fce34cf5d97a31074526369930f))

### Fixed

- Add socks support to httpx dependency
  ([#450](https://github.com/homeassistant-ai/ha-mcp/pull/450))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- **ci**: Robust release publishing logic
  ([#444](https://github.com/homeassistant-ai/ha-mcp/pull/444))

### Build System

- **deps**: Bump astral-sh/uv
  ([#454](https://github.com/homeassistant-ai/ha-mcp/pull/454))
- **deps**: Bump diff and astro in /site
  ([#441](https://github.com/homeassistant-ai/ha-mcp/pull/441))
</details>


## v6.3.0 (2026-01-17)

### Added

- OAuth 2.1 Authentication with DCR and Consent Form
  ([#368](https://github.com/homeassistant-ai/ha-mcp/pull/368))

### Changed

- Redesign changelog for end-user readability
  ([#434](https://github.com/homeassistant-ai/ha-mcp/pull/434))

### Fixed

- Change log path to user home and force uvx refresh in install scripts
  ([#443](https://github.com/homeassistant-ai/ha-mcp/pull/443))

---
<details>
<summary>Internal Changes</summary>


### Build System

- **deps**: Bump h3 from 1.15.4 to 1.15.5 in /site
  ([#436](https://github.com/homeassistant-ai/ha-mcp/pull/436))
- **deps**: Bump devalue from 5.5.0 to 5.6.2 in /site
  ([#435](https://github.com/homeassistant-ai/ha-mcp/pull/435))
- **deps**: Bump astral-sh/uv
  ([#426](https://github.com/homeassistant-ai/ha-mcp/pull/426))

### Continuous Integration

- **deps**: Bump renovatebot/github-action from 44.2.3 to 44.2.4
  ([#425](https://github.com/homeassistant-ai/ha-mcp/pull/425))

### Refactoring

- **deps**: Replace textdistance with stdlib difflib
  ([#432](https://github.com/homeassistant-ai/ha-mcp/pull/432))
</details>


## v6.2.0 (2026-01-12)

### Added

- Consolidate duplicate tools (108 → 105 tools)
  ([#423](https://github.com/homeassistant-ai/ha-mcp/pull/423))
- **addon**: Log package version on startup
  ([#419](https://github.com/homeassistant-ai/ha-mcp/pull/419))

### Fixed

- **client**: Ensure REST API paths are correctly resolved relative to /api/
  ([#418](https://github.com/homeassistant-ai/ha-mcp/pull/418))
- Pin numpy to 2.3.x for CPU compatibility
  ([#410](https://github.com/homeassistant-ai/ha-mcp/pull/410))

---
<details>
<summary>Internal Changes</summary>


### Added

- **debug**: Test direct connection to Core
  ([`02d7f61`](https://github.com/homeassistant-ai/ha-mcp/commit/02d7f612a9f21a74d0e91a6849eda077505823ee))
- **debug**: Add verbose logging and connection test for add-on
  ([#421](https://github.com/homeassistant-ai/ha-mcp/pull/421))

### Fixed

- **addon-dev**: Set hassio_role to admin (retry)
  ([#417](https://github.com/homeassistant-ai/ha-mcp/pull/417))
- **addon-dev**: Set hassio_role to homeassistant to allow DELETE operations
  ([#416](https://github.com/homeassistant-ai/ha-mcp/pull/416))
</details>


## v6.1.0 (2026-01-10)

### Added

- Harmonize config entry tools and add Flow API support
  ([#403](https://github.com/homeassistant-ai/ha-mcp/pull/403))
- Improve bug report clarity and add agent behavior feedback
  ([#401](https://github.com/homeassistant-ai/ha-mcp/pull/401))

### Changed

- Fix Cloudflared add-on Quick Tunnel documentation inaccuracy
  ([#407](https://github.com/homeassistant-ai/ha-mcp/pull/407))
- Move @cj-elevate to end of contributors list
  ([`7b452ed`](https://github.com/homeassistant-ai/ha-mcp/commit/7b452ede8dff8fa59839ba065e1ba84c0af627fb))
- Add @cj-elevate to contributors for PR #355
  ([`bba1c89`](https://github.com/homeassistant-ai/ha-mcp/commit/bba1c89db94b93c54ebd121b185aa38e2cce8853))

### Fixed

- Preserve nested conditions in or/and/not compound condition blocks
  ([#409](https://github.com/homeassistant-ai/ha-mcp/pull/409))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- **ci**: Add debug output and re-check draft status before publishing
  ([#400](https://github.com/homeassistant-ai/ha-mcp/pull/400))
</details>


## v6.0.0 (2026-01-07)

### Added

- Add Codex CLI support to setup wizard
  ([#387](https://github.com/homeassistant-ai/ha-mcp/pull/387))
- Redesign label management with add/remove/set operations
  ([#397](https://github.com/homeassistant-ai/ha-mcp/pull/397))

### Fixed

- Add truncation indicator to ha_search_entities
  ([#393](https://github.com/homeassistant-ai/ha-mcp/pull/393))
- Apply domain filter before fuzzy search, not after
  ([#394](https://github.com/homeassistant-ai/ha-mcp/pull/394))

---
<details>
<summary>Internal Changes</summary>


### Testing

- Add comprehensive E2E tests for label operations
  ([#399](https://github.com/homeassistant-ai/ha-mcp/pull/399))
</details>


## v5.1.0 (2026-01-06)

### Added

- Update pr-checker agent with PR execution philosophy
  ([`80bf518`](https://github.com/homeassistant-ai/ha-mcp/commit/80bf51896f4738f910ac68ab193e55bd19e1b393))
- Update issue-to-pr-resolver agent with PR execution philosophy
  ([`075b64a`](https://github.com/homeassistant-ai/ha-mcp/commit/075b64aa25010e3482aeda2e7ccc0a13f1e166e1))

### Changed

- Add workflow for implementing improvements in separate PRs
  ([`dd6aafc`](https://github.com/homeassistant-ai/ha-mcp/commit/dd6aafc62055c9cd92fe71fa68929b2f6c00fbcc))
- Add PR execution philosophy and final reporting guidelines
  ([`b6a5473`](https://github.com/homeassistant-ai/ha-mcp/commit/b6a547365ad03cf1518af56a757b780b2bfc880c))
- Clarify PR workflow with explicit comment checking
  ([`d9d6b35`](https://github.com/homeassistant-ai/ha-mcp/commit/d9d6b354dec479d2c0e9a2f327442cd6c5f9d9d7))
- Simplify ha_call_service docstring (117→34 lines)
  ([#379](https://github.com/homeassistant-ai/ha-mcp/pull/379))
- Change sponsor badge to blueviolet
  ([`1a1102f`](https://github.com/homeassistant-ai/ha-mcp/commit/1a1102f8694d4127eeb7af6e9cbaaea419d36646))
- Update sponsor badge text and color
  ([`939a09e`](https://github.com/homeassistant-ai/ha-mcp/commit/939a09eb67e7797d561090fe26a3db8279764b0d))
- Change sponsor emoji from heart to coffee
  ([`8f026df`](https://github.com/homeassistant-ai/ha-mcp/commit/8f026dfedf76de7d3788a42ae444c0bb6de64fd2))
- Add sponsor badge, community section, and star history
  ([`2fe299b`](https://github.com/homeassistant-ai/ha-mcp/commit/2fe299bb8815a08fd488489ab151454005b3c7d0))

### Fixed

- Preserve 'conditions' (plural) in choose/if blocks
  ([#388](https://github.com/homeassistant-ai/ha-mcp/pull/388))
- Resolve WebSocket race conditions and improve error handling
  ([#378](https://github.com/homeassistant-ai/ha-mcp/pull/378))

---
<details>
<summary>Internal Changes</summary>


### Build System

- **deps**: Bump astral-sh/uv
  ([#390](https://github.com/homeassistant-ai/ha-mcp/pull/390))

### Continuous Integration

- **deps**: Bump renovatebot/github-action from 44.2.2 to 44.2.3
  ([#389](https://github.com/homeassistant-ai/ha-mcp/pull/389))
- **deps**: Bump renovatebot/github-action from 44.2.1 to 44.2.2
  ([#372](https://github.com/homeassistant-ai/ha-mcp/pull/372))
</details>


## v5.0.6 (2025-12-28)

### Fixed

- Exclude jq dependency on all Windows platforms
  ([#371](https://github.com/homeassistant-ai/ha-mcp/pull/371))


## v5.0.5 (2025-12-24)

### Changed

- Document hotfix workflow with stable tag verification and timing
  ([`6bbd782`](https://github.com/homeassistant-ai/ha-mcp/commit/6bbd782ea31fad3e5d4d8aac0a03e26a4ec9a41a))

### Fixed

- Support blueprint automations in ha_config_set_automation
  ([#364](https://github.com/homeassistant-ai/ha-mcp/pull/364))
- **docs**: Update AGENTS.md with ha-mcp-web command
  ([`25ddcb7`](https://github.com/homeassistant-ai/ha-mcp/commit/25ddcb7e081bf029022588c82e5aeca260f97179))
- **docs**: Update Docker commands to use ha-mcp-web and remove backslashes
  ([`90822c0`](https://github.com/homeassistant-ai/ha-mcp/commit/90822c087b18cfb68eb2bc23c062a8494356011a))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- **ci**: Correct regex - match version digits only
  ([`970c358`](https://github.com/homeassistant-ai/ha-mcp/commit/970c358ab8c260564b98f51dd033f4ca06f58fe5))
- **ci**: Improve renovate regex pattern for HA container version
  ([`32da751`](https://github.com/homeassistant-ai/ha-mcp/commit/32da7510bcc5f71667f243f0d0f942b44348050a))
- **ci**: Clear ignorePaths to allow scanning tests/
  ([`b363519`](https://github.com/homeassistant-ai/ha-mcp/commit/b363519c2a05fb66bf21d012bedcc9d015f2fc28))
- **ci**: Use correct manager name custom.regex
  ([`e8bded1`](https://github.com/homeassistant-ai/ha-mcp/commit/e8bded1d8152e242a7aa91d7c66dd5a8256e3f5d))
- **ci**: Configure Renovate to only handle HA test container
  ([`22eefd1`](https://github.com/homeassistant-ai/ha-mcp/commit/22eefd1e71fc63f49e0113647c44ba99e2578d63))
- **ci**: Update HA test container and separate Renovate schedule
  ([`0a4bc2f`](https://github.com/homeassistant-ai/ha-mcp/commit/0a4bc2f2de8fce292dd15afe894a088a5e8dec61))
- **ci**: Configure Renovate to scan current repository
  ([`553917a`](https://github.com/homeassistant-ai/ha-mcp/commit/553917a5603f21474e8040a2cc5d050a48f00975))
</details>


## v5.0.4 (2025-12-23)

### Fixed

- Make jq optional on Windows ARM64
  ([#359](https://github.com/homeassistant-ai/ha-mcp/pull/359))


## v5.0.3 (2025-12-23)

### Fixed

- Resolve Docker environment variable validation error (#354)
  ([#356](https://github.com/homeassistant-ai/ha-mcp/pull/356))


## v5.0.2 (2025-12-22)

---
<details>
<summary>Internal Changes</summary>


### Fixed

- **ci**: Complete workflow fixes for unified release
  ([`c64f41a`](https://github.com/homeassistant-ai/ha-mcp/commit/c64f41a390a6cc514d1330d4b39e9e785947bb1e))
- **ci**: Create draft pre-releases for dev builds
  ([#352](https://github.com/homeassistant-ai/ha-mcp/pull/352))
- **ci**: Add git checkout for gh release upload
  ([#351](https://github.com/homeassistant-ai/ha-mcp/pull/351))
- **ci**: Filter artifact downloads to skip Docker build cache
  ([#350](https://github.com/homeassistant-ai/ha-mcp/pull/350))
- **ci**: Correct build command in reusable workflow
  ([#349](https://github.com/homeassistant-ai/ha-mcp/pull/349))
- **ci**: Checkout current commit instead of tag in build jobs
  ([`6f6da4e`](https://github.com/homeassistant-ai/ha-mcp/commit/6f6da4e2a8ff74a7eace2de10dbe9603f231cfe7))
- **ci**: Create pre-release as draft before uploading binaries
  ([`821bcf4`](https://github.com/homeassistant-ai/ha-mcp/commit/821bcf46d95857a08580db698f5a9275fea33004))
- **ci**: Add checkout step for gh release upload
  ([`4df604a`](https://github.com/homeassistant-ai/ha-mcp/commit/4df604ae3f82f37f46a5f75c785d7f41283ba168))
- **ci**: Only download binary artifacts, skip Docker build cache
  ([`6ca14b3`](https://github.com/homeassistant-ai/ha-mcp/commit/6ca14b3373bad908dc0dd86cd00c2f52ad9668dd))
- **ci**: Correct build command in reusable workflow
  ([`e299bf0`](https://github.com/homeassistant-ai/ha-mcp/commit/e299bf0216f3e9d48b9ba55fb3eddc18e3fb0efd))

### Build System

- **deps**: Bump astral-sh/uv
  ([#344](https://github.com/homeassistant-ai/ha-mcp/pull/344))

### Continuous Integration

- **deps**: Bump actions/create-github-app-token from 1 to 2
  ([#343](https://github.com/homeassistant-ai/ha-mcp/pull/343))
- **deps**: Bump renovatebot/github-action from 44.1.0 to 44.2.1
  ([#345](https://github.com/homeassistant-ai/ha-mcp/pull/345))
- **deps**: Bump python-semantic-release/python-semantic-release
  ([#346](https://github.com/homeassistant-ai/ha-mcp/pull/346))

### Refactoring

- **ci**: Unify release workflows with reusable build workflow
  ([#348](https://github.com/homeassistant-ai/ha-mcp/pull/348))
- **ci**: Unify release workflows with reusable build workflow
  ([`048a686`](https://github.com/homeassistant-ai/ha-mcp/commit/048a686c904c96cc6cce9bdb52d95dc20da79b29))
</details>


## v5.0.1 (2025-12-21)

### Added

- **dashboard**: Add jq_transform and find_card for efficient editing
  ([#333](https://github.com/homeassistant-ai/ha-mcp/pull/333))

### Changed

- **antigravity**: Remove known issues reference
  ([`f37eed9`](https://github.com/homeassistant-ai/ha-mcp/commit/f37eed9dd23b680242a2066780e21fb4cd65b160))
- Add FASTMCP_SHOW_CLI_BANNER workaround for Antigravity
  ([`eb222dd`](https://github.com/homeassistant-ai/ha-mcp/commit/eb222dd92f0f897a96047c631c5a52505ff86d38))

### Fixed

- Respect FASTMCP_SHOW_CLI_BANNER env var for banner control
  ([#336](https://github.com/homeassistant-ai/ha-mcp/pull/336))
- Update MCP Registry schema to 2025-12-11
  ([`c0f0a2e`](https://github.com/homeassistant-ai/ha-mcp/commit/c0f0a2e487f123b08e11b371dca8a5a23b6aeb1c))
- Update MCP Registry schema to current draft version
  ([`2401a05`](https://github.com/homeassistant-ai/ha-mcp/commit/2401a0533f47289ee4b4c7c60cf1352b88e3517b))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- **ci**: Use GitHub App token for releases with bypass permissions
  ([#340](https://github.com/homeassistant-ai/ha-mcp/pull/340))
- **ci**: Use RELEASE_TOKEN for tag creation to bypass rulesets
  ([#339](https://github.com/homeassistant-ai/ha-mcp/pull/339))
- **ci**: Dereference annotated tags in hotfix validation
  ([`9f223f2`](https://github.com/homeassistant-ai/ha-mcp/commit/9f223f277f3357cd6313f2e5fc31d1030f88f56d))
</details>


## v4.22.1 (2025-12-18)

### Changed

- **antigravity**: Recommend stdio mode, add troubleshooting
  ([`8dac62e`](https://github.com/homeassistant-ai/ha-mcp/commit/8dac62e6102e498c1e13ce26787e8699c8193e90))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- **ci**: Don't suppress upload errors in build-binary
  ([`3185c28`](https://github.com/homeassistant-ai/ha-mcp/commit/3185c2816b857df0c17e3068a8d184f73e72c4c5))
- **ci**: Resolve recurring workflow failures
  ([`ae1934b`](https://github.com/homeassistant-ai/ha-mcp/commit/ae1934b8c446ac06811cdf108c938c0ea58116df))

### Continuous Integration

- **deps**: Bump actions/upload-artifact from 4 to 6
  ([#328](https://github.com/homeassistant-ai/ha-mcp/pull/328))
- **deps**: Bump actions/setup-python from 5 to 6
  ([#327](https://github.com/homeassistant-ai/ha-mcp/pull/327))
- **deps**: Bump astral-sh/setup-uv from 4 to 7
  ([#326](https://github.com/homeassistant-ai/ha-mcp/pull/326))
- **deps**: Bump actions/download-artifact from 6 to 7
  ([#325](https://github.com/homeassistant-ai/ha-mcp/pull/325))

### Refactoring

- **ci**: Use draft releases for atomic release creation
  ([`5214097`](https://github.com/homeassistant-ai/ha-mcp/commit/52140979ec71cf6c21b6679a8574585e6ac9e8fb))
</details>


## v4.22.0 (2025-12-16)

### Added

- Add all helpers with WebSocket API support
  ([#323](https://github.com/homeassistant-ai/ha-mcp/pull/323))
- Add informational tool for HA configuration access
  ([#322](https://github.com/homeassistant-ai/ha-mcp/pull/322))

### Changed

- Add fact-checking caveat to model knowledge testing
  ([`ea5cb33`](https://github.com/homeassistant-ai/ha-mcp/commit/ea5cb336fa74378136a197a6da2834ca0c4af79a))
- Add no-context sub-agent strategy for testing model knowledge
  ([`9e737a0`](https://github.com/homeassistant-ai/ha-mcp/commit/9e737a0b3a3588e0fa31ed331d58c26e43df27b4))
- Add context engineering & progressive disclosure principles
  ([`40ab2a6`](https://github.com/homeassistant-ai/ha-mcp/commit/40ab2a65f721f65f16f0cb1d48259bb5e4fafeef))

### Fixed

- Apply LOG_LEVEL environment variable to Python logging
  ([#321](https://github.com/homeassistant-ai/ha-mcp/pull/321))

---
<details>
<summary>Internal Changes</summary>


### Build System

- **deps**: Bump astral-sh/uv
  ([#330](https://github.com/homeassistant-ai/ha-mcp/pull/330))

### Continuous Integration

- **deps**: Bump renovatebot/github-action from 44.0.5 to 44.1.0
  ([#329](https://github.com/homeassistant-ai/ha-mcp/pull/329))
</details>


## v4.21.0 (2025-12-11)

### Added

- Add ENABLED_TOOL_MODULES env var for tool filtering
  ([#316](https://github.com/homeassistant-ai/ha-mcp/pull/316))

### Changed

- Update Open WebUI instructions and setup wizard
  ([`67d03df`](https://github.com/homeassistant-ai/ha-mcp/commit/67d03df80eac5f4e581ef43727b9bbbe04612cc3))


## v4.20.0 (2025-12-09)

### Added

- Add ha_create_dashboard_resource tool for inline JS/CSS hosting
  ([#297](https://github.com/homeassistant-ai/ha-mcp/pull/297))

### Changed

- Reorganize FAQ and update client list
  ([`e7852ac`](https://github.com/homeassistant-ai/ha-mcp/commit/e7852ac96715bd8c0a934ce6eb5f1c112e9a19e6))
- Improve Setup Wizard section in README
  ([`4e1efab`](https://github.com/homeassistant-ai/ha-mcp/commit/4e1efab1b252904dfb8b373617c02ddb7c6d449e))
- Update README links and add Docker platform to setup wizard
  ([`56f62a6`](https://github.com/homeassistant-ai/ha-mcp/commit/56f62a6a89ae843dc077081dfb74d142faa644ca))
- Add @sergeykad to contributors
  ([`9d85ac0`](https://github.com/homeassistant-ai/ha-mcp/commit/9d85ac00b5084400fa2c5418aea2cd48fcd98560))

### Fixed

- Add --version/-V flag to CLI
  ([#312](https://github.com/homeassistant-ai/ha-mcp/pull/312))
- Use --version instead of --help in installer scripts
  ([#310](https://github.com/homeassistant-ai/ha-mcp/pull/310))
- Add --version/-V flag to CLI
  ([#309](https://github.com/homeassistant-ai/ha-mcp/pull/309))
- Update favicon to Home Assistant icon
  ([`02f33db`](https://github.com/homeassistant-ai/ha-mcp/commit/02f33dbade6176a1aeac4e706d1a3544e1acb720))

---
<details>
<summary>Internal Changes</summary>


### Build System

- **deps**: Bump astral-sh/uv
  ([#303](https://github.com/homeassistant-ai/ha-mcp/pull/303))

### Continuous Integration

- **deps**: Bump actions/download-artifact from 4 to 6
  ([#305](https://github.com/homeassistant-ai/ha-mcp/pull/305))
- **deps**: Bump actions/upload-pages-artifact from 3 to 4
  ([#304](https://github.com/homeassistant-ai/ha-mcp/pull/304))
- **deps**: Bump actions/checkout from 4 to 6
  ([#302](https://github.com/homeassistant-ai/ha-mcp/pull/302))
- **deps**: Bump actions/setup-node from 4 to 6
  ([#300](https://github.com/homeassistant-ai/ha-mcp/pull/300))
- **deps**: Bump actions/configure-pages from 4 to 5
  ([#299](https://github.com/homeassistant-ai/ha-mcp/pull/299))
- **deps**: Bump renovatebot/github-action from 44.0.4 to 44.0.5
  ([#301](https://github.com/homeassistant-ai/ha-mcp/pull/301))
</details>


## v4.19.0 (2025-12-07)

### Added

- Add filesystem access tools for Home Assistant config files
  ([#276](https://github.com/homeassistant-ai/ha-mcp/pull/276))
- Add dashboard resource management tools
  ([#278](https://github.com/homeassistant-ai/ha-mcp/pull/278))
- Weekly stable releases with hotfix support
  ([#292](https://github.com/homeassistant-ai/ha-mcp/pull/292))

### Changed

- Update AGENTS.md with parallel triage workflow
  ([`5239b29`](https://github.com/homeassistant-ai/ha-mcp/commit/5239b295931a2dcc10b841c5d8392c4fa14fe50b))

### Fixed

- Use system CA certificates for SSL verification
  ([#294](https://github.com/homeassistant-ai/ha-mcp/pull/294))
- Preserve voice assistant exposure settings when renaming entities
  ([#271](https://github.com/homeassistant-ai/ha-mcp/pull/271))
- Correct cleanup logic to parse tag from gh release list
  ([`e3abb76`](https://github.com/homeassistant-ai/ha-mcp/commit/e3abb7615bfe0863038f0eddb01daa25e4e0e067))

### Performance Improvements

- Implement parallel operations for improved performance (#258)
  ([#269](https://github.com/homeassistant-ai/ha-mcp/pull/269))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Rename github-issue-analyzer agent to triage with enhanced behavior
  ([`a730fd4`](https://github.com/homeassistant-ai/ha-mcp/commit/a730fd43c0df646ba741d4de2b4bb33b582cac64))

### Testing

- Add comprehensive tests for group management tools
  ([#277](https://github.com/homeassistant-ai/ha-mcp/pull/277))
- Add performance measurement to E2E tests
  ([#270](https://github.com/homeassistant-ai/ha-mcp/pull/270))
</details>


## v4.18.2 (2025-12-07)

### Fixed

- **site**: Add stdio support for Antigravity (same config as Windsurf)
  ([`0fbf5e8`](https://github.com/homeassistant-ai/ha-mcp/commit/0fbf5e8e81bec93f3f003311082b57e92724606e))


## v4.18.1 (2025-12-07)

### Fixed

- **site**: Add Open WebUI client configuration instructions
  ([`75f7f8b`](https://github.com/homeassistant-ai/ha-mcp/commit/75f7f8b914731e45d8d1102a88f6e05f8aefb3e1))


## v4.18.0 (2025-12-06)

### Added

- **site**: Add Open WebUI client configuration
  ([`2320fa6`](https://github.com/homeassistant-ai/ha-mcp/commit/2320fa68cd445f60aaac7839314314ba034bdcfa))


## v4.17.1 (2025-12-06)

### Fixed

- Regenerate package-lock.json for CI compatibility
  ([`3462d5e`](https://github.com/homeassistant-ai/ha-mcp/commit/3462d5e4b1a8c77c21d84d2cce0791ceed8704bd))


## v4.17.0 (2025-12-06)

### Added

- Add MCP client configuration docs site
  ([#286](https://github.com/homeassistant-ai/ha-mcp/pull/286))


## v4.16.2 (2025-12-06)

### Fixed

- Return helpful error message for YAML script delete attempts
  ([#268](https://github.com/homeassistant-ai/ha-mcp/pull/268))


## v4.16.1 (2025-12-06)

### Fixed

- Filter artifact download to avoid Docker buildx cache
  ([`1757e53`](https://github.com/homeassistant-ai/ha-mcp/commit/1757e537a308d43c02df2cbd12f37a6919d40c1a))


## v4.16.0 (2025-12-06)

### Added

- Implement dual-channel release strategy (dev + stable)
  ([#291](https://github.com/homeassistant-ai/ha-mcp/pull/291))


## v4.15.1 (2025-12-05)

### Fixed

- **macos**: Use full path to uvx in Claude Desktop config
  ([#284](https://github.com/homeassistant-ai/ha-mcp/pull/284))


## v4.15.0 (2025-12-05)

### Added

- Include system info in ha_get_overview response
  ([#283](https://github.com/homeassistant-ai/ha-mcp/pull/283))

### Changed

- Simplify signin and move manual install to step 2
  ([#282](https://github.com/homeassistant-ai/ha-mcp/pull/282))


## v4.14.2 (2025-12-05)

### Fixed

- Write JSON config without UTF-8 BOM on Windows
  ([#281](https://github.com/homeassistant-ai/ha-mcp/pull/281))


## v4.14.1 (2025-12-05)

### Changed

- Improve onboarding UX with demo environment
  ([#265](https://github.com/homeassistant-ai/ha-mcp/pull/265))

### Fixed

- Installer UX improvements
  ([#280](https://github.com/homeassistant-ai/ha-mcp/pull/280))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Update issue-to-pr-resolver agent workflow
  ([`1562ed9`](https://github.com/homeassistant-ai/ha-mcp/commit/1562ed931d1addff051f5b2f7d3314a39b6d1ad7))
</details>


## v4.14.0 (2025-12-05)

### Added

- Enhance ha_get_device with Zigbee integration support (Z2M & ZHA)
  ([#262](https://github.com/homeassistant-ai/ha-mcp/pull/262))


## v4.13.0 (2025-12-05)

### Added

- Add lab setup script with auto-updates and weekly reset
  ([#263](https://github.com/homeassistant-ai/ha-mcp/pull/263))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Add github-issue-analyzer agent with standard comment title
  ([`5211d45`](https://github.com/homeassistant-ai/ha-mcp/commit/5211d4559bdb2a3b242ac69c87bac8a53d4d9421))

### Testing

- Add HACS integration to E2E test environment
  ([#259](https://github.com/homeassistant-ai/ha-mcp/pull/259))
</details>


## v4.12.0 (2025-12-03)

### Added

- Add HACS integration tools for custom component discovery
  ([#250](https://github.com/homeassistant-ai/ha-mcp/pull/250))

### Changed

- Clarify bug description prompt in template
  ([`96b9bc7`](https://github.com/homeassistant-ai/ha-mcp/commit/96b9bc7cc379f568f9b90ea2b46e708aabd276ab))
- Update bug report template to emphasize ha_bug_report tool
  ([`2e72f16`](https://github.com/homeassistant-ai/ha-mcp/commit/2e72f166d5d9b1dcd6693c2258093743585e2b6b))

### Fixed

- Add missing py.typed marker file for type hint distribution
  ([#251](https://github.com/homeassistant-ai/ha-mcp/pull/251))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Add fastmcp to gitignore
  ([`7db3a66`](https://github.com/homeassistant-ai/ha-mcp/commit/7db3a668235d67213338b6bccb49fdf9116daa2a))
</details>


## v4.11.9 (2025-12-03)

### Fixed

- Improve bug report tool with better diagnostics
  ([#256](https://github.com/homeassistant-ai/ha-mcp/pull/256))


## v4.11.8 (2025-12-03)

### Fixed

- Disable VCS release via GitHub Action input
  ([#257](https://github.com/homeassistant-ai/ha-mcp/pull/257))


## v4.11.7 (2025-12-03)

### Fixed

- Correct semantic-release v10 config and add release fallback
  ([#255](https://github.com/homeassistant-ai/ha-mcp/pull/255))


## v4.11.6 (2025-12-03)

### Fixed

- Create GitHub release from build-binary workflow
  ([#254](https://github.com/homeassistant-ai/ha-mcp/pull/254))


## v4.11.5 (2025-12-03)

### Fixed

- Use gh release upload to avoid target_commitish conflict
  ([#252](https://github.com/homeassistant-ai/ha-mcp/pull/252))


## v4.11.4 (2025-12-03)

### Fixed

- Trigger binary builds after SemVer Release via workflow_run
  ([#249](https://github.com/homeassistant-ai/ha-mcp/pull/249))


## v4.11.3 (2025-12-03)

### Refactoring

- Remove MCP prompts feature
  ([#248](https://github.com/homeassistant-ai/ha-mcp/pull/248))


## v4.11.2 (2025-12-02)

### Changed

- Update uvx instructions to use @latest
  ([#241](https://github.com/homeassistant-ai/ha-mcp/pull/241))

### Fixed

- Use correct WebSocket command type for Supervisor API
  ([#246](https://github.com/homeassistant-ai/ha-mcp/pull/246))


## v4.11.1 (2025-12-02)

### Performance Improvements

- Improve startup time with lazy initialization
  ([#237](https://github.com/homeassistant-ai/ha-mcp/pull/237))


## v4.11.0 (2025-12-02)

### Added

- Add diagnostic mode for empty automation traces
  ([#235](https://github.com/homeassistant-ai/ha-mcp/pull/235))


## v4.10.0 (2025-12-02)

### Added

- Add structured error handling with error codes and suggestions
  ([#238](https://github.com/homeassistant-ai/ha-mcp/pull/238))
- Add server icon to FastMCP configuration
  ([#236](https://github.com/homeassistant-ai/ha-mcp/pull/236))
- Add ha_bug_report tool for collecting diagnostic info
  ([#233](https://github.com/homeassistant-ai/ha-mcp/pull/233))
- Add graceful shutdown on SIGTERM/SIGINT signals
  ([#232](https://github.com/homeassistant-ai/ha-mcp/pull/232))
- **search**: Add graceful degradation with fallback for ha_search_entities
  ([#231](https://github.com/homeassistant-ai/ha-mcp/pull/231))

### Fixed

- Improve error handling for missing env variables
  ([#234](https://github.com/homeassistant-ai/ha-mcp/pull/234))


## v4.9.0 (2025-12-02)

### Added

- Add HA_TEST_PORT env var for custom test container port
  ([`4743ee8`](https://github.com/homeassistant-ai/ha-mcp/commit/4743ee82491f8df82308f80d03565bd6de6909b5))


## v4.8.5 (2025-12-01)

### Fixed

- Include resource files in PyPI package distribution
  ([#230](https://github.com/homeassistant-ai/ha-mcp/pull/230))


## v4.8.4 (2025-12-01)

### Fixed

- Resolve entity_id to unique_id for trace lookup
  ([#229](https://github.com/homeassistant-ai/ha-mcp/pull/229))


## v4.8.3 (2025-12-01)

### Fixed

- Add error handling to search tools for better diagnostics
  ([#227](https://github.com/homeassistant-ai/ha-mcp/pull/227))


## v4.8.2 (2025-12-01)

### Fixed

- Fetch Core release notes from GitHub releases API
  ([#228](https://github.com/homeassistant-ai/ha-mcp/pull/228))


## v4.8.1 (2025-12-01)

### Fixed

- Add error handling to ha_deep_search
  ([#226](https://github.com/homeassistant-ai/ha-mcp/pull/226))


## v4.8.0 (2025-12-01)


## v4.7.7 (2025-12-01)

### Fixed

- Normalize automation GET config for round-trip compatibility
  ([#221](https://github.com/homeassistant-ai/ha-mcp/pull/221))


## v4.7.6 (2025-12-01)

### Fixed

- Add boolean coercion for string parameters from XML-style calls
  ([#219](https://github.com/homeassistant-ai/ha-mcp/pull/219))


## v4.7.5 (2025-12-01)

### Added

- Add idempotentHint and title to service tools
  ([`bbd2796`](https://github.com/homeassistant-ai/ha-mcp/commit/bbd2796b4f5a9c02c3e3c23b48ad6ff4af4956db))
- Use light icon (transparent) as main, add 32x32 size
  ([`9ce27c5`](https://github.com/homeassistant-ai/ha-mcp/commit/9ce27c5aa89316099abc4c1bb5de151bc525ac8f))
- **mcpb**: Add tool annotations and update icon
  ([`2d3ea51`](https://github.com/homeassistant-ai/ha-mcp/commit/2d3ea514f71f32c42c2b864324716bd0339859c2))
- Reorganize distribution files and add smoke test
  ([`f720e8a`](https://github.com/homeassistant-ai/ha-mcp/commit/f720e8a21d0172d12502a780484309a8e12e16c4))
- Polish mcpb manifest for submission
  ([`9bd2531`](https://github.com/homeassistant-ai/ha-mcp/commit/9bd25315cd0a71ad9954eacdaaea6c4fef985b8d))
- Auto-generate mcpb manifest with discovered tools
  ([`4af79a0`](https://github.com/homeassistant-ai/ha-mcp/commit/4af79a01808c6e946b0f196f9c97688c3f6add41))
- Add CD workflow with mcpb packaging and GitHub releases
  ([`d3f8e86`](https://github.com/homeassistant-ai/ha-mcp/commit/d3f8e86a0fb6bf745c17abdea866317228c9ba86))
- Add PyInstaller standalone binary builds
  ([`05630dc`](https://github.com/homeassistant-ai/ha-mcp/commit/05630dca6bae8143ca93d1ab4bfee7e52cf16c15))

### Changed

- Write privacy policy to cover future telemetry without updates
  ([`7b0c632`](https://github.com/homeassistant-ai/ha-mcp/commit/7b0c63273f227dc9a01870247875660c2f18a78d))
- Clarify telemetry is not currently implemented
  ([`4d47a47`](https://github.com/homeassistant-ai/ha-mcp/commit/4d47a470d6c61afdbb1957a0d4a63d5276cdc571))
- Make telemetry default behavior neutral
  ([`959f7b9`](https://github.com/homeassistant-ai/ha-mcp/commit/959f7b908e451b10e8c106a596325ec0a02e06dd))
- Fix third-party terminology in privacy policy
  ([`e9abd2d`](https://github.com/homeassistant-ai/ha-mcp/commit/e9abd2de2bce78f308485e988d158ad7004e0972))
- Update privacy policy for future telemetry and MCP client agnostic
  ([`3070140`](https://github.com/homeassistant-ai/ha-mcp/commit/3070140a76fdba2efdb68ad09a0598d95345ca14))
- Adjust privacy policy language to use "might collect"
  ([`3f17068`](https://github.com/homeassistant-ai/ha-mcp/commit/3f1706884252c55283a0da71daebe32ef034c04c))

### Fixed

- Add string coercion for numeric parameters (fixes #205, #206)
  ([#217](https://github.com/homeassistant-ai/ha-mcp/pull/217))
- Query area/entity registries for accurate area count in overview
  ([#216](https://github.com/homeassistant-ai/ha-mcp/pull/216))
- Normalize automation config field names (trigger/triggers)
  ([#215](https://github.com/homeassistant-ai/ha-mcp/pull/215))
- Icon bundle and manifest + add annotation tests
  ([`6d800ef`](https://github.com/homeassistant-ai/ha-mcp/commit/6d800efb275fd1f21a07effa0a2cae871b31dd0e))
- Include all icons in mcpb bundle (dark variants + SVG)
  ([`c22f656`](https://github.com/homeassistant-ai/ha-mcp/commit/c22f65666bd47b097f998a5d235f2b5db346c4d6))
- Add required destructiveHint to all modifying tools
  ([`8881fcf`](https://github.com/homeassistant-ai/ha-mcp/commit/8881fcf4e894470a618019399853e29c40d886e8))
- Include 32x32 icon in mcpb bundle
  ([`681b769`](https://github.com/homeassistant-ai/ha-mcp/commit/681b769c6c047981c5d1d3944349f011ced78878))
- Use platform_overrides for multi-platform mcpb manifest
  ([`61c94c8`](https://github.com/homeassistant-ai/ha-mcp/commit/61c94c89d69a4d702b6c718a5b4f4cea35b83031))
- **mcpb**: Use checkmarks in long_description
  ([`5049d0f`](https://github.com/homeassistant-ai/ha-mcp/commit/5049d0f67b0cb1168b64d1a85508eb2aa639a637))
- **mcpb**: Single line breaks in long_description
  ([`d3d4cc4`](https://github.com/homeassistant-ai/ha-mcp/commit/d3d4cc49a169d0f195ab731dbf1b945217368f3e))
- **mcpb**: Use asterisks for bullet points
  ([`4191145`](https://github.com/homeassistant-ai/ha-mcp/commit/4191145ac90a1e646b120d2cd299be41bd5e6105))
- **mcpb**: Add multiple icon sizes and use tool titles
  ([`5fb1a8b`](https://github.com/homeassistant-ai/ha-mcp/commit/5fb1a8b8209b2eae50c4cdd57b6b26569ba9b846))
- **mcpb**: Use icons array with size specification
  ([`629e863`](https://github.com/homeassistant-ai/ha-mcp/commit/629e863446d8c121daa65f11111c9ec17c5986d9))
- **mcpb**: Fix long_description formatting
  ([`7b62654`](https://github.com/homeassistant-ai/ha-mcp/commit/7b62654d4bf6f0464e84fb47560b7935b627611d))
- **mcpb**: Remove annotations from manifest (not in schema)
  ([`ac2807f`](https://github.com/homeassistant-ai/ha-mcp/commit/ac2807f0168b2e505e970b57cb23a0feb42ed56a))
- Address security scanner warnings and fix privacy policy
  ([`1cba8b6`](https://github.com/homeassistant-ai/ha-mcp/commit/1cba8b6e2f7eebfdaf679a90bd580d0c554736e3))
- Handle Windows encoding in smoke test
  ([`c0b1cca`](https://github.com/homeassistant-ai/ha-mcp/commit/c0b1ccaeb6e1e025b95c59a3546c4cbad5533163))
- Move pyinstaller_hooks to packaging/binary/
  ([`6f8e6d1`](https://github.com/homeassistant-ai/ha-mcp/commit/6f8e6d1aca6018e2b5d3a204c644013867251895))
- Correct PROJECT_ROOT calculation in spec file
  ([`332b388`](https://github.com/homeassistant-ai/ha-mcp/commit/332b38846d24cecc30fe1d1db7e92c1eebd348aa))
- Use absolute paths in PyInstaller spec file
  ([`18d1073`](https://github.com/homeassistant-ai/ha-mcp/commit/18d10737fd083395eff8357a8718ce64bad4ebb3))
- Use UTF-8 encoding in generate_manifest.py for Windows compatibility
  ([`c57d47e`](https://github.com/homeassistant-ai/ha-mcp/commit/c57d47eee1ffea20ad85bc583716b1a283cdc1e3))
- Add runtime hook to register idna codec at startup
  ([`42eb0a6`](https://github.com/homeassistant-ai/ha-mcp/commit/42eb0a66e80970e187bc2c9f911f5aac4dffaad0))
- Add more commonly missing PyInstaller hidden imports
  ([`ca77fa7`](https://github.com/homeassistant-ai/ha-mcp/commit/ca77fa78b29a11fb27bc77f4fc32f101ef91d9f7))
- Add idna codec hidden imports for PyInstaller
  ([`ea84d73`](https://github.com/homeassistant-ai/ha-mcp/commit/ea84d73731403338337bd3b0e10422ddcb07cac3))
- Include click module for uvicorn dependency
  ([`a908a90`](https://github.com/homeassistant-ai/ha-mcp/commit/a908a90f72d8e2cae6d355b9ce1bf3ebbce25d24))
- Add user_config for HA URL and token in mcpb manifest
  ([`f1ca800`](https://github.com/homeassistant-ai/ha-mcp/commit/f1ca800c9487fb392d07b2c06412b40e37f34b05))
- Add explicit permissions block to workflow
  ([`ea4fbc5`](https://github.com/homeassistant-ai/ha-mcp/commit/ea4fbc5b8fbc372bd34e29d8ed260a9b8bca5427))
- Use portable timeout approach for macOS
  ([`2be7a9e`](https://github.com/homeassistant-ai/ha-mcp/commit/2be7a9ef133cbd352b05953daa8cf73e1a911b7c))
- Use Python 3.13 and venv for PyInstaller builds
  ([`3ad28f5`](https://github.com/homeassistant-ai/ha-mcp/commit/3ad28f571cdf50abd6fc93f9fcae4386bcd1c542))

### Refactoring

- Consolidate macOS and Windows into single mcpb bundle
  ([`cde7e36`](https://github.com/homeassistant-ai/ha-mcp/commit/cde7e360b199898ed6c58e37cee43071b78d6570))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Add source SVG icon for future use
  ([`5fa0eea`](https://github.com/homeassistant-ai/ha-mcp/commit/5fa0eea11bfe3b27d2b992f4c0e7e2bde16e4dc9))

### Continuous Integration

- Improve Windows test diagnostics
  ([`3cd3633`](https://github.com/homeassistant-ai/ha-mcp/commit/3cd36333889a6caaa78ef3a13facee7ef48342ae))
</details>


## v4.7.4 (2025-11-29)

### Changed

- Add VS Code one-click install button
  ([#195](https://github.com/homeassistant-ai/ha-mcp/pull/195))

### Fixed

- Handle read-only filesystem in usage logger
  ([#196](https://github.com/homeassistant-ai/ha-mcp/pull/196))


## v4.7.3 (2025-11-29)

### Fixed

- Correct WebSocket URL construction for Supervisor proxy
  ([#193](https://github.com/homeassistant-ai/ha-mcp/pull/193))


## v4.7.2 (2025-11-29)

### Changed

- Add macOS UV setup guide
  ([#191](https://github.com/homeassistant-ai/ha-mcp/pull/191))
- Remove duplicate CONTRIBUTING.md reference
  ([`a57e315`](https://github.com/homeassistant-ai/ha-mcp/commit/a57e315c74fdaf8b8e87c38689f41390baaf8022))
- Reorder installation methods in README
  ([#188](https://github.com/homeassistant-ai/ha-mcp/pull/188))

### Fixed

- Handle None values in update entity attributes
  ([#192](https://github.com/homeassistant-ai/ha-mcp/pull/192))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Add idempotentHint, title, and tags to all tools
  ([#190](https://github.com/homeassistant-ai/ha-mcp/pull/190))
- Add MCP tool annotations for readOnlyHint and destructiveHint
  ([#184](https://github.com/homeassistant-ai/ha-mcp/pull/184))
- Remove obsolete run scripts
  ([`598e397`](https://github.com/homeassistant-ai/ha-mcp/commit/598e3970cc455bcbdc75ffa7ec0c80f9a503ce5f))
</details>


## v4.7.1 (2025-11-28)

### Changed

- Update README and addon docs for new v4.x tools
  ([#178](https://github.com/homeassistant-ai/ha-mcp/pull/178))

### Refactoring

- Auto-discover tool modules to prevent merge conflicts
  ([#183](https://github.com/homeassistant-ai/ha-mcp/pull/183))


## v4.7.0 (2025-11-28)

### Added

- Add historical data access tools (history + statistics)
  ([#176](https://github.com/homeassistant-ai/ha-mcp/pull/176))

### Fixed

- **build**: Include tests package for hamcp-test-env script
  ([#177](https://github.com/homeassistant-ai/ha-mcp/pull/177))


## v4.6.0 (2025-11-28)

### Added

- Add ha_get_camera_image tool to retrieve camera snapshots
  ([#175](https://github.com/homeassistant-ai/ha-mcp/pull/175))


## v4.5.0 (2025-11-28)

### Added

- Add addon management tools (ha_list_addons, ha_list_available_addons)
  ([#174](https://github.com/homeassistant-ai/ha-mcp/pull/174))


## v4.4.0 (2025-11-28)

### Added

- **tools**: Add ZHA device detection and integration source tools
  ([#172](https://github.com/homeassistant-ai/ha-mcp/pull/172))


## v4.3.0 (2025-11-28)

### Added

- Add Group management tools
  ([#171](https://github.com/homeassistant-ai/ha-mcp/pull/171))


## v4.2.0 (2025-11-28)

### Added

- Add ha_get_automation_traces tool for debugging automations
  ([#170](https://github.com/homeassistant-ai/ha-mcp/pull/170))


## v4.1.0 (2025-11-27)

### Added

- **tests**: Pin Home Assistant container version with Renovate tracking
  ([#167](https://github.com/homeassistant-ai/ha-mcp/pull/167))

### Changed

- Update README with all 63 tools
  ([#168](https://github.com/homeassistant-ai/ha-mcp/pull/168))


## v4.0.1 (2025-11-27)

### Fixed

- **search**: Resolve search_types validation and domain_filter issues
  ([#165](https://github.com/homeassistant-ai/ha-mcp/pull/165))


## v4.0.0 (2025-11-27)

### Added

- Major release with 11 new tool modules
  ([#146](https://github.com/homeassistant-ai/ha-mcp/pull/146))

---
<details>
<summary>Internal Changes</summary>


### Build System

- **deps**: Bump astral-sh/uv
  ([#148](https://github.com/homeassistant-ai/ha-mcp/pull/148))
</details>


## v3.7.0 (2025-11-27)

### Added

- **addon**: Add changelog for Home Assistant add-on updates
  ([#119](https://github.com/homeassistant-ai/ha-mcp/pull/119))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- **deps**: Switch dependabot from pip to uv ecosystem
  ([#147](https://github.com/homeassistant-ai/ha-mcp/pull/147))
</details>


## v3.6.2 (2025-11-26)

### Changed

- **tools**: Recommend description field for automations
  ([#111](https://github.com/homeassistant-ai/ha-mcp/pull/111))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- **ci**: Add explicit permissions to prepare job
  ([#117](https://github.com/homeassistant-ai/ha-mcp/pull/117))

### Chores

- Remove CHANGELOG.md
  ([#89](https://github.com/homeassistant-ai/ha-mcp/pull/89))

### Continuous Integration

- **deps**: Bump actions/checkout from 5 to 6
  ([#90](https://github.com/homeassistant-ai/ha-mcp/pull/90))
</details>


## v3.6.1 (2025-11-25)

### Fixed

- **docs**: Add missing --transport flag for mcp-proxy in add-on docs
  ([#94](https://github.com/homeassistant-ai/ha-mcp/pull/94))

---
<details>
<summary>Internal Changes</summary>


### Build System

- **deps**: Bump astral-sh/uv
  ([#92](https://github.com/homeassistant-ai/ha-mcp/pull/92))

### Continuous Integration

- **deps**: Bump renovatebot/github-action from 44.0.3 to 44.0.4
  ([#91](https://github.com/homeassistant-ai/ha-mcp/pull/91))
</details>


## v3.6.0 (2025-11-23)

### Added

- Python 3.13 only with automated Renovate upgrades
  ([#88](https://github.com/homeassistant-ai/ha-mcp/pull/88))


## v3.5.1 (2025-11-18)

### Changed

- Update dashboard guide with modern best practices
  ([#81](https://github.com/homeassistant-ai/ha-mcp/pull/81))

### Fixed

- Improve test isolation in test_deep_search_no_results
  ([#80](https://github.com/homeassistant-ai/ha-mcp/pull/80))


## v3.5.0 (2025-11-17)

### Added

- Add dashboard management tools for Lovelace UI
  ([#75](https://github.com/homeassistant-ai/ha-mcp/pull/75))

### Changed

- Remove Code Refactoring Patterns section from AGENTS.md
  ([`f4612c9`](https://github.com/homeassistant-ai/ha-mcp/commit/f4612c9477f67b50d76b091e740383d816a1981f))
- Update AGENTS.md to reflect registry refactoring architecture
  ([`97111a5`](https://github.com/homeassistant-ai/ha-mcp/commit/97111a59c00537abf38c13fe86e2d38905d04d7a))

---
<details>
<summary>Internal Changes</summary>


### Build System

- **deps**: Bump astral-sh/uv
  ([#77](https://github.com/homeassistant-ai/ha-mcp/pull/77))
- **deps**: Bump astral-sh/uv
  ([#66](https://github.com/homeassistant-ai/ha-mcp/pull/66))

### Continuous Integration

- **deps**: Bump python-semantic-release/python-semantic-release
  ([#78](https://github.com/homeassistant-ai/ha-mcp/pull/78))
- **deps**: Bump python-semantic-release/python-semantic-release
  ([#65](https://github.com/homeassistant-ai/ha-mcp/pull/65))
</details>


## v3.4.3 (2025-11-09)

### Fixed

- Align release workflow and server manifest
  ([#64](https://github.com/homeassistant-ai/ha-mcp/pull/64))


## v3.4.2 (2025-11-09)

### Fixed

- Validate server manifest via script
  ([#63](https://github.com/homeassistant-ai/ha-mcp/pull/63))


## v3.4.1 (2025-11-09)

### Fixed

- Correct release workflow indentation
  ([#62](https://github.com/homeassistant-ai/ha-mcp/pull/62))

---
<details>
<summary>Internal Changes</summary>


### Continuous Integration

- Automate MCP registry publishing
  ([#61](https://github.com/homeassistant-ai/ha-mcp/pull/61))
</details>


## v3.4.0 (2025-11-07)

### Added

- Add SSE FastMCP deployment config
  ([#60](https://github.com/homeassistant-ai/ha-mcp/pull/60))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Disable autofix workflow
  ([#59](https://github.com/homeassistant-ai/ha-mcp/pull/59))
</details>


## v3.3.2 (2025-11-07)

### Fixed

- Repair codex autofix workflow conditions
  ([#58](https://github.com/homeassistant-ai/ha-mcp/pull/58))


## v3.3.1 (2025-11-07)

### Changed

- Simplifies the installation instructions
  ([`fd8f68d`](https://github.com/homeassistant-ai/ha-mcp/commit/fd8f68db0f5cafcb7ad6d6c6b8b00440822c44a7))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- **ci**: Gate autofix workflow via mode
  ([#57](https://github.com/homeassistant-ai/ha-mcp/pull/57))

### Chores

- Disable codex autofix workflow
  ([#55](https://github.com/homeassistant-ai/ha-mcp/pull/55))
</details>


## v3.3.0 (2025-11-06)

### Added

- Add pypi publish
  ([`bd6d358`](https://github.com/homeassistant-ai/ha-mcp/commit/bd6d358b46212f0102292b56751d9f3f037e673c))

### Changed

- Clarify agent guidance on e2e requirements
  ([#53](https://github.com/homeassistant-ai/ha-mcp/pull/53))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Deduplicate dev dependencies
  ([#43](https://github.com/homeassistant-ai/ha-mcp/pull/43))
- **ci**: Add workflow to close inactive issues
  ([#45](https://github.com/homeassistant-ai/ha-mcp/pull/45))

### Continuous Integration

- **deps**: Bump peter-evans/create-pull-request from 6 to 7
  ([#49](https://github.com/homeassistant-ai/ha-mcp/pull/49))
- Streamline codex autofix actions
  ([#47](https://github.com/homeassistant-ai/ha-mcp/pull/47))
</details>


## v3.2.3 (2025-10-25)

### Fixed

- Try multiple codex models per step
  ([#42](https://github.com/homeassistant-ai/ha-mcp/pull/42))


## v3.2.2 (2025-10-24)

---
<details>
<summary>Internal Changes</summary>


### Fixed

- **ci**: Streamline codex autofix credentials
  ([#40](https://github.com/homeassistant-ai/ha-mcp/pull/40))
</details>


## v3.2.1 (2025-10-23)

### Fixed

- Retain textdistance version constraints
  ([#39](https://github.com/homeassistant-ai/ha-mcp/pull/39))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Use base textdistance dependency
  ([#38](https://github.com/homeassistant-ai/ha-mcp/pull/38))
</details>


## v3.2.0 (2025-10-23)

### Added

- Migrate fuzzy search to textdistance
  ([#36](https://github.com/homeassistant-ai/ha-mcp/pull/36))

### Changed

- Add Windows UV guide and reorganize assets
  ([#34](https://github.com/homeassistant-ai/ha-mcp/pull/34))


## v3.1.6 (2025-10-21)

### Fixed

- Align add-on schema with HA Supervisor
  ([#33](https://github.com/homeassistant-ai/ha-mcp/pull/33))

---
<details>
<summary>Internal Changes</summary>


### Build System

- **deps**: Bump astral-sh/uv
  ([#27](https://github.com/homeassistant-ai/ha-mcp/pull/27))
</details>


## v3.1.5 (2025-10-20)

### Refactoring

- Remove redundant static docs
  ([#26](https://github.com/homeassistant-ai/ha-mcp/pull/26))


## v3.1.4 (2025-10-20)

### Refactoring

- Drop duplicate convenience tools
  ([#25](https://github.com/homeassistant-ai/ha-mcp/pull/25))


## v3.1.3 (2025-10-18)

### Fixed

- Ha_deep_search docs
  ([#23](https://github.com/homeassistant-ai/ha-mcp/pull/23))


## v3.1.2 (2025-10-18)

### Fixed

- Return subscription id from WebSocket result
  ([#22](https://github.com/homeassistant-ai/ha-mcp/pull/22))


## v3.1.1 (2025-10-18)

### Changed

- Add ha_deep_search tool to documentation
  ([#20](https://github.com/homeassistant-ai/ha-mcp/pull/20))

### Refactoring

- Split registry.py into focused modules (2106 → 76 lines)
  ([#21](https://github.com/homeassistant-ai/ha-mcp/pull/21))


## v3.1.0 (2025-10-17)

### Added

- Add ha_deep_search tool for searching automation/script/helper configs
  ([#19](https://github.com/homeassistant-ai/ha-mcp/pull/19))


## v3.0.1 (2025-10-17)

### Fixed

- Correct logbook API endpoint format (Issue #16)
  ([#18](https://github.com/homeassistant-ai/ha-mcp/pull/18))

---
<details>
<summary>Internal Changes</summary>


### Build System

- **deps**: Bump astral-sh/uv
  ([#17](https://github.com/homeassistant-ai/ha-mcp/pull/17))
</details>


## v3.0.0 (2025-10-17)

### Changed

- Finalize Docker and addon documentation with tested syntax
  ([#15](https://github.com/homeassistant-ai/ha-mcp/pull/15))


## v2.5.7 (2025-10-10)

### Fixed

- Make addon build wait for semantic-release to complete
  ([`2ae666a`](https://github.com/homeassistant-ai/ha-mcp/commit/2ae666a4468370c39c1b7bf25b6dfb34db7ee897))


## v2.5.6 (2025-10-10)

### Fixed

- Add git add to build_command to include config.yaml in version commits
  ([`0d50f24`](https://github.com/homeassistant-ai/ha-mcp/commit/0d50f24b95ae132efa53342a65236c43ebac92f8))


## v2.5.5 (2025-10-10)

### Fixed

- Use semantic-release build_command to sync addon version in same commit
  ([`c725aaa`](https://github.com/homeassistant-ai/ha-mcp/commit/c725aaa9d143d6a0e26b65a485be36d2eda83886))
- Use direct mcp.run() instead of os.execvp with debug output
  ([`91b698b`](https://github.com/homeassistant-ai/ha-mcp/commit/91b698bba474e5ff344e038e535775c19fcdf4b8))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Sync addon version to 2.5.4
  ([`0055dda`](https://github.com/homeassistant-ai/ha-mcp/commit/0055ddab181292cf525f83e9dc845943cf1539a2))
- Configure semantic-release to update addon config.yaml version
  ([`ff65337`](https://github.com/homeassistant-ai/ha-mcp/commit/ff6533768b931f1853c8c2af37957cb01643a60b))
- Sync addon version with package semver and fix slug
  ([`8b90a76`](https://github.com/homeassistant-ai/ha-mcp/commit/8b90a766908b5c709135cd991ee49b314a35f4f8))

### Testing

- Update addon startup tests for direct mcp.run() approach
  ([`1d7ee6b`](https://github.com/homeassistant-ai/ha-mcp/commit/1d7ee6b47ea27141778ab2a254b772a68855415c))
</details>


## v2.5.4 (2025-10-10)

### Fixed

- Enable host network mode for local network access
  ([`b991ddf`](https://github.com/homeassistant-ai/ha-mcp/commit/b991ddf100f458a7c5a1d6a3997ced7e8ba2c9fb))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Update uv.lock
  ([`80842ab`](https://github.com/homeassistant-ai/ha-mcp/commit/80842abc6e9b3ab3c6892456302c96a08b52936c))

### Testing

- Add integration tests for addon container startup
  ([`1881075`](https://github.com/homeassistant-ai/ha-mcp/commit/1881075874c38869df687b2e8e26f68262537240))
</details>


## v2.5.3 (2025-10-10)

### Fixed

- Specify ha_mcp module in fastmcp run command
  ([`22ddb0b`](https://github.com/homeassistant-ai/ha-mcp/commit/22ddb0b75bd07048fb10bd394903ea18a130e20a))


## v2.5.2 (2025-10-10)

### Fixed

- Correct COPY paths in Dockerfile for project root context
  ([`bcb6568`](https://github.com/homeassistant-ai/ha-mcp/commit/bcb6568d57c82815b4ec23227cd1abce15577ef2))


## v2.5.1 (2025-10-10)

### Fixed

- Use Debian-based uv image instead of non-existent Alpine variant
  ([`3e94860`](https://github.com/homeassistant-ai/ha-mcp/commit/3e94860c51916e5d8b84a7a62d328122b88380b7))


## v2.5.0 (2025-10-10)

### Added

- Add HA token authentication for add-on
  ([#14](https://github.com/homeassistant-ai/ha-mcp/pull/14))


## v2.4.0 (2025-10-10)

### Added

- Add-on pre-built images with HTTP transport
  ([#13](https://github.com/homeassistant-ai/ha-mcp/pull/13))

---
<details>
<summary>Internal Changes</summary>


### Continuous Integration

- **deps**: Bump astral-sh/setup-uv from 6 to 7
  ([#11](https://github.com/homeassistant-ai/ha-mcp/pull/11))
</details>


## v2.3.2 (2025-10-09)

### Changed

- Document repository.yaml requirement in AGENTS.md
  ([`7dfd746`](https://github.com/homeassistant-ai/ha-mcp/commit/7dfd746df27c033a8dae3c0593da287ba1c1327a))
- Revert README to simple installation instructions
  ([`2f501cf`](https://github.com/homeassistant-ai/ha-mcp/commit/2f501cf31c34e0ccbbb5870e5be79ddd1732c4d5))

### Fixed

- Add repository.yaml for HA add-on repository identification
  ([`c57e433`](https://github.com/homeassistant-ai/ha-mcp/commit/c57e43384992880393b50416774ebc9f3b60d3ef))

---
<details>
<summary>Internal Changes</summary>


### Testing

- Add repository.yaml validation tests
  ([`dc0e0df`](https://github.com/homeassistant-ai/ha-mcp/commit/dc0e0df9621ad0006c1c2241a7fa51bc82ad06f4))
</details>


## v2.3.1 (2025-10-09)

### Fixed

- Limit platforms to 64-bit (amd64/arm64) supported by uv image
  ([#12](https://github.com/homeassistant-ai/ha-mcp/pull/12))


## v2.3.0 (2025-10-09)

### Added

- Docker deployment and Home Assistant add-on support
  ([#10](https://github.com/homeassistant-ai/ha-mcp/pull/10))

### Changed

- Clarify YouTube link is same demo
  ([`cc3527c`](https://github.com/homeassistant-ai/ha-mcp/commit/cc3527c367ae36cdf01fec599a9c3a1c09eedcd3))
- Add YouTube demo link
  ([`f189df9`](https://github.com/homeassistant-ai/ha-mcp/commit/f189df9f06c8cecb068969c08c8175b0e8dd7170))
- Move logo to img directory
  ([`19e8394`](https://github.com/homeassistant-ai/ha-mcp/commit/19e83947d440952653629af981b1390b7cd18e74))
- Add demo animation to README
  ([`8670474`](https://github.com/homeassistant-ai/ha-mcp/commit/86704745669af8e8ef78117f0c2edccb1dd477a9))
- Add demo animation to README
  ([`8d0c574`](https://github.com/homeassistant-ai/ha-mcp/commit/8d0c574f21a940d49b6d1aa9eb7950ca7fe5b5b8))


## v2.2.0 (2025-10-05)

### Added

- Add backup creation and restore tools
  ([#9](https://github.com/homeassistant-ai/ha-mcp/pull/9))


## v2.1.0 (2025-10-02)

### Added

- Add detail_level parameter to ha_get_overview with 4 levels
  ([#8](https://github.com/homeassistant-ai/ha-mcp/pull/8))

### Changed

- Add Claude Code acknowledgment and remove footer tagline
  ([`291ce86`](https://github.com/homeassistant-ai/ha-mcp/commit/291ce86c8302dd8c532b0c39125adb7eb7cfa721))


## v2.0.0 (2025-10-02)

### Added

- Rename package and repository to ha-mcp
  ([#7](https://github.com/homeassistant-ai/ha-mcp/pull/7))

### Changed

- Remove non-reusable package rename documentation from AGENTS.md
  ([`d0602ba`](https://github.com/homeassistant-ai/ha-mcp/commit/d0602ba800195063ee1f8f9ab85a9983bc154920))
- Add lessons learned from ha_config_* refactoring to AGENTS.md
  ([`25a8f66`](https://github.com/homeassistant-ai/ha-mcp/commit/25a8f66dd3a5c1861fc7f756ba603ac4cb8b67c1))


## v1.0.3 (2025-10-01)

### Changed

- Fix typos and formatting in README
  ([`ebfa004`](https://github.com/homeassistant-ai/ha-mcp/commit/ebfa004f76143c3c53735bc1834ee17539980e4d))

### Refactoring

- Split ha_manage_* into ha_config_{get,set,remove}_* tools
  ([#6](https://github.com/homeassistant-ai/ha-mcp/pull/6))


## v1.0.2 (2025-09-19)

### Fixed

- Resolve GitHub Action semantic-release configuration issues
  ([#3](https://github.com/homeassistant-ai/ha-mcp/pull/3))
- Documentation formatting and accuracy improvements
  ([#2](https://github.com/homeassistant-ai/ha-mcp/pull/2))

---
<details>
<summary>Internal Changes</summary>


### Continuous Integration

- **deps**: Bump python-semantic-release/python-semantic-release
  ([`a09cd92`](https://github.com/homeassistant-ai/ha-mcp/commit/a09cd929fc1dd8f2991eace3af8892af0b1b6367))
</details>


## v1.0.1 (2025-09-18)

### Fixed

- Remove Docker ecosystem from dependabot config
  ([`b393282`](https://github.com/homeassistant-ai/ha-mcp/commit/b393282f7e5774ea706f364b27ff522e4af800a8))


## v1.0.0 (2025-09-18)
