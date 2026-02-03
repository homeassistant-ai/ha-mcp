# CHANGELOG

<!-- version list -->


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
