# CHANGELOG

<!-- version list -->


## v9.0.0 (2026-10-28)

### Added

- Store snapshot backups where Home Assistant stores its own
  ([#2822](https://github.com/homeassistant-ai/ha-mcp/pull/2822))
- Redact operator-listed app option keys under redact_secrets
  ([#2849](https://github.com/homeassistant-ai/ha-mcp/pull/2849))
- **component**: Access switch to grant and revoke access to the in-process server
  ([#2821](https://github.com/homeassistant-ai/ha-mcp/pull/2821))
- Install from the main repo in HACS, pre-release notes, optional callback allowlist
  ([#2784](https://github.com/homeassistant-ai/ha-mcp/pull/2784))
- **component**: Add Download diagnostics for the custom component
  ([#2759](https://github.com/homeassistant-ai/ha-mcp/pull/2759))
- **logs**: Migrate ha_get_logs onto native Core and Supervisor paths
  ([#2770](https://github.com/homeassistant-ai/ha-mcp/pull/2770))
- **component**: Run the in-process server with an administrator's own token (#2427)
  ([#2742](https://github.com/homeassistant-ai/ha-mcp/pull/2742))
- **component**: None-mode OAuth redirects only to allowlisted callbacks (#2427)
  ([#2741](https://github.com/homeassistant-ai/ha-mcp/pull/2741))
- **component**: Run the server each component release pins instead of self-updating it (#2427)
  ([#2740](https://github.com/homeassistant-ai/ha-mcp/pull/2740))
- **component**: Locked-down defaults for new server entries, no secrets in the log (#2427)
  ([#2739](https://github.com/homeassistant-ai/ha-mcp/pull/2739))
- Add a per-state summary mode to ha_get_history
  ([#2760](https://github.com/homeassistant-ai/ha-mcp/pull/2760))
- Add custom_instructions setting for owner-defined server instructions
  ([#2734](https://github.com/homeassistant-ai/ha-mcp/pull/2734))

### Changed

- **component**: Point legacy OAuth hint at the secret webhook URL for connection testing
  ([#2894](https://github.com/homeassistant-ai/ha-mcp/pull/2894))
- **updates**: State that install blocks and may time out or drop the connection
  ([#2762](https://github.com/homeassistant-ai/ha-mcp/pull/2762))

### Fixed

- Restore entity backups to their registry settings and check what restores bring back
  ([#2846](https://github.com/homeassistant-ai/ha-mcp/pull/2846))
- **visibility**: Conceal hidden scripts, scenes and automations addressed by bare key or config id
  ([#2870](https://github.com/homeassistant-ai/ha-mcp/pull/2870))
- **backup**: Preserve final newline differences in text diffs
  ([#2847](https://github.com/homeassistant-ai/ha-mcp/pull/2847))
- **logs**: Return whole records for raw error_log reads (#2768)
  ([#2813](https://github.com/homeassistant-ai/ha-mcp/pull/2813))
- **restart**: Refuse during a database upgrade on embedded; add safe_mode
  ([#2842](https://github.com/homeassistant-ai/ha-mcp/pull/2842))
- Keep a worker stopped during its cold import from binding the port
  ([#2845](https://github.com/homeassistant-ai/ha-mcp/pull/2845))
- Log the expected disconnect after a deferred embedded restart as one line
  ([#2838](https://github.com/homeassistant-ai/ha-mcp/pull/2838))
- Let Home Assistant validate group writes and report them as Core sees them
  ([#2824](https://github.com/homeassistant-ai/ha-mcp/pull/2824))
- **auth**: Bind the consent form's token to its own authorization code
  ([#2837](https://github.com/homeassistant-ai/ha-mcp/pull/2837))
- Steer agents to ha_get_skill_guide whenever it is served
  ([#2805](https://github.com/homeassistant-ai/ha-mcp/pull/2805))
- **apps**: Clarify connection failures and ESPHome MCP access
  ([#2803](https://github.com/homeassistant-ai/ha-mcp/pull/2803))
- Accept minutes in ha_get_history relative times
  ([#2753](https://github.com/homeassistant-ai/ha-mcp/pull/2753))
- Show a zone's registry icon in the ha_set_zone update reply
  ([#2757](https://github.com/homeassistant-ai/ha-mcp/pull/2757))
- Drop handwritten validity claims from the dashboard tools
  ([#2737](https://github.com/homeassistant-ai/ha-mcp/pull/2737))
- Route zone writes through the helper path and find zones in the registry
  ([#2707](https://github.com/homeassistant-ai/ha-mcp/pull/2707))
- **integrations**: Refuse ha_mcp_tools config entries in the generic integration tools
  ([#2749](https://github.com/homeassistant-ai/ha-mcp/pull/2749))
- Clarify that ha_config_set_yaml add merges one level deep
  ([#2751](https://github.com/homeassistant-ai/ha-mcp/pull/2751))
- Fall back to Docker Hub for the python base image in CI
  ([#2738](https://github.com/homeassistant-ai/ha-mcp/pull/2738))
- Don't report button-card styles.card as a misshapen card slot
  ([#2735](https://github.com/homeassistant-ai/ha-mcp/pull/2735))

---
<details>
<summary>Internal Changes</summary>


### Changed

- Make the component path the default in the native contract
  ([#2871](https://github.com/homeassistant-ai/ha-mcp/pull/2871))
- **tests**: Name the registry leftover a deleted storage item leaves
  ([#2756](https://github.com/homeassistant-ai/ha-mcp/pull/2756))

### Build System

- **deps-dev**: Bump fast-uri from 3.1.7 to 3.1.9 in /site
  ([#2817](https://github.com/homeassistant-ai/ha-mcp/pull/2817))
- **deps**: Bump undici from 8.10.2 to 8.12.3 in /site
  ([#2816](https://github.com/homeassistant-ai/ha-mcp/pull/2816))
- **deps**: Bump astro from 7.2.8 to 7.3.3 in /site
  ([#2811](https://github.com/homeassistant-ai/ha-mcp/pull/2811))

### Chores

- Sync ratchet baselines after merge [skip ci]
  ([`73a6ed9`](https://github.com/homeassistant-ai/ha-mcp/commit/73a6ed96ad78d395fca36078f178cbf99c5e2a50))
- Sync tool docs after merge [skip ci]
  ([`34c0543`](https://github.com/homeassistant-ai/ha-mcp/commit/34c0543ab0e8189191e86c7ec2a4772cc66b1235))
- Machine-translate locale updates
  ([`9c4fd66`](https://github.com/homeassistant-ai/ha-mcp/commit/9c4fd6607c2b79973e25759a527f5764e0d6c2b2))
- **deps**: Bump postcss-selector-parser in /site
  ([#2887](https://github.com/homeassistant-ai/ha-mcp/pull/2887))
- **deps**: Update dependency renovatebot/renovate to v44.172.1
  ([#2886](https://github.com/homeassistant-ai/ha-mcp/pull/2886))
- **deps**: Update uv
  ([#2885](https://github.com/homeassistant-ai/ha-mcp/pull/2885))
- Sync ratchet baselines after merge [skip ci]
  ([`4cce756`](https://github.com/homeassistant-ai/ha-mcp/commit/4cce756f484553c3577fc93d4c2812376d0c18af))
- Sync tool docs after merge [skip ci]
  ([`7dfd2d5`](https://github.com/homeassistant-ai/ha-mcp/commit/7dfd2d511d6ad679cce99a21878ac4fbac930c4b))
- **deps**: Update dependency home-assistant/operating-system to v18.4
  ([#2880](https://github.com/homeassistant-ai/ha-mcp/pull/2880))
- **deps**: Update public.ecr.aws/docker/library/python:3.13-slim docker digest to b3575dc
  ([#2878](https://github.com/homeassistant-ai/ha-mcp/pull/2878))
- **deps**: Update src/ha_mcp/resources/skills-vendor digest to 6d7fc78
  ([#2879](https://github.com/homeassistant-ai/ha-mcp/pull/2879))
- Sync ratchet baselines after merge [skip ci]
  ([`372d77e`](https://github.com/homeassistant-ai/ha-mcp/commit/372d77ebbd68a2c3a434ccf679b8fe3902808a6c))
- **deps**: Update dependency home-assistant/supervisor to v2026.10.4
  ([#2872](https://github.com/homeassistant-ai/ha-mcp/pull/2872))
- Machine-translate locale updates
  ([`86c5b4d`](https://github.com/homeassistant-ai/ha-mcp/commit/86c5b4d7fbad8cef32764bbca174e995c4803b30))
- Sync ratchet baselines after merge [skip ci]
  ([`0ff032e`](https://github.com/homeassistant-ai/ha-mcp/commit/0ff032eb519c7fc8c84a968d01ee77fc946c4d5e))
- Sync tool docs after merge [skip ci]
  ([`3690af6`](https://github.com/homeassistant-ai/ha-mcp/commit/3690af6e0f62ccec6c7fa70f748494ba2605f419))
- Sync ratchet baselines after merge [skip ci]
  ([`4a03740`](https://github.com/homeassistant-ai/ha-mcp/commit/4a03740e7a042f244420e38d3536b603bbe9c733))
- Sync ratchet baselines after merge [skip ci]
  ([`ed418c5`](https://github.com/homeassistant-ai/ha-mcp/commit/ed418c51ead3626eeac05f4056c345375bceb280))
- Sync tool docs after merge [skip ci]
  ([`b10fcbd`](https://github.com/homeassistant-ai/ha-mcp/commit/b10fcbdd55d3dfabaeae49b2d141ae3c26ce3149))
- Sync ratchet baselines after merge [skip ci]
  ([`3317791`](https://github.com/homeassistant-ai/ha-mcp/commit/33177916d509ff9b474c51d6eaa2ffaaf189cc90))
- Machine-translate locale updates
  ([`eee3a1e`](https://github.com/homeassistant-ai/ha-mcp/commit/eee3a1eed4e1cc5d0e858d424513bbfa66c74376))
- Sync ratchet baselines after merge [skip ci]
  ([`40c14c4`](https://github.com/homeassistant-ai/ha-mcp/commit/40c14c4a3433b3b47b6bb45de1d33db6bff327bf))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.10.4
  ([#2840](https://github.com/homeassistant-ai/ha-mcp/pull/2840))
- Sync tool docs after merge [skip ci]
  ([`bbde06b`](https://github.com/homeassistant-ai/ha-mcp/commit/bbde06bc44bf1b96c677c83334053f8afd1978cd))
- Sync ratchet baselines after merge [skip ci]
  ([`fad70b7`](https://github.com/homeassistant-ai/ha-mcp/commit/fad70b76d0f0da5f622e21b78b76795820c7f6e8))
- Sync ratchet baselines after merge [skip ci]
  ([`4874641`](https://github.com/homeassistant-ai/ha-mcp/commit/487464180578802c9f69dd813f461dd44622e0d0))
- **deps**: Update dependency fastmcp-slim to v4.1.2 [security]
  ([#2818](https://github.com/homeassistant-ai/ha-mcp/pull/2818))
- **deps**: Raise openapi-pydantic floor and keep Dependabot off vendored pins
  ([#2826](https://github.com/homeassistant-ai/ha-mcp/pull/2826))
- Machine-translate locale updates
  ([`83f86f7`](https://github.com/homeassistant-ai/ha-mcp/commit/83f86f7e31fefa29dbb340a5eb4055de88473d5b))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.10.3
  ([#2814](https://github.com/homeassistant-ai/ha-mcp/pull/2814))
- Sync ratchet baselines after merge [skip ci]
  ([`f0338c4`](https://github.com/homeassistant-ai/ha-mcp/commit/f0338c48be12d0a52bbffe66e6c73cd0526cbcb7))
- Sync ratchet baselines after merge [skip ci]
  ([`42d014b`](https://github.com/homeassistant-ai/ha-mcp/commit/42d014bdfaa0a775b251794108166d562e42598e))
- Sync ratchet baselines after merge [skip ci]
  ([`9c91e6e`](https://github.com/homeassistant-ai/ha-mcp/commit/9c91e6ed7759b13e19aed39c073a81465d996eb5))
- Sync tool docs after merge [skip ci]
  ([`de2d5f3`](https://github.com/homeassistant-ai/ha-mcp/commit/de2d5f360b11993f35dea5fa3129c839d3866885))
- **deps**: Update dependency home-assistant/supervisor to v2026.10.3
  ([#2804](https://github.com/homeassistant-ai/ha-mcp/pull/2804))
- **deps**: Update uv
  ([#2799](https://github.com/homeassistant-ai/ha-mcp/pull/2799))
- **deps**: Update dependency renovatebot/renovate to v44.155.9
  ([#2798](https://github.com/homeassistant-ai/ha-mcp/pull/2798))
- **deps**: Update dependency websockets to v17.2
  ([#2792](https://github.com/homeassistant-ai/ha-mcp/pull/2792))
- **deps**: Update dependency renovatebot/renovate to v44.155.7
  ([#2791](https://github.com/homeassistant-ai/ha-mcp/pull/2791))
- **deps**: Update dependency fastmcp-slim to v4.1.0
  ([#2790](https://github.com/homeassistant-ai/ha-mcp/pull/2790))
- **deps**: Update src/ha_mcp/resources/skills-vendor digest to 68f2690
  ([#2789](https://github.com/homeassistant-ai/ha-mcp/pull/2789))
- Machine-translate locale updates
  ([`625bd36`](https://github.com/homeassistant-ai/ha-mcp/commit/625bd36ac099b418612a0de88f758d9c1090865e))
- Sync ratchet baselines after merge [skip ci]
  ([`78b8147`](https://github.com/homeassistant-ai/ha-mcp/commit/78b8147d683a7af49920ce025b066a64e3581d5e))
- Sync tool docs after merge [skip ci]
  ([`4be1508`](https://github.com/homeassistant-ai/ha-mcp/commit/4be15085c89b73dce04082f7304737783b72543d))
- Sync ratchet baselines after merge [skip ci]
  ([`47b40ae`](https://github.com/homeassistant-ai/ha-mcp/commit/47b40ae4d532d24add67b9c8325af5a416c033b0))
- Sync tool docs after merge [skip ci]
  ([`c4c2704`](https://github.com/homeassistant-ai/ha-mcp/commit/c4c27041f3dc39df091bdea5a9b47235bf1b7ca6))
- Sync ratchet baselines after merge [skip ci]
  ([`980daa9`](https://github.com/homeassistant-ai/ha-mcp/commit/980daa944956a6abe2a81b0db04a3810e2c22a9b))
- Sync ratchet baselines after merge [skip ci]
  ([`fc2369e`](https://github.com/homeassistant-ai/ha-mcp/commit/fc2369ebbd2db9a5ce39ef129d2ee87b652f09e4))
- Sync tool docs after merge [skip ci]
  ([`4937e64`](https://github.com/homeassistant-ai/ha-mcp/commit/4937e6438aad36274c5521b7a749a25c1e9628ff))
- Sync tool docs after merge [skip ci]
  ([`c05bfff`](https://github.com/homeassistant-ai/ha-mcp/commit/c05bfffa263f138b2ef0888ec4c15689675f6056))
- Sync tool docs after merge [skip ci]
  ([`1484980`](https://github.com/homeassistant-ai/ha-mcp/commit/14849806db193158bd1c5aa38ea8292ba2e857e3))
- Sync ratchet baselines after merge [skip ci]
  ([`572fb54`](https://github.com/homeassistant-ai/ha-mcp/commit/572fb5448fdf2b2dbe41914403574fa53e5bb264))
- Sync tool docs after merge [skip ci]
  ([`dc36baa`](https://github.com/homeassistant-ai/ha-mcp/commit/dc36baac0ab24e11670d938a88c67d3f64c1905d))
- Sync tool docs after merge [skip ci]
  ([`d3ecebc`](https://github.com/homeassistant-ai/ha-mcp/commit/d3ecebcba49229bb6ba0195525c4990ad95df75e))
- Sync tool docs after merge [skip ci]
  ([`6789062`](https://github.com/homeassistant-ai/ha-mcp/commit/6789062e59f1a4f40d0748fbe47ed5011979f962))
- Sync ratchet baselines after merge [skip ci]
  ([`7e38013`](https://github.com/homeassistant-ai/ha-mcp/commit/7e3801346957763faff68ff5450060f4d006b938))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.10.2
  ([#2755](https://github.com/homeassistant-ai/ha-mcp/pull/2755))
- Sync tool docs after merge [skip ci]
  ([`c50c4cb`](https://github.com/homeassistant-ai/ha-mcp/commit/c50c4cbb4b619f6c4908a22b289c38111ecac46a))
- **deps**: Update dependency home-assistant/supervisor to v2026.10.2
  ([#2748](https://github.com/homeassistant-ai/ha-mcp/pull/2748))
- Machine-translate locale updates
  ([`1580a6c`](https://github.com/homeassistant-ai/ha-mcp/commit/1580a6c18ae811db2bad322c75c33e54a83a2b04))
- Sync ratchet baselines after merge [skip ci]
  ([`285da7c`](https://github.com/homeassistant-ai/ha-mcp/commit/285da7c36b04a3030f6899cc2c982c6fa06777a8))
- Sync ratchet baselines after merge [skip ci]
  ([`6b0dd5a`](https://github.com/homeassistant-ai/ha-mcp/commit/6b0dd5a8542fac89f54dd9bbf6b38e801847d12d))
- **deps**: Update vendored fastmcp
  ([#2731](https://github.com/homeassistant-ai/ha-mcp/pull/2731))
- **addon**: Publish version 8.7.0 [skip ci]
  ([`051e45f`](https://github.com/homeassistant-ai/ha-mcp/commit/051e45fab480967dc9bf79dd333106390b588537))

### Continuous Integration

- Pull the python base image from a GHCR mirror before the rate-limited registries
  ([#2895](https://github.com/homeassistant-ai/ha-mcp/pull/2895))
- **mirror**: Keep the integration mirror's stable release in HACS's reach
  ([#2883](https://github.com/homeassistant-ai/ha-mcp/pull/2883))
- **deps**: Bump renovatebot/github-action
  ([#2868](https://github.com/homeassistant-ai/ha-mcp/pull/2868))
- **intake**: Read reports and answers as rendered and keep the sweep running
  ([#2788](https://github.com/homeassistant-ai/ha-mcp/pull/2788))
- **site**: Build the docs site from the stable tag instead of master
  ([#2808](https://github.com/homeassistant-ai/ha-mcp/pull/2808))
- **deps**: Bump vite from 8.1.5 to 8.3.4 in /site
  ([#2793](https://github.com/homeassistant-ai/ha-mcp/pull/2793))
- **deps**: Bump the github-actions group across 1 directory with 7 updates
  ([#2782](https://github.com/homeassistant-ai/ha-mcp/pull/2782))

### Testing

- **e2e**: Wait for the app's own discovery flow in the MCP discovery test
  ([#2882](https://github.com/homeassistant-ai/ha-mcp/pull/2882))
- **e2e**: Cache and tune testcontainer bring-up
  ([#2852](https://github.com/homeassistant-ai/ha-mcp/pull/2852))
- Add public MCP regression coverage before runtime refactor
  ([#2856](https://github.com/homeassistant-ai/ha-mcp/pull/2856))
</details>


## v8.7.0 (2026-10-14)

### Added

- One dashboard card search that reaches cards nested inside custom cards
  ([#2700](https://github.com/homeassistant-ai/ha-mcp/pull/2700))
- Identify the helper behind an entity before removing it
  ([#2695](https://github.com/homeassistant-ai/ha-mcp/pull/2695))
- Compact ha_search_tools results with a full-schema second hop
  ([#2670](https://github.com/homeassistant-ai/ha-mcp/pull/2670))
- Use native Core contracts for energy and recorder tools
  ([#2685](https://github.com/homeassistant-ai/ha-mcp/pull/2685))
- Describe and check dashboard cards from Home Assistant's frontend
  ([#2671](https://github.com/homeassistant-ai/ha-mcp/pull/2671))
- Take helper fields, validation and backups from Home Assistant
  ([#2648](https://github.com/homeassistant-ai/ha-mcp/pull/2648))
- **policy**: Match object keys with a *~ path segment
  ([#2641](https://github.com/homeassistant-ai/ha-mcp/pull/2641))
- **radio**: Read Z-Wave parameters without configuration entities
  ([#2607](https://github.com/homeassistant-ai/ha-mcp/pull/2607))
- **policy**: Author AND-ed conditions in the policy card editor
  ([#2630](https://github.com/homeassistant-ai/ha-mcp/pull/2630))
- Add backup access controls and report ignored tool disables
  ([#2636](https://github.com/homeassistant-ai/ha-mcp/pull/2636))
- Home Assistant 2026.10 MCP discovery and tool metadata
  ([#2613](https://github.com/homeassistant-ai/ha-mcp/pull/2613))
- Ha_set_entity use_device_name for Home Assistant 2026.10's Use device name switch
  ([#2639](https://github.com/homeassistant-ai/ha-mcp/pull/2639))
- Require an ha_report_issue report on new issues
  ([#2603](https://github.com/homeassistant-ai/ha-mcp/pull/2603))
- Resume tool security approvals through MRTR
  ([#2615](https://github.com/homeassistant-ai/ha-mcp/pull/2615))
- Add Thread dataset preference and deletion actions
  ([#2592](https://github.com/homeassistant-ai/ha-mcp/pull/2592))
- **report**: File ha_report_issue reports as finished GitHub issues
  ([#2588](https://github.com/homeassistant-ai/ha-mcp/pull/2588))

### Changed

- Remove mcp-proxy setup recommendations
  ([#2710](https://github.com/homeassistant-ai/ha-mcp/pull/2710))
- Correct dev tag timing and addon-publish trigger after binary removal
  ([#2651](https://github.com/homeassistant-ai/ha-mcp/pull/2651))
- One English text per setting; render ranges and restart notes from the metadata
  ([#2669](https://github.com/homeassistant-ai/ha-mcp/pull/2669))
- Declare each setting once and generate the app options
  ([#2637](https://github.com/homeassistant-ai/ha-mcp/pull/2637))
- Retire the standalone binaries and .mcpb bundle; use uvx or Docker
  ([#2646](https://github.com/homeassistant-ai/ha-mcp/pull/2646))
- Split util_helpers.py by concern
  ([#2625](https://github.com/homeassistant-ai/ha-mcp/pull/2625))
- Split tools_search.py into a tools/search package
  ([#2623](https://github.com/homeassistant-ai/ha-mcp/pull/2623))
- Split tools_config_helpers.py into a config_helpers package
  ([#2624](https://github.com/homeassistant-ai/ha-mcp/pull/2624))
- Split the component's websocket_api.py into a package
  ([#2628](https://github.com/homeassistant-ai/ha-mcp/pull/2628))
- Split settings.js into nine parts
  ([#2622](https://github.com/homeassistant-ai/ha-mcp/pull/2622))
- Split server
  ([#2626](https://github.com/homeassistant-ai/ha-mcp/pull/2626))
- Split config.py into four modules
  ([#2621](https://github.com/homeassistant-ai/ha-mcp/pull/2621))
- Bring five long functions under the statement limit
  ([#2612](https://github.com/homeassistant-ai/ha-mcp/pull/2612))
- Fold simple-helper fields into config and route helper writes through the component
  ([#2618](https://github.com/homeassistant-ai/ha-mcp/pull/2618))
- Stop scanning every installed package for the version
  ([#2595](https://github.com/homeassistant-ai/ha-mcp/pull/2595))

### Fixed

- Redact unmarked PEM private keys
  ([#2714](https://github.com/homeassistant-ai/ha-mcp/pull/2714))
- Clarify Claude Desktop approval guidance across write tools
  ([#2711](https://github.com/homeassistant-ai/ha-mcp/pull/2711))
- **component**: Take the component's flow helpers from Core's helper flow list
  ([#2697](https://github.com/homeassistant-ai/ha-mcp/pull/2697))
- Relay Nabu Casa cloudhooks through the webhook ingress
  ([#2703](https://github.com/homeassistant-ai/ha-mcp/pull/2703))
- Read statistics metadata after the rows in ha_get_history
  ([#2693](https://github.com/homeassistant-ai/ha-mcp/pull/2693))
- Reply before restarting HA when running embedded
  ([#2692](https://github.com/homeassistant-ai/ha-mcp/pull/2692))
- Support Home Assistant 2026.11 pip_kwargs() signature
  ([#2690](https://github.com/homeassistant-ai/ha-mcp/pull/2690))
- Use OAuth request credentials for HACS tools
  ([#2689](https://github.com/homeassistant-ai/ha-mcp/pull/2689))
- Resolve statistics units through native recorder metadata
  ([#2684](https://github.com/homeassistant-ai/ha-mcp/pull/2684))
- Correct OAuth metadata for read-only webhook connections
  ([#2683](https://github.com/homeassistant-ai/ha-mcp/pull/2683))
- **validation**: Tell agents how to fix rejected config tool calls
  ([#2652](https://github.com/homeassistant-ai/ha-mcp/pull/2652))
- **component**: Carry the caller's context on component service calls
  ([#2614](https://github.com/homeassistant-ai/ha-mcp/pull/2614))
- Ha_set_area_or_floor accepts null to clear floor_id, icon, picture and sensor references
  ([#2654](https://github.com/homeassistant-ai/ha-mcp/pull/2654))
- Update a zone's stored icon and refuse clearing it
  ([#2643](https://github.com/homeassistant-ai/ha-mcp/pull/2643))
- Name an unparseable Core version in the use_device_name refusal
  ([#2640](https://github.com/homeassistant-ai/ha-mcp/pull/2640))
- Ha_set_area_or_floor sets and clears area temperature/humidity sensors
  ([#2638](https://github.com/homeassistant-ai/ha-mcp/pull/2638))
- Include pinned tools in ha_search_tools and ask for English queries
  ([#2635](https://github.com/homeassistant-ai/ha-mcp/pull/2635))
- Clear names on whitespace, reject quote-only names, fix device_name echo
  ([#2617](https://github.com/homeassistant-ai/ha-mcp/pull/2617))
- Cap oversized system_log messages under compact
  ([#2606](https://github.com/homeassistant-ai/ha-mcp/pull/2606))
- Update skills-vendor to 6c9fffe and follow its reference file split
  ([#2574](https://github.com/homeassistant-ai/ha-mcp/pull/2574))
- Ship only ha_mcp in the wheel and build with uv_build
  ([#2599](https://github.com/homeassistant-ai/ha-mcp/pull/2599))

---
<details>
<summary>Internal Changes</summary>


### Added

- **ci**: Maintainer slash agent for issues and PRs
  ([#2467](https://github.com/homeassistant-ai/ha-mcp/pull/2467))

### Changed

- **tests**: Say rule 6 does not ask for committed dependencies
  ([#2678](https://github.com/homeassistant-ai/ha-mcp/pull/2678))
- Keep CI results out of the PR template Testing section
  ([#2644](https://github.com/homeassistant-ai/ha-mcp/pull/2644))
- Comment changes count for the component gate; PR CI needs a master base
  ([#2620](https://github.com/homeassistant-ai/ha-mcp/pull/2620))
- **agents**: Fix stale references and app wording in agent docs
  ([#2598](https://github.com/homeassistant-ai/ha-mcp/pull/2598))
- Split the e2e conftest into helper modules
  ([#2627](https://github.com/homeassistant-ai/ha-mcp/pull/2627))

### Fixed

- **ci**: Queue slash review notifications without cancellation
  ([#2709](https://github.com/homeassistant-ai/ha-mcp/pull/2709))
- Clarify pre-merge live testing and slash-agent attestations
  ([#2705](https://github.com/homeassistant-ai/ha-mcp/pull/2705))

### Build System

- **deps**: Bump devalue from 5.9.2 to 5.9.4 in /site
  ([#2605](https://github.com/homeassistant-ai/ha-mcp/pull/2605))
- **deps**: Bump brace-expansion in /site
  ([#2589](https://github.com/homeassistant-ai/ha-mcp/pull/2589))

### Chores

- **deps**: Update uv
  ([#2730](https://github.com/homeassistant-ai/ha-mcp/pull/2730))
- **deps**: Update src/ha_mcp/resources/skills-vendor digest to 7690c5e
  ([#2728](https://github.com/homeassistant-ai/ha-mcp/pull/2728))
- **deps**: Update public.ecr.aws/docker/library/python:3.13-slim docker digest to 70729b4
  ([#2727](https://github.com/homeassistant-ai/ha-mcp/pull/2727))
- Sync ratchet baselines after merge [skip ci]
  ([`0dac79d`](https://github.com/homeassistant-ai/ha-mcp/commit/0dac79d2ac5cbc0348f6faccd55a188959c02093))
- Machine-translate locale updates
  ([`ad7e648`](https://github.com/homeassistant-ai/ha-mcp/commit/ad7e648bd2a9b7b45888c4393b386d7cc1fad29b))
- Sync ratchet baselines after merge [skip ci]
  ([`3b895d6`](https://github.com/homeassistant-ai/ha-mcp/commit/3b895d603a249d5a58693b21580f2d081594854d))
- Sync tool docs after merge [skip ci]
  ([`50e3e18`](https://github.com/homeassistant-ai/ha-mcp/commit/50e3e18a06cddeb6ceb1789225a44a3d63b8b359))
- Sync ratchet baselines after merge [skip ci]
  ([`ff35ea2`](https://github.com/homeassistant-ai/ha-mcp/commit/ff35ea293fee89fd7ab61f426fb7ad90db437e96))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.10.1
  ([#2706](https://github.com/homeassistant-ai/ha-mcp/pull/2706))
- Machine-translate locale updates
  ([`2e2c34d`](https://github.com/homeassistant-ai/ha-mcp/commit/2e2c34d7fe86f34aa4f51b3b832290251b492d66))
- **addon**: Promote webhook-proxy dev -> stable 3.0.4
  ([#2704](https://github.com/homeassistant-ai/ha-mcp/pull/2704))
- Sync ratchet baselines after merge [skip ci]
  ([`fdbe457`](https://github.com/homeassistant-ai/ha-mcp/commit/fdbe457cde72d7e28bf766bd90ca22b417db972e))
- Sync ratchet baselines after merge [skip ci]
  ([`f4cf27d`](https://github.com/homeassistant-ai/ha-mcp/commit/f4cf27d9489dd2c4da374c6221ba8aadd905b768))
- Sync tool docs after merge [skip ci]
  ([`3c97b08`](https://github.com/homeassistant-ai/ha-mcp/commit/3c97b08366a534f4031ff4d96d696df7e78d7bbe))
- Sync tool docs after merge [skip ci]
  ([`32880f1`](https://github.com/homeassistant-ai/ha-mcp/commit/32880f12a6ddaab174c56ef0e8bdf7205e823407))
- Sync ratchet baselines after merge [skip ci]
  ([`7666c6c`](https://github.com/homeassistant-ai/ha-mcp/commit/7666c6cd8b16639ecf4f6d74d29f1986e830cd7f))
- Sync ratchet baselines after merge [skip ci]
  ([`c037881`](https://github.com/homeassistant-ai/ha-mcp/commit/c03788133b341676880d98f3e9eb3c3a726e8ee4))
- Sync tool docs after merge [skip ci]
  ([`a797fb3`](https://github.com/homeassistant-ai/ha-mcp/commit/a797fb361f31a55b7bd4dcac7db31cc03d9f6eb5))
- Sync ratchet baselines after merge [skip ci]
  ([`1e6254f`](https://github.com/homeassistant-ai/ha-mcp/commit/1e6254f9e7899a625ad2d21ad98246f84b810d6e))
- Sync ratchet baselines after merge [skip ci]
  ([`b5b743c`](https://github.com/homeassistant-ai/ha-mcp/commit/b5b743c248b034968a741ee5e747f222a27fc086))
- Sync tool docs after merge [skip ci]
  ([`6cbef88`](https://github.com/homeassistant-ai/ha-mcp/commit/6cbef88fa067cf903e54c44d3a7771594eca60a8))
- Sync ratchet baselines after merge [skip ci]
  ([`7a61e2e`](https://github.com/homeassistant-ai/ha-mcp/commit/7a61e2eb5c2986c51bef612d4a7b64f5f13cc1a5))
- Sync ratchet baselines after merge [skip ci]
  ([`72f466f`](https://github.com/homeassistant-ai/ha-mcp/commit/72f466f9ea95c22d8b7855ae5724733105b0d8ef))
- Sync tool docs after merge [skip ci]
  ([`5fc6cd9`](https://github.com/homeassistant-ai/ha-mcp/commit/5fc6cd942348942f78b7ff84dfcb0678d86ba634))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.10.0
  ([#2686](https://github.com/homeassistant-ai/ha-mcp/pull/2686))
- Sync ratchet baselines after merge [skip ci]
  ([`3ac419f`](https://github.com/homeassistant-ai/ha-mcp/commit/3ac419fb5e2fc410e1dc3f0a9a480f92b0de6fe9))
- Sync tool docs after merge [skip ci]
  ([`f50bc49`](https://github.com/homeassistant-ai/ha-mcp/commit/f50bc49cf01abbcef6c63f4ea0c2269c215d31cd))
- Sync ratchet baselines after merge [skip ci]
  ([`98da287`](https://github.com/homeassistant-ai/ha-mcp/commit/98da2877a2ed41f9eed0f1d2517e666c0ef663d1))
- Machine-translate locale updates
  ([`a4d5216`](https://github.com/homeassistant-ai/ha-mcp/commit/a4d5216c4f230e5b6a9f626d74265e035a1245af))
- **deps**: Update dependency fastmcp-slim to v4.0.10
  ([#2679](https://github.com/homeassistant-ai/ha-mcp/pull/2679))
- **deps**: Update uv
  ([#2680](https://github.com/homeassistant-ai/ha-mcp/pull/2680))
- **deps**: Bump source-map-js from 1.2.1 to 1.2.2 in /site
  ([#2677](https://github.com/homeassistant-ai/ha-mcp/pull/2677))
- **deps**: Bump sharp from 0.35.4 to 0.35.5 in /site
  ([#2675](https://github.com/homeassistant-ai/ha-mcp/pull/2675))
- **deps**: Bump smol-toml from 1.8.0 to 1.9.0 in /site
  ([#2674](https://github.com/homeassistant-ai/ha-mcp/pull/2674))
- Sync ratchet baselines after merge [skip ci]
  ([`6ba297e`](https://github.com/homeassistant-ai/ha-mcp/commit/6ba297e3175d3357788705655f3272f3fbd961ac))
- **deps**: Update python:3.13-slim docker digest to bf44cdf
  ([#2672](https://github.com/homeassistant-ai/ha-mcp/pull/2672))
- **deps**: Update src/ha_mcp/resources/skills-vendor digest to 6ac284a
  ([#2673](https://github.com/homeassistant-ai/ha-mcp/pull/2673))
- Machine-translate locale updates
  ([`c763c5b`](https://github.com/homeassistant-ai/ha-mcp/commit/c763c5b357f651ac504d1e6dbb2f6f19775e7040))
- Sync ratchet baselines after merge [skip ci]
  ([`28386c0`](https://github.com/homeassistant-ai/ha-mcp/commit/28386c0d9a9380d8ab3a14de22d9320c7ebc8180))
- Sync ratchet baselines after merge [skip ci]
  ([`32f27ae`](https://github.com/homeassistant-ai/ha-mcp/commit/32f27ae4cf20fbbf7fa1080951aa896dcf4645e6))
- Sync tool docs after merge [skip ci]
  ([`ce717d6`](https://github.com/homeassistant-ai/ha-mcp/commit/ce717d6c15319494a5f8a21bef9cf933dd33f9ae))
- Sync ratchet baselines after merge [skip ci]
  ([`2849605`](https://github.com/homeassistant-ai/ha-mcp/commit/2849605e67d08d4833cfa75d79f25fc30eedece4))
- Machine-translate locale updates
  ([`adaa968`](https://github.com/homeassistant-ai/ha-mcp/commit/adaa968ed1b05ebf88b81d3f1088c8ec41fa3778))
- Sync ratchet baselines after merge [skip ci]
  ([`1efdcfb`](https://github.com/homeassistant-ai/ha-mcp/commit/1efdcfb129840184bcff1f63bb24d04ba9ccc661))
- Sync tool docs after merge [skip ci]
  ([`484b9ed`](https://github.com/homeassistant-ai/ha-mcp/commit/484b9edbb1adbf0323a980062c005d4b9a26d2ff))
- Sync ratchet baselines after merge [skip ci]
  ([`9cd24b7`](https://github.com/homeassistant-ai/ha-mcp/commit/9cd24b7faf475b15f68809788a4180389faecd56))
- Sync tool docs after merge [skip ci]
  ([`7e4a5f4`](https://github.com/homeassistant-ai/ha-mcp/commit/7e4a5f49aee91b11a861f1cb7d82bb646f14e790))
- Sync ratchet baselines after merge [skip ci]
  ([`97e1735`](https://github.com/homeassistant-ai/ha-mcp/commit/97e1735c435052bd3dcfe35d2a72770ce215d94d))
- Sync tool docs after merge [skip ci]
  ([`98b770a`](https://github.com/homeassistant-ai/ha-mcp/commit/98b770a28e968fd205db6c6dfb32f050e96a41fb))
- Sync ratchet baselines after merge [skip ci]
  ([`7fb17ff`](https://github.com/homeassistant-ai/ha-mcp/commit/7fb17ff2eb7a7e81cc11baa5122d78dceafe6a38))
- Sync ratchet baselines after merge [skip ci]
  ([`476ffa2`](https://github.com/homeassistant-ai/ha-mcp/commit/476ffa2229fc73df205c21fef1a42f4dece476e5))
- Sync tool docs after merge [skip ci]
  ([`9554c2a`](https://github.com/homeassistant-ai/ha-mcp/commit/9554c2a3681457e8e944d8e881b3b70c1c5a88ff))
- Sync tool docs after merge [skip ci]
  ([`ee5cac3`](https://github.com/homeassistant-ai/ha-mcp/commit/ee5cac37f958c9cea223ce9246c13240626b90fc))
- Machine-translate locale updates
  ([`0ac705b`](https://github.com/homeassistant-ai/ha-mcp/commit/0ac705bcd8a326424a86fd4f85d907b6bda9caf0))
- Sync tool docs after merge [skip ci]
  ([`89e4261`](https://github.com/homeassistant-ai/ha-mcp/commit/89e4261ad9811488e19865c82bcd58ec1c1c2e54))
- Sync tool docs after merge [skip ci]
  ([`1acce73`](https://github.com/homeassistant-ai/ha-mcp/commit/1acce73410ece3ad184e9d0f96935b3d9e4b559d))
- Sync tool docs after merge [skip ci]
  ([`da1eae7`](https://github.com/homeassistant-ai/ha-mcp/commit/da1eae78a2e3e0f0745db67d0569a5c898cc5d70))
- Add a duplicate-code ratchet
  ([#2631](https://github.com/homeassistant-ai/ha-mcp/pull/2631))
- Sync tool docs after merge [skip ci]
  ([`f26f14e`](https://github.com/homeassistant-ai/ha-mcp/commit/f26f14edd8427dcb850bb4478c6def1b48bce7be))
- Enable PLR0915 and mark the existing long functions
  ([#2610](https://github.com/homeassistant-ai/ha-mcp/pull/2610))
- Enable BLE001 and mark the existing blind excepts
  ([#2609](https://github.com/homeassistant-ai/ha-mcp/pull/2609))
- Sync tool docs after merge [skip ci]
  ([`5c38f99`](https://github.com/homeassistant-ai/ha-mcp/commit/5c38f99aed5818b7ccbfbdbf4dafac2620c4af90))
- Add a module-size ratchet
  ([#2608](https://github.com/homeassistant-ai/ha-mcp/pull/2608))
- Sync tool docs after merge [skip ci]
  ([`6985f0a`](https://github.com/homeassistant-ai/ha-mcp/commit/6985f0ad7af3259d07139101ce8364b9e8c082bc))
- Remove unused and default settings from pyproject.toml
  ([#2604](https://github.com/homeassistant-ai/ha-mcp/pull/2604))
- Machine-translate locale updates
  ([`674f508`](https://github.com/homeassistant-ai/ha-mcp/commit/674f508fc60704d400a0cdc2bc58d6a759b06fa3))
- **addon**: Publish version 8.6.0 [skip ci]
  ([`7431f2f`](https://github.com/homeassistant-ai/ha-mcp/commit/7431f2f5d0d98e3a514b6314ceb7901736abb1fd))

### Continuous Integration

- **deps**: Bump the github-actions group with 3 updates
  ([#2716](https://github.com/homeassistant-ai/ha-mcp/pull/2716))
- Run the dev environment as a live HA, updated with ha-mcp's own tools
  ([#2702](https://github.com/homeassistant-ai/ha-mcp/pull/2702))
- Run the BAT runner tests in the unit test job
  ([#2657](https://github.com/homeassistant-ai/ha-mcp/pull/2657))
- Devenv reuses a cached HAOS image across component changes
  ([#2656](https://github.com/homeassistant-ai/ha-mcp/pull/2656))
- **deps**: Bump the github-actions group with 2 updates
  ([#2668](https://github.com/homeassistant-ai/ha-mcp/pull/2668))
- Encrypt the dev environment's URLs to a per-run public key
  ([#2647](https://github.com/homeassistant-ai/ha-mcp/pull/2647))
- Add a fork-only live dev HA environment (Docker or HAOS)
  ([#2645](https://github.com/homeassistant-ai/ha-mcp/pull/2645))
- Stop pull requests from editing the ratchet baselines
  ([#2642](https://github.com/homeassistant-ai/ha-mcp/pull/2642))
- Fail when a ratchet baseline grows against the base branch
  ([#2634](https://github.com/homeassistant-ai/ha-mcp/pull/2634))
- Hide release-note entries that change no user-facing file
  ([#2596](https://github.com/homeassistant-ai/ha-mcp/pull/2596))
- **intake**: Label filed ha_report_issue reports and ask for missing ones
  ([#2591](https://github.com/homeassistant-ai/ha-mcp/pull/2591))

### Testing

- Assert the exact stdio bridge config
  ([#2712](https://github.com/homeassistant-ai/ha-mcp/pull/2712))
- **uat**: Log the tool names ha_search_tools returned in the BAT trace
  ([#2650](https://github.com/homeassistant-ai/ha-mcp/pull/2650))
- Run the JSDOM harness in one node process per pytest worker
  ([#2597](https://github.com/homeassistant-ai/ha-mcp/pull/2597))
</details>


## v8.6.0 (2026-09-30)

### Added

- **policy**: Let a policy approve the calls it lists (allow list)
  ([#2554](https://github.com/homeassistant-ai/ha-mcp/pull/2554))
- Ha_get_skill_guide returns SKILL.md with no arguments
  ([#2556](https://github.com/homeassistant-ai/ha-mcp/pull/2556))
- Move runtime controls off ha_call_service onto dedicated tools
  ([#2529](https://github.com/homeassistant-ai/ha-mcp/pull/2529))
- **ha_eval_template**: Return Home Assistant's template errors; add variables, strict and condition testing
  ([#2524](https://github.com/homeassistant-ai/ha-mcp/pull/2524))
- Add automation runtime enable state
  ([#2448](https://github.com/homeassistant-ai/ha-mcp/pull/2448))
- **policy**: Announce pending tool approvals on the event bus
  ([#2514](https://github.com/homeassistant-ai/ha-mcp/pull/2514))
- **i18n**: Add Azerbaijani (az) locale
  ([#2499](https://github.com/homeassistant-ai/ha-mcp/pull/2499))
- **calendar**: Add update mode to ha_config_set_calendar_event
  ([#2490](https://github.com/homeassistant-ai/ha-mcp/pull/2490))
- **bug-report**: Identify the host app that launched ha-mcp
  ([#2488](https://github.com/homeassistant-ai/ha-mcp/pull/2488))
- Simplify component search and add scene discovery
  ([#2481](https://github.com/homeassistant-ai/ha-mcp/pull/2481))
- Assign Home Assistant labels to areas
  ([#2459](https://github.com/homeassistant-ai/ha-mcp/pull/2459))

### Changed

- Antigravity subscriptions/listen workaround and HTTP transport badge
  ([#2549](https://github.com/homeassistant-ai/ha-mcp/pull/2549))
- Surface the hosted-connector rules in the FAQ and issue templates
  ([#2500](https://github.com/homeassistant-ai/ha-mcp/pull/2500))
- **tools**: Trim tool and property descriptions to what the model needs at call time
  ([#2528](https://github.com/homeassistant-ai/ha-mcp/pull/2528))
- Omit private tool metadata without an LLM API consumer
  ([#2487](https://github.com/homeassistant-ai/ha-mcp/pull/2487))

### Fixed

- Refuse admin-only REST routes for non-admin tokens instead of tripping HA's IP ban
  ([#2551](https://github.com/homeassistant-ai/ha-mcp/pull/2551))
- Name list fields received as {"item": ...} or "" instead of passing on HA's unrelated error
  ([#2553](https://github.com/homeassistant-ai/ha-mcp/pull/2553))
- Prevent password autofill and invalid connection paths
  ([#2552](https://github.com/homeassistant-ai/ha-mcp/pull/2552))
- Return stored config entry options without flattening nested values
  ([#2543](https://github.com/homeassistant-ai/ha-mcp/pull/2543))
- Correct tool descriptions, delete binary files, restore UAT tool traces
  ([#2539](https://github.com/homeassistant-ai/ha-mcp/pull/2539))
- Log tool status lines instead of sending deprecated MCP log messages
  ([#2531](https://github.com/homeassistant-ai/ha-mcp/pull/2531))
- Add app support pointer to Supervisor errors
  ([#2534](https://github.com/homeassistant-ai/ha-mcp/pull/2534))
- Keep the entity's own-name alias when ha_set_entity writes aliases
  ([#2496](https://github.com/homeassistant-ai/ha-mcp/pull/2496))
- Surface Claude Desktop approval-hang guidance to agents and users
  ([#2480](https://github.com/homeassistant-ai/ha-mcp/pull/2480))
- Serialize concurrent registry label updates
  ([#2475](https://github.com/homeassistant-ai/ha-mcp/pull/2475))

---
<details>
<summary>Internal Changes</summary>


### Changed

- **tests**: Align instructions and tests with the test design rules
  ([#2558](https://github.com/homeassistant-ai/ha-mcp/pull/2558))
- **tests**: Preserve meaningful regression coverage
  ([`62819d6`](https://github.com/homeassistant-ai/ha-mcp/commit/62819d6bfcc438f003b5820a570b789b4a0ab522))
- **tests**: Keep test-first order and fit JS and YAML tests
  ([`99865db`](https://github.com/homeassistant-ai/ha-mcp/commit/99865db30b5d8a56392daf3ffa757e84c81be398))
- **tests**: Add test design rules
  ([#2555](https://github.com/homeassistant-ai/ha-mcp/pull/2555))
- Fix stale run instructions in skills and AGENTS.md
  ([#2537](https://github.com/homeassistant-ai/ha-mcp/pull/2537))

### Fixed

- **locales**: One number rule for Locale Sync and its pre-push check
  ([#2523](https://github.com/homeassistant-ai/ha-mcp/pull/2523))
- Ignore Markdown code/bold markers when matching intake quotes
  ([#2517](https://github.com/homeassistant-ai/ha-mcp/pull/2517))
- Wait for Supervisor's running state before app lifecycle calls
  ([#2489](https://github.com/homeassistant-ai/ha-mcp/pull/2489))

### Chores

- **deps**: Update uv
  ([#2578](https://github.com/homeassistant-ai/ha-mcp/pull/2578))
- **deps**: Update dependency fastmcp-slim to v4.0.5
  ([#2577](https://github.com/homeassistant-ai/ha-mcp/pull/2577))
- **deps**: Update python:3.13-slim docker digest to 7c61056
  ([#2573](https://github.com/homeassistant-ai/ha-mcp/pull/2573))
- Machine-translate locale updates
  ([`4634a98`](https://github.com/homeassistant-ai/ha-mcp/commit/4634a98cacf43c2eb45cf1e955e3ab3564964f18))
- Sync tool docs after merge [skip ci]
  ([`c765627`](https://github.com/homeassistant-ai/ha-mcp/commit/c7656272003249936ea20a62cd67d39ea4f5f1b5))
- **deps**: Update dependency home-assistant/supervisor to v2026.09.3
  ([#2572](https://github.com/homeassistant-ai/ha-mcp/pull/2572))
- Machine-translate locale updates
  ([`70e71d9`](https://github.com/homeassistant-ai/ha-mcp/commit/70e71d9efdc842453579cccffa65aa76f173cc76))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.9.4
  ([#2550](https://github.com/homeassistant-ai/ha-mcp/pull/2550))
- Sync tool docs after merge [skip ci]
  ([`1ce2ce4`](https://github.com/homeassistant-ai/ha-mcp/commit/1ce2ce4cd508d479ba879285911bdb8ec1c82c68))
- Sync tool docs after merge [skip ci]
  ([`7d292d8`](https://github.com/homeassistant-ai/ha-mcp/commit/7d292d8beea164afc34eaaf4c9a62bd9b35d5d75))
- Machine-translate locale updates
  ([`f7ce464`](https://github.com/homeassistant-ai/ha-mcp/commit/f7ce464ef7890eac21404b8a7f3b78e0ae513894))
- **deps**: Update dependency renovatebot/renovate to v44.103.1
  ([#2536](https://github.com/homeassistant-ai/ha-mcp/pull/2536))
- **deps**: Update src/ha_mcp/resources/skills-vendor digest to bc73bda
  ([#2535](https://github.com/homeassistant-ai/ha-mcp/pull/2535))
- Sync tool docs after merge [skip ci]
  ([`359aeb8`](https://github.com/homeassistant-ai/ha-mcp/commit/359aeb8bb299e88acd6c78e674ec46fa5ed1b1b3))
- Sync tool docs after merge [skip ci]
  ([`864463d`](https://github.com/homeassistant-ai/ha-mcp/commit/864463db8b801aa79706801d28f1d0c1cef08637))
- Machine-translate locale updates
  ([`1b56328`](https://github.com/homeassistant-ai/ha-mcp/commit/1b56328d268717bc0b091785bdd1a3d0c8034f27))
- Sync tool docs after merge [skip ci]
  ([`af7631a`](https://github.com/homeassistant-ai/ha-mcp/commit/af7631a86836b6a5392978227bc0760e2b470722))
- Sync tool docs after merge [skip ci]
  ([`3a64e7b`](https://github.com/homeassistant-ai/ha-mcp/commit/3a64e7b9d1ee2897999d53954d4801c24c77d0ab))
- Machine-translate locale updates
  ([`0987ade`](https://github.com/homeassistant-ai/ha-mcp/commit/0987ade9776328c3bb166876cd949b5d75e88303))
- **deps**: Update uv
  ([#2520](https://github.com/homeassistant-ai/ha-mcp/pull/2520))
- Sync tool docs after merge [skip ci]
  ([`abf9862`](https://github.com/homeassistant-ai/ha-mcp/commit/abf9862eb887a25da8c91773e932899a6d2418f6))
- **deps**: Update dependency renovatebot/renovate to v44.93.5
  ([#2521](https://github.com/homeassistant-ai/ha-mcp/pull/2521))
- **deps**: Update src/ha_mcp/resources/skills-vendor digest to 309c07a
  ([#2519](https://github.com/homeassistant-ai/ha-mcp/pull/2519))
- **deps**: Update python:3.13-slim docker digest to 8d9d0b8
  ([#2518](https://github.com/homeassistant-ai/ha-mcp/pull/2518))
- Sync tool docs after merge [skip ci]
  ([`2bc821d`](https://github.com/homeassistant-ai/ha-mcp/commit/2bc821dbe585641c4663fca0b335798a0b541161))
- Machine-translate locale updates
  ([`ae4db2d`](https://github.com/homeassistant-ai/ha-mcp/commit/ae4db2de720b72a4bdfb48fd904cf6fe0f482270))
- Sync tool docs after merge [skip ci]
  ([`c9d8240`](https://github.com/homeassistant-ai/ha-mcp/commit/c9d8240904d9bad232cc48993a61a8a9e3dae3e0))
- **deps**: Update dependency home-assistant/operating-system to v18.3
  ([#2493](https://github.com/homeassistant-ai/ha-mcp/pull/2493))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.9.3
  ([#2491](https://github.com/homeassistant-ai/ha-mcp/pull/2491))
- Sync tool docs after merge [skip ci]
  ([`d6d6c5e`](https://github.com/homeassistant-ai/ha-mcp/commit/d6d6c5eba68921427d82499ad14fc2256e8b2152))
- Sync tool docs after merge [skip ci]
  ([`debbebd`](https://github.com/homeassistant-ai/ha-mcp/commit/debbebdc141b2a8dcb04efde70f5729aeb823d23))
- Sync tool docs after merge [skip ci]
  ([`10a2bbb`](https://github.com/homeassistant-ai/ha-mcp/commit/10a2bbbfcac02b18aad53b31201f038b4dd524b8))
- **deps**: Update dependency home-assistant/supervisor to v2026.09.2
  ([#2473](https://github.com/homeassistant-ai/ha-mcp/pull/2473))
- **addon**: Publish version 8.5.0 [skip ci]
  ([`8182c6e`](https://github.com/homeassistant-ai/ha-mcp/commit/8182c6e6db68b37ae7041dec6dd4ade7b7356fb7))

### Continuous Integration

- **deps**: Bump the github-actions group with 5 updates
  ([#2569](https://github.com/homeassistant-ai/ha-mcp/pull/2569))
- **deps**: Bump the github-actions group with 2 updates
  ([#2513](https://github.com/homeassistant-ai/ha-mcp/pull/2513))
- Replace issue enrichment with factual Codex documentation
  ([#2447](https://github.com/homeassistant-ai/ha-mcp/pull/2447))
- **deps**: Bump devalue from 5.8.1 to 5.9.2 in /site
  ([#2486](https://github.com/homeassistant-ai/ha-mcp/pull/2486))
- Track the HAOS pin from stable.json instead of GitHub releases
  ([#2485](https://github.com/homeassistant-ai/ha-mcp/pull/2485))

### Testing

- **e2e**: Attach the Home Assistant log to failures and gate timeouts
  ([#2571](https://github.com/homeassistant-ai/ha-mcp/pull/2571))
- **e2e**: Keep local E2E runs out of the developer's ~/.ha-mcp
  ([#2544](https://github.com/homeassistant-ai/ha-mcp/pull/2544))
- Keep unit tests, BAT and app tests off the developer machine
  ([#2541](https://github.com/homeassistant-ai/ha-mcp/pull/2541))
</details>


## v8.5.0 (2026-09-16)

### Added

- Add check_updates action to reload the app store
  ([#2460](https://github.com/homeassistant-ai/ha-mcp/pull/2460))
- Migrate to the vendored FastMCP 4.0.3 and MCP SDK 2.2.0
  ([#2443](https://github.com/homeassistant-ai/ha-mcp/pull/2443))
- Add read-only HTTP and Home Assistant webhook endpoints
  ([#2440](https://github.com/homeassistant-ai/ha-mcp/pull/2440))
- Make Extra YAML write keys a multiline editor
  ([#2441](https://github.com/homeassistant-ai/ha-mcp/pull/2441))
- Add opt-in HTTP diagnostics and JSON responses
  ([#2431](https://github.com/homeassistant-ai/ha-mcp/pull/2431))
- Back up and restore Template helper options
  ([#2419](https://github.com/homeassistant-ai/ha-mcp/pull/2419))
- **i18n**: Add Traditional Chinese locale
  ([#2267](https://github.com/homeassistant-ai/ha-mcp/pull/2267))
- Add native dashboard edits and structured patches
  ([#2403](https://github.com/homeassistant-ai/ha-mcp/pull/2403))
- Add history query workload safeguards
  ([#2371](https://github.com/homeassistant-ai/ha-mcp/pull/2371))
- Add optional cross-session tool concurrency limit
  ([#2369](https://github.com/homeassistant-ai/ha-mcp/pull/2369))
- **search**: Reach manage tools from every call proxy
  ([#2378](https://github.com/homeassistant-ai/ha-mcp/pull/2378))
- **logs**: Add fault_log source for home-assistant.log.fault
  ([#2377](https://github.com/homeassistant-ai/ha-mcp/pull/2377))
- Consolidate blueprint tools into ha_manage_blueprints with delete, save, and substitute
  ([#2356](https://github.com/homeassistant-ai/ha-mcp/pull/2356))

### Changed

- Clarify Claude.ai auto-detection and retry guidance
  ([#2425](https://github.com/homeassistant-ai/ha-mcp/pull/2425))
- Avoid unnecessary work for narrow overview requests
  ([#2370](https://github.com/homeassistant-ai/ha-mcp/pull/2370))

### Fixed

- Point unknown tool arguments at the intended parameter
  ([#2465](https://github.com/homeassistant-ai/ha-mcp/pull/2465))
- Wait for scene and script registry identities after writes
  ([#2461](https://github.com/homeassistant-ai/ha-mcp/pull/2461))
- Make opt-in HTTP diagnostics visible under WARNING logging
  ([#2436](https://github.com/homeassistant-ai/ha-mcp/pull/2436))
- **locale-sync**: Retry an unparseable engine answer like a transient status
  ([#2424](https://github.com/homeassistant-ai/ha-mcp/pull/2424))
- Guard automation alias replacements with a prior config hash
  ([#2417](https://github.com/homeassistant-ai/ha-mcp/pull/2417))
- Enforce read-only mode inside ha_call_service
  ([#2418](https://github.com/homeassistant-ai/ha-mcp/pull/2418))
- **policy**: Consume an approval exactly once across concurrent identical calls
  ([#2399](https://github.com/homeassistant-ai/ha-mcp/pull/2399))
- Default auto-backup dir to <data dir>/backups outside the add-on
  ([#2376](https://github.com/homeassistant-ai/ha-mcp/pull/2376))
- Refuse over-nested list-param JSON and fail a merge that strands the component under a released version
  ([#2375](https://github.com/homeassistant-ai/ha-mcp/pull/2375))

---
<details>
<summary>Internal Changes</summary>


### Changed

- Restore scope anti-patterns and move general code conventions into AGENTS.md
  ([#2466](https://github.com/homeassistant-ai/ha-mcp/pull/2466))

### Fixed

- **deps**: Preserve embedded compatibility with supported HA Core
  ([#2400](https://github.com/homeassistant-ai/ha-mcp/pull/2400))
- **ci**: Regenerate vendored websockets in Renovate updates
  ([#2385](https://github.com/homeassistant-ai/ha-mcp/pull/2385))

### Chores

- **deps**: Update dependency astral-sh/uv to v0.12.11
  ([#2470](https://github.com/homeassistant-ai/ha-mcp/pull/2470))
- **deps**: Update dependency renovatebot/renovate to v44.69.11
  ([#2471](https://github.com/homeassistant-ai/ha-mcp/pull/2471))
- Sync tool docs after merge [skip ci]
  ([`3a21cd0`](https://github.com/homeassistant-ai/ha-mcp/commit/3a21cd0c583de3f32e8075cbcbf2e755a0856b01))
- **deps**: Update dependency renovatebot/renovate to v44.69.10
  ([#2469](https://github.com/homeassistant-ai/ha-mcp/pull/2469))
- **deps**: Update uv
  ([#2468](https://github.com/homeassistant-ai/ha-mcp/pull/2468))
- Sync tool docs after merge [skip ci]
  ([`8e88bb8`](https://github.com/homeassistant-ai/ha-mcp/commit/8e88bb81e989d1ea1d09133a3a754acc83718e15))
- Sync tool docs after merge [skip ci]
  ([`e554f67`](https://github.com/homeassistant-ai/ha-mcp/commit/e554f67c271704f6aa97e757c28992936b594cf0))
- **component**: Raise the pending component version to 2.2.0
  ([#2463](https://github.com/homeassistant-ai/ha-mcp/pull/2463))
- Prepare the tree for the vendored FastMCP
  ([#2445](https://github.com/homeassistant-ai/ha-mcp/pull/2445))
- Remove docs/superpowers planning notes and ignore the directory
  ([#2456](https://github.com/homeassistant-ai/ha-mcp/pull/2456))
- Machine-translate locale updates
  ([`aa9df9d`](https://github.com/homeassistant-ai/ha-mcp/commit/aa9df9deda6678d2bc796df5f2457d82068e8b07))
- Machine-translate locale updates
  ([`30c6cb3`](https://github.com/homeassistant-ai/ha-mcp/commit/30c6cb3a369a3b0089499d16fd5663826705743d))
- Vendor FastMCP 4.0.3 and MCP SDK 2.2.0 sources
  ([#2442](https://github.com/homeassistant-ai/ha-mcp/pull/2442))
- **addon**: Promote webhook-proxy dev -> stable 3.0.3
  ([#2439](https://github.com/homeassistant-ai/ha-mcp/pull/2439))
- Machine-translate locale updates
  ([`b2e9bc1`](https://github.com/homeassistant-ai/ha-mcp/commit/b2e9bc1851d92a8309f8060786ebb11630b315eb))
- Remove dead code and subprocess timeouts redundant with pytest's
  ([#2430](https://github.com/homeassistant-ai/ha-mcp/pull/2430))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.9.2
  ([#2429](https://github.com/homeassistant-ai/ha-mcp/pull/2429))
- Machine-translate locale updates
  ([`7e9c5f7`](https://github.com/homeassistant-ai/ha-mcp/commit/7e9c5f7423977d85090db86dd7e668b08ae24392))
- Sync tool docs after merge [skip ci]
  ([`fd891e7`](https://github.com/homeassistant-ai/ha-mcp/commit/fd891e72811ad4d8a143ebac526093716453d62e))
- Machine-translate locale updates
  ([`4113050`](https://github.com/homeassistant-ai/ha-mcp/commit/41130507f6451f4dae0dcea07b0b10ed63264e52))
- Sync tool docs after merge [skip ci]
  ([`a977fca`](https://github.com/homeassistant-ai/ha-mcp/commit/a977fcaf9d70b08465add7bc9cc6c842426a9d1e))
- Sync tool docs after merge [skip ci]
  ([`63b6cd8`](https://github.com/homeassistant-ai/ha-mcp/commit/63b6cd835bc2d666a763a43305e8a15d6e16c722))
- Sync tool docs after merge [skip ci]
  ([`178b1ce`](https://github.com/homeassistant-ai/ha-mcp/commit/178b1ced740330b2c03c047bf22b4dbf1865496a))
- **deps**: Bump astro from 7.1.0 to 7.2.8 in /site
  ([#2416](https://github.com/homeassistant-ai/ha-mcp/pull/2416))
- **deps**: Bump svgo from 4.0.2 to 4.1.0 in /site
  ([#2415](https://github.com/homeassistant-ai/ha-mcp/pull/2415))
- **deps**: Bump sharp from 0.35.3 to 0.35.4 in /site
  ([#2414](https://github.com/homeassistant-ai/ha-mcp/pull/2414))
- **deps**: Bump js-yaml from 4.3.1 to 4.3.2 in /site
  ([#2413](https://github.com/homeassistant-ai/ha-mcp/pull/2413))
- **deps**: Update uv
  ([#2412](https://github.com/homeassistant-ai/ha-mcp/pull/2412))
- **deps**: Update dependency renovatebot/renovate to v44.56.2
  ([#2411](https://github.com/homeassistant-ai/ha-mcp/pull/2411))
- **deps**: Update dependency renovatebot/renovate to v44.56.1
  ([#2409](https://github.com/homeassistant-ai/ha-mcp/pull/2409))
- **deps**: Update dependency home-assistant/supervisor to v2026.09.0
  ([#2408](https://github.com/homeassistant-ai/ha-mcp/pull/2408))
- **deps**: Update dependency astral-sh/uv to v0.12.8
  ([#2406](https://github.com/homeassistant-ai/ha-mcp/pull/2406))
- **deps**: Update src/ha_mcp/resources/skills-vendor digest to 196c134
  ([#2405](https://github.com/homeassistant-ai/ha-mcp/pull/2405))
- Machine-translate locale updates
  ([`66f1e23`](https://github.com/homeassistant-ai/ha-mcp/commit/66f1e2325667cd3022deea221c0c2220cc9fbb07))
- Sync tool docs after merge [skip ci]
  ([`2830e0b`](https://github.com/homeassistant-ai/ha-mcp/commit/2830e0b07fb5635dad5a88d9f99ab586d58180b8))
- Sync tool docs after merge [skip ci]
  ([`4c39dbc`](https://github.com/homeassistant-ai/ha-mcp/commit/4c39dbcae45eb38fb332a6bab43214a28d59381d))
- Machine-translate locale updates
  ([`4d08bf4`](https://github.com/homeassistant-ai/ha-mcp/commit/4d08bf4276e7bf300d861868ee1d11729d8ec8d3))
- **deps**: Update dependency websockets to v17.1
  ([#2384](https://github.com/homeassistant-ai/ha-mcp/pull/2384))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.12.10
  ([#2381](https://github.com/homeassistant-ai/ha-mcp/pull/2381))
- **deps**: Update python:3.13-slim docker digest to 9d2e555
  ([#2380](https://github.com/homeassistant-ai/ha-mcp/pull/2380))
- **deps**: Update dependency renovatebot/renovate to v44.52.0
  ([#2383](https://github.com/homeassistant-ai/ha-mcp/pull/2383))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.9.1
  ([#2382](https://github.com/homeassistant-ai/ha-mcp/pull/2382))
- Machine-translate locale updates
  ([`7a880a6`](https://github.com/homeassistant-ai/ha-mcp/commit/7a880a6bd4496d8cc57e2c1a9be6253da6a950cd))
- Sync tool docs after merge [skip ci]
  ([`6ac15f4`](https://github.com/homeassistant-ai/ha-mcp/commit/6ac15f4728d9d2a136e54a8ed83f5b84a6a907d7))
- **addon**: Publish version 8.4.3 [skip ci]
  ([`47582a7`](https://github.com/homeassistant-ai/ha-mcp/commit/47582a700b0889a355033ade36de7c3077780b48))

### Continuous Integration

- **deps**: Bump the github-actions group with 3 updates
  ([#2453](https://github.com/homeassistant-ai/ha-mcp/pull/2453))
- Stop caching the beta HAOS image, and keep docs out of the stable image key
  ([#2423](https://github.com/homeassistant-ai/ha-mcp/pull/2423))
- Add reusable Codex action with caller-controlled capabilities
  ([#2339](https://github.com/homeassistant-ai/ha-mcp/pull/2339))
- **deps**: Bump the github-actions group with 2 updates
  ([#2398](https://github.com/homeassistant-ai/ha-mcp/pull/2398))
- Enable Renovate PR checkbox scans and protected auto-merge
  ([#2386](https://github.com/homeassistant-ai/ha-mcp/pull/2386))
- Scan Renovate hourly and track HA stable releases
  ([#2379](https://github.com/homeassistant-ai/ha-mcp/pull/2379))

### Testing

- Consolidate pytest configuration
  ([#2449](https://github.com/homeassistant-ai/ha-mcp/pull/2449))
</details>


## v8.4.3 (2026-09-04)

### Fixed

- **devices**: Support Core 2026.9 child devices and effective areas
  ([#2366](https://github.com/homeassistant-ai/ha-mcp/pull/2366))
- **component**: Prove the LLM-API schema conversion inside Home Assistant and finish #2361
  ([#2365](https://github.com/homeassistant-ai/ha-mcp/pull/2365))
- **component**: Stop an exclusive schema bound breaking every Anthropic turn
  ([#2363](https://github.com/homeassistant-ai/ha-mcp/pull/2363))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Sync tool docs after merge [skip ci]
  ([`aac38ef`](https://github.com/homeassistant-ai/ha-mcp/commit/aac38ef85840f15da81060e24b390b3e6a675489))
- **addon**: Publish version 8.4.2 [skip ci]
  ([`67a52f2`](https://github.com/homeassistant-ai/ha-mcp/commit/67a52f27bfaf34a665f3d2a089ea7f7045ab7dcc))

### Continuous Integration

- Move the E2E Home Assistant image to 2026.9.0
  ([#2364](https://github.com/homeassistant-ai/ha-mcp/pull/2364))
- Add container beta E2E lane and drop the release-age gate on HA images
  ([#2362](https://github.com/homeassistant-ai/ha-mcp/pull/2362))
</details>


## v8.4.2 (2026-09-04)

### Changed

- Update Claude.ai connector setup for the new wizard flow
  ([#2352](https://github.com/homeassistant-ai/ha-mcp/pull/2352))

### Fixed

- **logging**: Stop StartupLogCollector deadlocking Home Assistant
  ([#2360](https://github.com/homeassistant-ai/ha-mcp/pull/2360))
- **visibility**: Honor allowlist authorization
  ([#2345](https://github.com/homeassistant-ai/ha-mcp/pull/2345))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Machine-translate locale updates
  ([`1669d2c`](https://github.com/homeassistant-ai/ha-mcp/commit/1669d2c332acac154f7ee1e6471d3a53c24f1673))
- **deps-dev**: Bump fast-uri from 3.1.5 to 3.1.7 in /site
  ([#2355](https://github.com/homeassistant-ai/ha-mcp/pull/2355))
- **addon**: Publish version 8.4.1 [skip ci]
  ([`10cd3d1`](https://github.com/homeassistant-ai/ha-mcp/commit/10cd3d1207f8270ae6e35c0c40d7fc6dc411e9e3))
</details>


## v8.4.1 (2026-09-02)

### Changed

- **tools**: Document ha_call_service parameter descriptions
  ([#2327](https://github.com/homeassistant-ai/ha-mcp/pull/2327))
- Slim and modularize AGENTS.md
  ([#2323](https://github.com/homeassistant-ai/ha-mcp/pull/2323))

### Fixed

- Extract parameter descriptions for bare Annotated tool signatures
  ([#2328](https://github.com/homeassistant-ai/ha-mcp/pull/2328))
- **oauth**: Serve the protected-resource document only at its path-scoped URL
  ([#2321](https://github.com/homeassistant-ai/ha-mcp/pull/2321))
- Fail fast on a nonexistent or unavailable ha_call_service/ha_bulk_control target
  ([#2319](https://github.com/homeassistant-ai/ha-mcp/pull/2319))
- **search**: Boost ha_manage_energy_prefs for tariff/price queries
  ([#2326](https://github.com/homeassistant-ai/ha-mcp/pull/2326))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Sync tool docs after merge [skip ci]
  ([`4971449`](https://github.com/homeassistant-ai/ha-mcp/commit/4971449d5df532ea9a88436455c506e5bd2e4162))
- **coderabbit**: Disable docstring coverage check
  ([#2348](https://github.com/homeassistant-ai/ha-mcp/pull/2348))
- **deps**: Update python:3.13-slim docker digest to 881d807
  ([#2343](https://github.com/homeassistant-ai/ha-mcp/pull/2343))
- **deps**: Bump browserslist from 4.28.1 to 4.28.8 in /site
  ([#2347](https://github.com/homeassistant-ai/ha-mcp/pull/2347))
- **deps**: Bump postcss-selector-parser from 6.1.2 to 6.1.4 in /site
  ([#2346](https://github.com/homeassistant-ai/ha-mcp/pull/2346))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.12.8
  ([#2344](https://github.com/homeassistant-ai/ha-mcp/pull/2344))
- Sync tool docs after merge [skip ci]
  ([`0d26409`](https://github.com/homeassistant-ai/ha-mcp/commit/0d26409ec216f961524560df846be4cad30b363d))
- Machine-translate locale updates
  ([`b202880`](https://github.com/homeassistant-ai/ha-mcp/commit/b20288095981754b1216a2a1c146d84804c03f54))
- **addon**: Publish version 8.4.0 [skip ci]
  ([`ad122e4`](https://github.com/homeassistant-ai/ha-mcp/commit/ad122e4787ab2a184e8140fb2fe30a70b1e38306))

### Continuous Integration

- Fold the CodeQL matrix and its gate into one job
  ([#2340](https://github.com/homeassistant-ai/ha-mcp/pull/2340))
- Run beta HAOS E2E on user-originated master pushes and nightly
  ([#2338](https://github.com/homeassistant-ai/ha-mcp/pull/2338))
- **deps**: Bump renovatebot/github-action in the github-actions group
  ([#2337](https://github.com/homeassistant-ai/ha-mcp/pull/2337))
- Cancel superseded PR runs instead of letting them finish
  ([#2325](https://github.com/homeassistant-ai/ha-mcp/pull/2325))
- Fold the seven fast checks into one Fast Checks lane
  ([#2314](https://github.com/homeassistant-ai/ha-mcp/pull/2314))

### Testing

- Key the non-topology e2e exclusion by job rather than by workflow
  ([#2342](https://github.com/homeassistant-ai/ha-mcp/pull/2342))
</details>


## v8.4.0 (2026-08-29)

### Added

- **lite-docstrings**: Cover ha_manage_backup and ha_report_issue
  ([#2153](https://github.com/homeassistant-ai/ha-mcp/pull/2153))
- Add Klingon localization
  ([#2056](https://github.com/homeassistant-ai/ha-mcp/pull/2056))
- **search**: Use Home Assistant's reference graph for entity dependency discovery
  ([#2277](https://github.com/homeassistant-ai/ha-mcp/pull/2277))
- **screenshot**: Report theme changes instead of writing them back
  ([#2265](https://github.com/homeassistant-ai/ha-mcp/pull/2265))
- **bulk**: Add deterministic structural selectors
  ([#2246](https://github.com/homeassistant-ai/ha-mcp/pull/2246))
- **i18n**: Add Korean (ko) translations
  ([#2251](https://github.com/homeassistant-ai/ha-mcp/pull/2251))
- **search**: Expose explicit entity membership
  ([#2244](https://github.com/homeassistant-ai/ha-mcp/pull/2244))

### Changed

- Clarify custom filesystem path settings
  ([#2310](https://github.com/homeassistant-ai/ha-mcp/pull/2310))
- Cross-reference ha_get_dashboard_screenshot from dashboard config screenshot params
  ([#2295](https://github.com/homeassistant-ai/ha-mcp/pull/2295))
- Clarify exact filesystem allowlist paths
  ([#2271](https://github.com/homeassistant-ai/ha-mcp/pull/2271))

### Fixed

- Allow custom dashboard screenshot dimensions
  ([#2305](https://github.com/homeassistant-ai/ha-mcp/pull/2305))
- Cover no-tools-entry installs end to end and complete the two-entry component contract
  ([#2302](https://github.com/homeassistant-ai/ha-mcp/pull/2302))
- Keep ha_search on the component fast path with dashboard search_types and on server-entry-only installs
  ([#2291](https://github.com/homeassistant-ai/ha-mcp/pull/2291))
- **settings-ui**: Repaint all flag switches after an ambiguous save; close out the #2153 review deferrals
  ([#2296](https://github.com/homeassistant-ai/ha-mcp/pull/2296))
- Bound error_log fetches with paginated journald windows
  ([#2290](https://github.com/homeassistant-ai/ha-mcp/pull/2290))
- **component**: Give the loopback MCP session an actual generous timeout
  ([#2276](https://github.com/homeassistant-ai/ha-mcp/pull/2276))
- Bound supervisor log fetch time
  ([#2281](https://github.com/homeassistant-ai/ha-mcp/pull/2281))
- **embedded**: Support Probatio schema conversion
  ([#2286](https://github.com/homeassistant-ai/ha-mcp/pull/2286))
- **addon**: Bound Supervisor error payloads reaching the model
  ([#2280](https://github.com/homeassistant-ai/ha-mcp/pull/2280))
- Demote disconnect-caused stateless session crash logs
  ([#2257](https://github.com/homeassistant-ai/ha-mcp/pull/2257))
- **addon**: Restore Supervisor app management
  ([#2274](https://github.com/homeassistant-ai/ha-mcp/pull/2274))
- Stop partial options-flow submits from wiping unnamed fields
  ([#2256](https://github.com/homeassistant-ai/ha-mcp/pull/2256))
- **component**: Respect Home Assistant skip_pip
  ([#2275](https://github.com/homeassistant-ai/ha-mcp/pull/2275))
- **component**: Make loopback and multi-origin DCR clients refreshable in ha_auth mode
  ([#2249](https://github.com/homeassistant-ai/ha-mcp/pull/2249))
- Name the dependency conflict behind embedded-server import failures
  ([#2245](https://github.com/homeassistant-ai/ha-mcp/pull/2245))
- Honor TLS settings in app proxy
  ([#2242](https://github.com/homeassistant-ai/ha-mcp/pull/2242))
- **search**: Floor-area and bulk-control hardening follow-ups
  ([#2243](https://github.com/homeassistant-ai/ha-mcp/pull/2243))
- **search**: Resolve floor names in area filters
  ([#2235](https://github.com/homeassistant-ai/ha-mcp/pull/2235))
- Tighten HAOS cache guards and visibility logs
  ([#2240](https://github.com/homeassistant-ai/ha-mcp/pull/2240))
- Suppress redundant managed settings URL hints
  ([#2238](https://github.com/homeassistant-ai/ha-mcp/pull/2238))

---
<details>
<summary>Internal Changes</summary>


### Changed

- Correct the reference-graph ranking comment
  ([#2285](https://github.com/homeassistant-ai/ha-mcp/pull/2285))
- Drop hardcoded home path from bat-story-eval skill
  ([#2253](https://github.com/homeassistant-ai/ha-mcp/pull/2253))
- **tests**: Correct the e2e expected-failure helper guidance
  ([#2252](https://github.com/homeassistant-ai/ha-mcp/pull/2252))

### Fixed

- **ci**: Stop Renovate silently aborting its whole run on every branch
  ([#2283](https://github.com/homeassistant-ai/ha-mcp/pull/2283))

### Chores

- Machine-translate locale updates
  ([`f237986`](https://github.com/homeassistant-ai/ha-mcp/commit/f2379861c6e43c399bc7eb7523bbe93771167b19))
- Sync tool docs after merge [skip ci]
  ([`07d537b`](https://github.com/homeassistant-ai/ha-mcp/commit/07d537b02efc22ec534da4da517f95d54202f5f5))
- Machine-translate locale updates
  ([`d202dbd`](https://github.com/homeassistant-ai/ha-mcp/commit/d202dbdae6e4ebe18e857e9a30ecda3fb4a83c84))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.8.3
  ([#2299](https://github.com/homeassistant-ai/ha-mcp/pull/2299))
- **deps**: Update dependency renovatebot/renovate to v44.50.1
  ([#2300](https://github.com/homeassistant-ai/ha-mcp/pull/2300))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.12.7
  ([#2301](https://github.com/homeassistant-ai/ha-mcp/pull/2301))
- **deps**: Update src/ha_mcp/resources/skills-vendor digest to d0c6129
  ([#2298](https://github.com/homeassistant-ai/ha-mcp/pull/2298))
- **deps**: Update python:3.13-slim docker digest to 7ce4b6d
  ([#2297](https://github.com/homeassistant-ai/ha-mcp/pull/2297))
- Sync tool docs after merge [skip ci]
  ([`ba9cf61`](https://github.com/homeassistant-ai/ha-mcp/commit/ba9cf612549a72db084d6b66ea6a66939a952997))
- Sync tool docs after merge [skip ci]
  ([`0e17b7e`](https://github.com/homeassistant-ai/ha-mcp/commit/0e17b7e23136955e21ad569a8953f7cec3081e9c))
- Machine-translate locale updates
  ([`c501a53`](https://github.com/homeassistant-ai/ha-mcp/commit/c501a5313396544e3ec017d98e5cde07e675f891))
- **deps**: Update python:3.13-slim docker digest to 7e3a6ac
  ([#2282](https://github.com/homeassistant-ai/ha-mcp/pull/2282))
- Sync tool docs after merge [skip ci]
  ([`4098dc1`](https://github.com/homeassistant-ai/ha-mcp/commit/4098dc1410719c5d49529c974027d0a4f1a10662))
- Sync tool docs after merge [skip ci]
  ([`00df2aa`](https://github.com/homeassistant-ai/ha-mcp/commit/00df2aabd47faf53170f03886c2b05d1178e35eb))
- Sync tool docs after merge [skip ci]
  ([`db827e0`](https://github.com/homeassistant-ai/ha-mcp/commit/db827e0f994bd20fdedc79e9c9fa46ee4ca156a6))
- Sync tool docs after merge [skip ci]
  ([`e9a92c1`](https://github.com/homeassistant-ai/ha-mcp/commit/e9a92c1b487bc0ace54e91185356888da8c9752b))
- Machine-translate locale updates
  ([`939913a`](https://github.com/homeassistant-ai/ha-mcp/commit/939913aba54a8d457df9773699ed44df94287861))
- **addon**: Promote webhook-proxy dev -> stable 3.0.2
  ([#2269](https://github.com/homeassistant-ai/ha-mcp/pull/2269))
- Machine-translate locale updates
  ([`6918cbd`](https://github.com/homeassistant-ai/ha-mcp/commit/6918cbdcc3a80c2a7c81e7e7967d7f5a785c2e74))
- Sync tool docs after merge [skip ci]
  ([`f747d06`](https://github.com/homeassistant-ai/ha-mcp/commit/f747d069f48b68a8458a971dbe9f58c8128c0489))
- Sync tool docs after merge [skip ci]
  ([`bf61660`](https://github.com/homeassistant-ai/ha-mcp/commit/bf6166081bc62070812bfd4a637e8c0f35632116))
- Sync tool docs after merge [skip ci]
  ([`08d9f0d`](https://github.com/homeassistant-ai/ha-mcp/commit/08d9f0dc588c2daaa55ec817e07f3252839b9c51))
- Machine-translate locale updates
  ([`a5f1fac`](https://github.com/homeassistant-ai/ha-mcp/commit/a5f1fac7db3b3f6dadd8da5e51aa0b5179005105))
- Sync tool docs after merge [skip ci]
  ([`68f7466`](https://github.com/homeassistant-ai/ha-mcp/commit/68f74666fc368cd51f5b069e4946b7e83cedf059))
- Sync tool docs after merge [skip ci]
  ([`2bb720f`](https://github.com/homeassistant-ai/ha-mcp/commit/2bb720f62c53c70c868bc8333c82a3bd5ca1501e))
- Sync tool docs after merge [skip ci]
  ([`df70a00`](https://github.com/homeassistant-ai/ha-mcp/commit/df70a00a6c03d700effedd04220535a3d5883a88))
- Machine-translate locale updates
  ([`8f76ef2`](https://github.com/homeassistant-ai/ha-mcp/commit/8f76ef2b62258130917835c5fac9d2006541f5d7))
- **addon**: Publish version 8.3.0 [skip ci]
  ([`8ed7f47`](https://github.com/homeassistant-ai/ha-mcp/commit/8ed7f47105e7902ded6fa61d30f2a7f926e68f2a))

### Continuous Integration

- **deps**: Bump the github-actions group with 2 updates
  ([#2264](https://github.com/homeassistant-ai/ha-mcp/pull/2264))

### Testing

- Pin the probe memo's concurrency contract and close the lane-table drift gap
  ([#2309](https://github.com/homeassistant-ai/ha-mcp/pull/2309))
</details>


## v8.3.0 (2026-08-19)

### Added

- Cover stdio visibility and preserve issue reports
  ([#2233](https://github.com/homeassistant-ai/ha-mcp/pull/2233))
- Rename the add-on tools to ha_get_app / ha_manage_app and finish the App wording
  ([#2222](https://github.com/homeassistant-ai/ha-mcp/pull/2222))
- Reconfigure existing Home Assistant integrations safely
  ([#2149](https://github.com/homeassistant-ai/ha-mcp/pull/2149))
- Show installation method in settings footer
  ([#2231](https://github.com/homeassistant-ai/ha-mcp/pull/2231))
- Add process action to ha_manage_pipeline
  ([#2215](https://github.com/homeassistant-ai/ha-mcp/pull/2215))
- **webhook-proxy**: Mirror the unified OAuth surface onto the dev flavor and gate it on backend liveness
  ([#2218](https://github.com/homeassistant-ai/ha-mcp/pull/2218))
- OAuth follow-up — component 2.0.0, approved review fold-ins, multi-origin registrations for Google Spark
  ([#2217](https://github.com/homeassistant-ai/ha-mcp/pull/2217))
- Claude-proof OAuth surface — unified endpoints, in-component CIMD validation, DCR compat
  ([#2213](https://github.com/homeassistant-ai/ha-mcp/pull/2213))
- **site**: Add Hermes Agent support to setup wizard
  ([#2208](https://github.com/homeassistant-ai/ha-mcp/pull/2208))
- Add the Czech (cs) locale across all four translated surfaces
  ([#2207](https://github.com/homeassistant-ai/ha-mcp/pull/2207))
- **logs**: Add structured summary mode to ha_get_logs(source='error_log')
  ([#2107](https://github.com/homeassistant-ai/ha-mcp/pull/2107))
- Add the Esperanto (eo) locale across all four translated surfaces
  ([#2179](https://github.com/homeassistant-ai/ha-mcp/pull/2179))

### Changed

- Finish the app wording on the README settings-UI line
  ([`b51bf22`](https://github.com/homeassistant-ai/ha-mcp/commit/b51bf22685dbf9a5f472309e7ae70cf8113dcce3))
- Clarify HACS pre-release selection
  ([#2227](https://github.com/homeassistant-ai/ha-mcp/pull/2227))

### Fixed

- Explain how to disable startup notification
  ([#2230](https://github.com/homeassistant-ai/ha-mcp/pull/2230))
- Leg-consistent refresh comparison, DCR nesting hardening, raw Basic credentials
  ([#2219](https://github.com/homeassistant-ai/ha-mcp/pull/2219))
- Add restart action to legacy OAuth repair
  ([#2211](https://github.com/homeassistant-ai/ha-mcp/pull/2211))
- Support history_stats and mold_indicator helpers in ha_config_set_helper
  ([#2206](https://github.com/homeassistant-ai/ha-mcp/pull/2206))
- Handle null trigger states in automation traces
  ([#2203](https://github.com/homeassistant-ai/ha-mcp/pull/2203))
- Repair locale divergences from the English source and guard the class in CI
  ([#2180](https://github.com/homeassistant-ai/ha-mcp/pull/2180))
- Support opaque access tokens in OIDC mode
  ([#2194](https://github.com/homeassistant-ai/ha-mcp/pull/2194))
- Survive a rich older than fastmcp's logging setup expects
  ([#2195](https://github.com/homeassistant-ai/ha-mcp/pull/2195))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Reuse shared locale translations and update Gemini model
  ([#2232](https://github.com/homeassistant-ai/ha-mcp/pull/2232))

### Chores

- **addon**: Promote webhook-proxy dev -> stable 3.0.1
  ([#2234](https://github.com/homeassistant-ai/ha-mcp/pull/2234))
- Machine-translate locale updates
  ([`083ebc1`](https://github.com/homeassistant-ai/ha-mcp/commit/083ebc175125a1f2ec6a3c901fcf3bb34b921818))
- Machine-translate locale updates
  ([`8963ff6`](https://github.com/homeassistant-ai/ha-mcp/commit/8963ff67c5301b8a5696e786bae3c7ad38535847))
- Sync tool docs after merge [skip ci]
  ([`7421a6b`](https://github.com/homeassistant-ai/ha-mcp/commit/7421a6b701dbc7f900d1fa25f541fa1bc66c5846))
- **addon**: Promote webhook-proxy dev -> stable 3.0.0
  ([#2221](https://github.com/homeassistant-ai/ha-mcp/pull/2221))
- Sync tool docs after merge [skip ci]
  ([`74144c7`](https://github.com/homeassistant-ai/ha-mcp/commit/74144c7ab520ebbb9311baa7d34ab8157af196f7))
- Machine-translate locale updates
  ([`f594e79`](https://github.com/homeassistant-ai/ha-mcp/commit/f594e795ee7292011bb0e2eb026c2633aca0787c))
- Machine-translate locale updates
  ([`b6d8201`](https://github.com/homeassistant-ai/ha-mcp/commit/b6d8201e586af5f57215023d10c4236cc73a2829))
- Sync tool docs after merge [skip ci]
  ([`163e736`](https://github.com/homeassistant-ai/ha-mcp/commit/163e73610a88e4a6906b1b14e3ef33c1c12b22c9))
- Machine-translate locale updates
  ([`c8a1b31`](https://github.com/homeassistant-ai/ha-mcp/commit/c8a1b31909e794e9900f3e2d6488b9b3b9b13b7c))
- Sync tool docs after merge [skip ci]
  ([`4c310c4`](https://github.com/homeassistant-ai/ha-mcp/commit/4c310c4ea0422ea924b1208ace4c86d53fc56277))
- Machine-translate locale updates
  ([`f59e548`](https://github.com/homeassistant-ai/ha-mcp/commit/f59e5480088a0488990ae25a185828135c541ae9))
- **addon**: Publish version 8.2.0 [skip ci]
  ([`767dc9d`](https://github.com/homeassistant-ai/ha-mcp/commit/767dc9de45302ab2efbddefdc55ea84f20ed446c))

### Continuous Integration

- **deps**: Bump renovatebot/github-action in the github-actions group
  ([#2226](https://github.com/homeassistant-ai/ha-mcp/pull/2226))
- Harden Renovate follow-up behavior
  ([#2202](https://github.com/homeassistant-ai/ha-mcp/pull/2202))
- Restore Renovate updates and vulnerability-alert access
  ([#2200](https://github.com/homeassistant-ai/ha-mcp/pull/2200))
- Harden dependency supply chain
  ([#2196](https://github.com/homeassistant-ai/ha-mcp/pull/2196))
- **deps**: Bump renovatebot/github-action in the github-actions group
  ([#2193](https://github.com/homeassistant-ai/ha-mcp/pull/2193))

### Testing

- Generalize PR checkout credential guard
  ([#2199](https://github.com/homeassistant-ai/ha-mcp/pull/2199))
</details>


## v8.2.0 (2026-08-10)

### Added

- Add the Swedish (sv) locale across all four translated surfaces
  ([#2173](https://github.com/homeassistant-ai/ha-mcp/pull/2173))
- Add the Polish (pl) locale across all four translated surfaces
  ([#2171](https://github.com/homeassistant-ai/ha-mcp/pull/2171))
- Automatically surface component updates in HACS after a server update
  ([#2166](https://github.com/homeassistant-ai/ha-mcp/pull/2166))
- Add update_information action to ha_manage_hacs
  ([#2165](https://github.com/homeassistant-ai/ha-mcp/pull/2165))
- Add redact_secrets toggle to redact secrets from tool responses
  ([#2164](https://github.com/homeassistant-ai/ha-mcp/pull/2164))
- Add ha_manage_security_policy tool behind Policies-tab toggle
  ([#2155](https://github.com/homeassistant-ai/ha-mcp/pull/2155))
- Add the Dutch (nl) locale across all four translated surfaces
  ([#2145](https://github.com/homeassistant-ai/ha-mcp/pull/2145))
- Add security policy access toggle for dev tools
  ([#2147](https://github.com/homeassistant-ai/ha-mcp/pull/2147))

### Fixed

- Route the embedded self-reload through the shielded services endpoint
  ([#2184](https://github.com/homeassistant-ai/ha-mcp/pull/2184))
- Allow updates to existing hyphenless dashboards
  ([#2181](https://github.com/homeassistant-ai/ha-mcp/pull/2181))
- Schedule the HACS refresh nudge via the FastMCP lifespan
  ([#2172](https://github.com/homeassistant-ai/ha-mcp/pull/2172))
- Keep hardcoded config-entry titles untranslated too
  ([#2175](https://github.com/homeassistant-ai/ha-mcp/pull/2175))
- Keep hardcoded config-flow labels untranslated
  ([#2170](https://github.com/homeassistant-ai/ha-mcp/pull/2170))
- Reject dangling registry references across all registry write tools
  ([#2162](https://github.com/homeassistant-ai/ha-mcp/pull/2162))
- Reject blank locale catalog values instead of rendering them
  ([#2163](https://github.com/homeassistant-ai/ha-mcp/pull/2163))
- Validate entity area IDs
  ([#2160](https://github.com/homeassistant-ai/ha-mcp/pull/2160))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Close truncated release-note fences correctly and surface empty extracts
  ([#2156](https://github.com/homeassistant-ai/ha-mcp/pull/2156))
- Cap release notes at GitHub's 125k body limit
  ([#2154](https://github.com/homeassistant-ai/ha-mcp/pull/2154))

### Chores

- Machine-translate locale updates
  ([`5db8bf8`](https://github.com/homeassistant-ai/ha-mcp/commit/5db8bf80bb8f58488c61426064181755d50680da))
- Machine-translate locale updates
  ([`18fc429`](https://github.com/homeassistant-ai/ha-mcp/commit/18fc429abf494100415e5444a40a17d033669810))
- Machine-translate locale updates
  ([`b481929`](https://github.com/homeassistant-ai/ha-mcp/commit/b48192933590bdc9bea917d07ce5caaeb312f87e))
- Sync tool docs after merge [skip ci]
  ([`6a5a993`](https://github.com/homeassistant-ai/ha-mcp/commit/6a5a99324e519705197d1ba2361372dfb03a3b1a))
- Enable CodeRabbit auto-review on non-default base branches
  ([#2168](https://github.com/homeassistant-ai/ha-mcp/pull/2168))
- Sync tool docs after merge [skip ci]
  ([`7f5d59c`](https://github.com/homeassistant-ai/ha-mcp/commit/7f5d59cc0448af172002193a4f86906bc6bbe10d))
- Machine-translate locale updates
  ([`306fada`](https://github.com/homeassistant-ai/ha-mcp/commit/306fada4bde42623216a11c2461483dc9e76ca5e))
- Sync tool docs after merge [skip ci]
  ([`4bef86a`](https://github.com/homeassistant-ai/ha-mcp/commit/4bef86acdb110c8eff4db76662ad594501acc27f))
- **deps**: Bump js-yaml from 4.2.0 to 4.3.1 in /site
  ([#2161](https://github.com/homeassistant-ai/ha-mcp/pull/2161))
- **addon**: Publish version 8.1.1 [skip ci]
  ([`a66c87b`](https://github.com/homeassistant-ai/ha-mcp/commit/a66c87b913c24f39a7cce771d0bd2d8452eb4f26))
</details>


## v8.1.1 (2026-08-06)

### Changed

- Name the 409 host sentence among the strings a catalog owes
  ([#2151](https://github.com/homeassistant-ai/ha-mcp/pull/2151))
- Correct the meta-only stub claim in the add-a-language procedure
  ([#2144](https://github.com/homeassistant-ai/ha-mcp/pull/2144))

### Fixed

- Vendor websockets and stop force-replacing HA's shipped packages
  ([#2150](https://github.com/homeassistant-ai/ha-mcp/pull/2150))

---
<details>
<summary>Internal Changes</summary>


### Chores

- **addon**: Publish version 8.1.0 [skip ci]
  ([`6213fc8`](https://github.com/homeassistant-ai/ha-mcp/commit/6213fc8047171a2731af6299f9bcecd73e96fcad))
</details>


## v8.1.0 (2026-08-05)

### Added

- Land machine translations post-merge instead of in the PR
  ([#2115](https://github.com/homeassistant-ai/ha-mcp/pull/2115))
- **site**: Add shareable setup wizard URLs
  ([#2117](https://github.com/homeassistant-ai/ha-mcp/pull/2117))

### Changed

- Document Webhook Proxy ha_auth as an access gate, not per-user authorization
  ([#2140](https://github.com/homeassistant-ai/ha-mcp/pull/2140))
- Document settings-UI and OIDC trust properties in SECURITY.md
  ([#2136](https://github.com/homeassistant-ai/ha-mcp/pull/2136))

### Fixed

- Observe the shutdown cleanup task result
  ([#2139](https://github.com/homeassistant-ai/ha-mcp/pull/2139))
- Cancel pending worker-loop tasks before embedded teardown
  ([#2128](https://github.com/homeassistant-ai/ha-mcp/pull/2128))
- Drive flows that revisit menu steps via successive selection lists
  ([#2124](https://github.com/homeassistant-ai/ha-mcp/pull/2124))
- Replace the stdio settings sidecar on every startup and keep its URL stable
  ([#2134](https://github.com/homeassistant-ai/ha-mcp/pull/2134))
- Clarify the addon-not-back error message
  ([#2132](https://github.com/homeassistant-ai/ha-mcp/pull/2132))
- Anchor the translation prompt to each catalog's address register
  ([#2110](https://github.com/homeassistant-ai/ha-mcp/pull/2110))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Machine-translate locale updates
  ([`ee165e8`](https://github.com/homeassistant-ai/ha-mcp/commit/ee165e81d438f31fe0c47d9399141e4d96ee3283))
- Sync tool docs after merge [skip ci]
  ([`8cc8398`](https://github.com/homeassistant-ai/ha-mcp/commit/8cc839810c4db42e000e9daebc191029d18e22b1))
- **deps**: Update dependency home-assistant/operating-system to v18.2
  ([#2138](https://github.com/homeassistant-ai/ha-mcp/pull/2138))
- **deps**: Update src/ha_mcp/resources/skills-vendor digest to 9e4eff2
  ([#2137](https://github.com/homeassistant-ai/ha-mcp/pull/2137))
- Machine-translate locale updates
  ([`d1f74e5`](https://github.com/homeassistant-ai/ha-mcp/commit/d1f74e5934b63ab23f4369d1b5c2658615b46aa7))
- Replace the retired triage bot with CodeRabbit issue enrichment
  ([#2125](https://github.com/homeassistant-ai/ha-mcp/pull/2125))
- Let CodeRabbit review draft PRs
  ([#2118](https://github.com/homeassistant-ai/ha-mcp/pull/2118))
- **deps-dev**: Bump fast-uri from 3.1.4 to 3.1.5 in /site
  ([#2130](https://github.com/homeassistant-ai/ha-mcp/pull/2130))
- **deps**: Bump postcss from 8.5.21 to 8.5.25 in /site
  ([#2129](https://github.com/homeassistant-ai/ha-mcp/pull/2129))
- **addon**: Publish version 8.0.0 [skip ci]
  ([`c6eacb0`](https://github.com/homeassistant-ai/ha-mcp/commit/c6eacb0a7845d559050806ed24de25c0c69f3c06))

### Continuous Integration

- **deps**: Bump the github-actions group with 5 updates
  ([#2123](https://github.com/homeassistant-ai/ha-mcp/pull/2123))
</details>


## v8.0.0 (2026-08-02)

### Added

- Warn when a variables key reads a later key in the same block
  ([#2084](https://github.com/homeassistant-ai/ha-mcp/pull/2084))
- Single-source translations with generated catalogs and automatic retranslation
  ([#2095](https://github.com/homeassistant-ai/ha-mcp/pull/2095))
- Add the Italian (it) locale across all four translated surfaces
  ([#2062](https://github.com/homeassistant-ai/ha-mcp/pull/2062))
- Embed inline dashboard resources as data: URIs instead of the Cloudflare worker
  ([#2069](https://github.com/homeassistant-ai/ha-mcp/pull/2069))
- Add the Spanish (es) locale across all four translated surfaces
  ([#2055](https://github.com/homeassistant-ai/ha-mcp/pull/2055))
- Complete the Simplified Chinese (zh-Hans) locale and enforce locale parity
  ([#2042](https://github.com/homeassistant-ai/ha-mcp/pull/2042))
- Remove ha-mcp-sse entry point
  ([#2032](https://github.com/homeassistant-ai/ha-mcp/pull/2032))
- Include RFC 9207 iss parameter in OAuth authorization responses
  ([#2033](https://github.com/homeassistant-ai/ha-mcp/pull/2033))
- Rework tool security policy semantics (ANY-match) + developer tools to drive the settings UI
  ([#1993](https://github.com/homeassistant-ai/ha-mcp/pull/1993))
- Add opt-in enforce mode to the entity visibility filter
  ([#2024](https://github.com/homeassistant-ai/ha-mcp/pull/2024))
- Let operators extend the YAML write allowlist behind a deny floor
  ([#1997](https://github.com/homeassistant-ai/ha-mcp/pull/1997))
- Allow state_filter-only ha_search to enumerate entities by state
  ([#2018](https://github.com/homeassistant-ai/ha-mcp/pull/2018))

### Changed

- Warn Tailscale Funnel users that Claude.ai connectors need port 443 (#2080)
  ([#2094](https://github.com/homeassistant-ai/ha-mcp/pull/2094))
- Self-host the README star history chart
  ([#2086](https://github.com/homeassistant-ai/ha-mcp/pull/2086))
- Make the add-a-language procedure match what CI enforces
  ([#2074](https://github.com/homeassistant-ai/ha-mcp/pull/2074))
- Pin mcp<2.0.0 in the mcp-proxy configs we generate
  ([#2075](https://github.com/homeassistant-ai/ha-mcp/pull/2075))
- Correct pinned-tool docs — mandatory tools cannot be unpinned
  ([#2061](https://github.com/homeassistant-ai/ha-mcp/pull/2061))
- Document ChatGPT stale connector tool list workaround
  ([#2017](https://github.com/homeassistant-ai/ha-mcp/pull/2017))

### Fixed

- Read only the project .env, and close the CI lint-scope gaps from the #2102 review
  ([#2105](https://github.com/homeassistant-ai/ha-mcp/pull/2105))
- Report per-item failures in bulk operation status instead of aborting the batch
  ([#2076](https://github.com/homeassistant-ai/ha-mcp/pull/2076))
- Resubmit config keys redeclared by later flow steps
  ([#2087](https://github.com/homeassistant-ai/ha-mcp/pull/2087))
- Return service_response once at top level in ha_call_service
  ([#2088](https://github.com/homeassistant-ai/ha-mcp/pull/2088))
- Swap the stdio bridge from mcp-proxy to fastmcp-remote
  ([#2101](https://github.com/homeassistant-ai/ha-mcp/pull/2101))
- Make Docker settings persist across container re-creation
  ([#2079](https://github.com/homeassistant-ai/ha-mcp/pull/2079))
- Reword tool search warnings to target clients, not Claude models
  ([#2077](https://github.com/homeassistant-ai/ha-mcp/pull/2077))
- Pin the English tool texts that translations are written against
  ([#2064](https://github.com/homeassistant-ai/ha-mcp/pull/2064))
- Make one English string read the same on every translated surface
  ([#2063](https://github.com/homeassistant-ai/ha-mcp/pull/2063))
- Offload secrets.yaml masking to the executor
  ([#2068](https://github.com/homeassistant-ai/ha-mcp/pull/2068))
- Reject cross-panel links that point at a tab which does not exist
  ([#2046](https://github.com/homeassistant-ai/ha-mcp/pull/2046))
- Propagate cancellation in deep-search fan-outs and usage logging
  ([#2045](https://github.com/homeassistant-ai/ha-mcp/pull/2045))
- Resolve a bracketed YAML path as both pattern and literal
  ([#2036](https://github.com/homeassistant-ai/ha-mcp/pull/2036))
- Standard-mode console logging + relay-stub upstream contract pins
  ([#2039](https://github.com/homeassistant-ai/ha-mcp/pull/2039))
- Promote-flow dev version rebase and e2e entry-setup gate
  ([#2040](https://github.com/homeassistant-ai/ha-mcp/pull/2040))
- Retry Supervisor add-on calls on job-group collisions
  ([#2041](https://github.com/homeassistant-ai/ha-mcp/pull/2041))
- Create all-day calendar events from date-only values
  ([#2035](https://github.com/homeassistant-ai/ha-mcp/pull/2035))
- Drop wall-clock total from MCP relay stream timeouts
  ([#2034](https://github.com/homeassistant-ai/ha-mcp/pull/2034))
- Dashboard search stays on legacy path without false partial
  ([#2020](https://github.com/homeassistant-ai/ha-mcp/pull/2020))
- Force-exit the stdio entry point to avoid SIGABRT at shutdown
  ([#2029](https://github.com/homeassistant-ai/ha-mcp/pull/2029))
- Report Hue/YAML scenes as CONFIG_NOT_FOUND, not ENTITY_NOT_FOUND
  ([#2003](https://github.com/homeassistant-ai/ha-mcp/pull/2003))
- Clarify missing "File & YAML Tools" entry in error text and options UI
  ([#2019](https://github.com/homeassistant-ai/ha-mcp/pull/2019))
- Honor view_path scoping in ha_config_get_dashboard get mode
  ([#2023](https://github.com/homeassistant-ai/ha-mcp/pull/2023))
- Restore read-only annotations on the dashboard get and screenshot tools
  ([#2014](https://github.com/homeassistant-ai/ha-mcp/pull/2014))
- Prevent overlapping overview pages
  ([#2009](https://github.com/homeassistant-ai/ha-mcp/pull/2009))
- Keep the WebSocket pool usable after an event-loop change
  ([#2001](https://github.com/homeassistant-ai/ha-mcp/pull/2001))

---
<details>
<summary>Internal Changes</summary>


### Changed

- Add auth/ package to the architecture tree
  ([#2012](https://github.com/homeassistant-ai/ha-mcp/pull/2012))
- Drop two orphaned locale keys and pin the direction that let them in
  ([#2082](https://github.com/homeassistant-ai/ha-mcp/pull/2082))
- Remove dead code and stale references
  ([#2043](https://github.com/homeassistant-ai/ha-mcp/pull/2043))

### Fixed

- Authenticate star history generation
  ([#2098](https://github.com/homeassistant-ai/ha-mcp/pull/2098))
- Hold pydantic-monty at 0.0.18 and satisfy ruff 0.16 LOG004
  ([#2054](https://github.com/homeassistant-ai/ha-mcp/pull/2054))
- Bound each addon HA-link probe by the readiness deadline
  ([#2028](https://github.com/homeassistant-ai/ha-mcp/pull/2028))

### Chores

- Sync tool docs after merge [skip ci]
  ([`7afe5eb`](https://github.com/homeassistant-ai/ha-mcp/commit/7afe5eb0df57ee2b3a57cdd6cd8626c35a2b7ad2))
- Sync tool docs after merge [skip ci]
  ([`56f95c5`](https://github.com/homeassistant-ai/ha-mcp/commit/56f95c5bbe7b825a12ac458c2f99ab3ce9961aa3))
- Enable targeted ruff pylint rules, fix 104 violations
  ([#2102](https://github.com/homeassistant-ai/ha-mcp/pull/2102))
- Sync tool docs after merge [skip ci]
  ([`c30ffb1`](https://github.com/homeassistant-ai/ha-mcp/commit/c30ffb1d4878a5d970521ebf785c624cc15eb09a))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.33
  ([#2070](https://github.com/homeassistant-ai/ha-mcp/pull/2070))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.7.4
  ([#2071](https://github.com/homeassistant-ai/ha-mcp/pull/2071))
- Sync tool docs after merge [skip ci]
  ([`ba8249e`](https://github.com/homeassistant-ai/ha-mcp/commit/ba8249e847b1203efa006b066610792e9e8a3ea4))
- **addon**: Promote webhook-proxy dev -> stable 2.1.0
  ([#2037](https://github.com/homeassistant-ai/ha-mcp/pull/2037))
- Sync tool docs after merge [skip ci]
  ([`225575c`](https://github.com/homeassistant-ai/ha-mcp/commit/225575cddb681cad6eebe4e63f15bd1810c39a4d))
- Sync tool docs after merge [skip ci]
  ([`6fe0714`](https://github.com/homeassistant-ai/ha-mcp/commit/6fe071455b6bc1985b9cb83dc4da25a18e3e0f0a))
- Sync tool docs after merge [skip ci]
  ([`f8ef5f9`](https://github.com/homeassistant-ai/ha-mcp/commit/f8ef5f9c59deef56a3d4d8ef29afe7579c2a418d))
- Sync tool docs after merge [skip ci]
  ([`bb93bee`](https://github.com/homeassistant-ai/ha-mcp/commit/bb93bee17719659ac3c99549ba0440b45b654ec2))
- **deps-dev**: Bump fast-uri from 3.1.2 to 3.1.4 in /site
  ([#2005](https://github.com/homeassistant-ai/ha-mcp/pull/2005))
- **deps**: Bump svgo from 4.0.1 to 4.0.2 in /site
  ([#2006](https://github.com/homeassistant-ai/ha-mcp/pull/2006))
- **deps**: Bump sharp from 0.34.5 to 0.35.3 in /site
  ([#2004](https://github.com/homeassistant-ai/ha-mcp/pull/2004))
- **addon**: Publish version 7.14.2 [skip ci]
  ([`c435dcb`](https://github.com/homeassistant-ai/ha-mcp/commit/c435dcb866a617da44e0527e0f4feca3b0612822))

### Continuous Integration

- **deps**: Bump the github-actions group with 4 updates
  ([#2053](https://github.com/homeassistant-ai/ha-mcp/pull/2053))

### Testing

- Pin the mixed-entities status gate and search fallbacks
  ([#2104](https://github.com/homeassistant-ai/ha-mcp/pull/2104))
- Resolve HAOS addon container names across the app_ prefix rename
  ([#2092](https://github.com/homeassistant-ai/ha-mcp/pull/2092))
- Gate inaddon setup on the addon's Home Assistant link
  ([#2025](https://github.com/homeassistant-ai/ha-mcp/pull/2025))
</details>


## v7.14.2 (2026-07-22)

### Fixed

- Tolerate setuptools editable-finder KeyError on Python 3.14 bring-up
  ([#1989](https://github.com/homeassistant-ai/ha-mcp/pull/1989))
- Allow reading blueprints/ by default (#1965)
  ([#1988](https://github.com/homeassistant-ai/ha-mcp/pull/1988))
- None-mode auto-approve setup must fail open
  ([#1984](https://github.com/homeassistant-ai/ha-mcp/pull/1984))

---
<details>
<summary>Internal Changes</summary>


### Chores

- **deps**: Update src/ha_mcp/resources/skills-vendor digest to 191cfec
  ([#1998](https://github.com/homeassistant-ai/ha-mcp/pull/1998))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.7.2
  ([#1999](https://github.com/homeassistant-ai/ha-mcp/pull/1999))
- **deps**: Bump astro from 6.4.6 to 7.1.0 in /site
  ([#1995](https://github.com/homeassistant-ai/ha-mcp/pull/1995))
- Sync tool docs after merge [skip ci]
  ([`77de1d6`](https://github.com/homeassistant-ai/ha-mcp/commit/77de1d6cde9a5a4c61a378d849e791a7df6c1dca))
- **addon**: Promote webhook-proxy 2.0.5 + fold in #1978 parity fixes
  ([#1977](https://github.com/homeassistant-ai/ha-mcp/pull/1977))
- **addon**: Publish version 7.14.1 [skip ci]
  ([`4911d09`](https://github.com/homeassistant-ai/ha-mcp/commit/4911d09d1c19923230f624d2f3158f1cda5ccc46))

### Continuous Integration

- **deps**: Bump the github-actions group with 4 updates
  ([#1983](https://github.com/homeassistant-ai/ha-mcp/pull/1983))
</details>


## v7.14.1 (2026-07-19)

### Changed

- Record repo-wide C901 enforcement, guard against new per-file ignores
  ([#1967](https://github.com/homeassistant-ai/ha-mcp/pull/1967))

### Fixed

- None-mode webhook connects from claude.ai with no login
  ([#1976](https://github.com/homeassistant-ai/ha-mcp/pull/1976))
- Raise on a dead WebSocket transport so dead entities fails loud
  ([#1966](https://github.com/homeassistant-ai/ha-mcp/pull/1966))
- Make pending component updates visible (learn-more docs, HACS nudge, held-install warning)
  ([#1970](https://github.com/homeassistant-ai/ha-mcp/pull/1970))
- Raise MIN_COMPONENT_VERSION to 1.2.0 so stale 1.1.0 builds fail the gate
  ([#1961](https://github.com/homeassistant-ai/ha-mcp/pull/1961))

---
<details>
<summary>Internal Changes</summary>


### Chores

- **addon**: Publish version 7.14.0 [skip ci]
  ([`fb455a2`](https://github.com/homeassistant-ai/ha-mcp/commit/fb455a20357a31e7653e63a96b3a663fab514a6f))

### Continuous Integration

- Never fail the mirror gate open on a skipped triggering run
  ([#1968](https://github.com/homeassistant-ai/ha-mcp/pull/1968))

### Testing

- Cover ha_manage_backup(action="diff") tool-layer error mapping
  ([#1963](https://github.com/homeassistant-ai/ha-mcp/pull/1963))
</details>


## v7.14.0 (2026-07-19)

### Added

- Component-backed server-entry direct-write, closing out issue #1813
  ([#1935](https://github.com/homeassistant-ai/ha-mcp/pull/1935))
- Surface connect URLs for every network interface (#1862)
  ([#1928](https://github.com/homeassistant-ai/ha-mcp/pull/1928))
- Phase 3 write capabilities — call_service and bulk_call_service (#1813)
  ([#1921](https://github.com/homeassistant-ai/ha-mcp/pull/1921))
- Add external OIDC authentication support for standalone mode
  ([#1888](https://github.com/homeassistant-ai/ha-mcp/pull/1888))
- Phase 2 component-backed robustness reads (config entries, registry lookups, system snapshot, dashboards, services, visibility) (#1813)
  ([#1902](https://github.com/homeassistant-ai/ha-mcp/pull/1902))
- **addon**: Add icon and logo to both add-on flavors
  ([#1916](https://github.com/homeassistant-ai/ha-mcp/pull/1916))
- Phase 1 component-backed read capabilities (states, blueprint bodies, device reads, entity enrichment, exposure) (#1813)
  ([#1895](https://github.com/homeassistant-ai/ha-mcp/pull/1895))
- Accept locally installed skills and allow disabling ha_get_skill_guide
  ([#1900](https://github.com/homeassistant-ai/ha-mcp/pull/1900))
- Add overwrite option to ha_import_blueprint for blueprint re-import
  ([#1897](https://github.com/homeassistant-ai/ha-mcp/pull/1897))

### Changed

- **c901**: Clear final 6 files, delete emptied grandfather list (closes #925)
  ([#1964](https://github.com/homeassistant-ai/ha-mcp/pull/1964))
- **c901**: UAT harness below C901 threshold, remove from grandfather list
  ([#1955](https://github.com/homeassistant-ai/ha-mcp/pull/1955))
- **c901**: Test infra and unit tests below C901 threshold, remove from grandfather list
  ([#1953](https://github.com/homeassistant-ai/ha-mcp/pull/1953))
- **c901**: Fix 3 files below C901 threshold, remove from grandfather list
  ([#1940](https://github.com/homeassistant-ai/ha-mcp/pull/1940))
- **c901**: Fix webhook-proxy dev flavor (2 files) below C901 threshold, remove from grandfather list
  ([#1952](https://github.com/homeassistant-ai/ha-mcp/pull/1952))
- **c901**: E2e infra below C901 threshold, remove from grandfather list
  ([#1951](https://github.com/homeassistant-ai/ha-mcp/pull/1951))
- **c901**: Custom component below C901 threshold, remove from grandfather list
  ([#1943](https://github.com/homeassistant-ai/ha-mcp/pull/1943))
- **c901**: Fix 7 e2e test files below C901 threshold, remove from grandfather list
  ([#1941](https://github.com/homeassistant-ai/ha-mcp/pull/1941))
- **c901**: Fix backup subsystem (3 files) below C901 threshold, remove from grandfather list
  ([#1937](https://github.com/homeassistant-ai/ha-mcp/pull/1937))
- **c901**: Fix 9 src tool files below C901 threshold, remove from grandfather list
  ([#1938](https://github.com/homeassistant-ai/ha-mcp/pull/1938))

### Fixed

- Prefer an HTTP-500 sample in the deep-search failed fragment
  ([#1962](https://github.com/homeassistant-ai/ha-mcp/pull/1962))
- Join the entity registry on the ha_config_list_helpers legacy fallback
  ([#1960](https://github.com/homeassistant-ai/ha-mcp/pull/1960))
- Raise on parse error for a single explicit ha_config_get_yaml target
  ([#1959](https://github.com/homeassistant-ai/ha-mcp/pull/1959))
- Allow ha_manage_backup(scope="edits", action="diff") in read-only mode
  ([#1958](https://github.com/homeassistant-ai/ha-mcp/pull/1958))
- Name a representative error in the deep-search non-404 failed fragment
  ([#1930](https://github.com/homeassistant-ai/ha-mcp/pull/1930))
- Exclude inactive issue-registry entries from overview repairs (#1905)
  ([#1906](https://github.com/homeassistant-ai/ha-mcp/pull/1906))
- Serve OAuth/OIDC settings UI behind a dedicated secret path
  ([#1934](https://github.com/homeassistant-ai/ha-mcp/pull/1934))
- Escape translated catalog strings before rendering as HTML in settings UI
  ([#1931](https://github.com/homeassistant-ai/ha-mcp/pull/1931))
- Return actionable not-found for ha_config_set_label with an unknown label_id
  ([#1926](https://github.com/homeassistant-ai/ha-mcp/pull/1926))
- State which server ha_dev_manage_server update_source actually changes
  ([#1929](https://github.com/homeassistant-ai/ha-mcp/pull/1929))
- Make strict-BPS ack key a rotating attestation phrase
  ([#1925](https://github.com/homeassistant-ai/ha-mcp/pull/1925))
- Reinstall the embedded server when its pip source changes, not just its version
  ([#1923](https://github.com/homeassistant-ai/ha-mcp/pull/1923))
- Keep MCP URL on one log line; document dev/stable add-on conflict
  ([#1922](https://github.com/homeassistant-ai/ha-mcp/pull/1922))
- Alert on out-of-cycle stable mirror tags and track orphaned install jobs
  ([#1915](https://github.com/homeassistant-ai/ha-mcp/pull/1915))
- Restore the engine user's saved theme after dashboard screenshots
  ([#1912](https://github.com/homeassistant-ai/ha-mcp/pull/1912))
- Track importing workers process-wide so a reload cannot purge under them
  ([#1911](https://github.com/homeassistant-ai/ha-mcp/pull/1911))
- Guide stale-schema clients past BestPracticeKey client-side rejection
  ([#1910](https://github.com/homeassistant-ai/ha-mcp/pull/1910))
- Keep embedded bring-up alive while the worker makes startup progress
  ([#1908](https://github.com/homeassistant-ai/ha-mcp/pull/1908))
- Remove phantom bulk-config WS commands and honor HA SSL/port on the embedded loopback
  ([#1892](https://github.com/homeassistant-ai/ha-mcp/pull/1892))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Add contributors update skill
  ([#1932](https://github.com/homeassistant-ai/ha-mcp/pull/1932))
- **addon**: Promote webhook-proxy dev -> stable 2.0.4
  ([#1956](https://github.com/homeassistant-ai/ha-mcp/pull/1956))
- **addon**: Promote webhook-proxy dev -> stable 2.0.3
  ([#1936](https://github.com/homeassistant-ai/ha-mcp/pull/1936))
- Sync tool docs after merge [skip ci]
  ([`8bae89b`](https://github.com/homeassistant-ai/ha-mcp/commit/8bae89b37d7d021382a39a499598adcf79677a96))
- Disable sunset Gemini Code Assist and point review docs at Codex
  ([#1919](https://github.com/homeassistant-ai/ha-mcp/pull/1919))
- Sync tool docs after merge [skip ci]
  ([`8422402`](https://github.com/homeassistant-ai/ha-mcp/commit/842240287966d89872704f866383878b933a1841))
- Sync tool docs after merge [skip ci]
  ([`084ae59`](https://github.com/homeassistant-ai/ha-mcp/commit/084ae599e27e06f81bcaa801f31cbbd4a335b0bc))
- Sync tool docs after merge [skip ci]
  ([`11bb55e`](https://github.com/homeassistant-ai/ha-mcp/commit/11bb55e181befbb4e767880a0a7d4ef6cd540778))
- Sync tool docs after merge [skip ci]
  ([`fc45acb`](https://github.com/homeassistant-ai/ha-mcp/commit/fc45acbc2e1b51213b4cc5c313ca749004b489f8))
- Sync tool docs after merge [skip ci]
  ([`1a66dfc`](https://github.com/homeassistant-ai/ha-mcp/commit/1a66dfc808738b2765e1766e5852f505320e6d8e))
- **addon**: Publish version 7.13.0 [skip ci]
  ([`108d848`](https://github.com/homeassistant-ai/ha-mcp/commit/108d84869ee866784e13c4b7e4c1316d26ecc79a))

### Testing

- Close #1906 review gaps and bump component to 1.2.1
  ([#1942](https://github.com/homeassistant-ai/ha-mcp/pull/1942))
</details>


## v7.13.0 (2026-07-16)

### Added

- Add legacy OAuth mode to the in-process server for OAuth-only MCP clients
  ([#1880](https://github.com/homeassistant-ai/ha-mcp/pull/1880))
- Add snapshot delete + fix backup progress heartbeats (#1861)
  ([#1881](https://github.com/homeassistant-ai/ha-mcp/pull/1881))
- Add limit/offset pagination to the three unpaginated list tools
  ([#1885](https://github.com/homeassistant-ai/ha-mcp/pull/1885))
- Clarify custom component two-entry setup and remove redundant installer tool
  ([#1883](https://github.com/homeassistant-ai/ha-mcp/pull/1883))
- Read-only YAML fragment lookup for configuration and package keys
  ([#1882](https://github.com/homeassistant-ai/ha-mcp/pull/1882))
- Set openWorldHint explicitly on every MCP tool
  ([#1879](https://github.com/homeassistant-ai/ha-mcp/pull/1879))
- Opt-in GET /healthz liveness endpoint (MCP_HEALTHZ env var)
  ([#1777](https://github.com/homeassistant-ai/ha-mcp/pull/1777))
- Add ws_command escape hatch to ha_call_service for WebSocket-only commands
  ([#1876](https://github.com/homeassistant-ai/ha-mcp/pull/1876))
- Edit recorder config and packages under any configured folder
  ([#1863](https://github.com/homeassistant-ai/ha-mcp/pull/1863))
- Make dashboard screenshot workflows robust
  ([#1837](https://github.com/homeassistant-ai/ha-mcp/pull/1837))
- Phase 0 server-side tool optimizations from the #1813 audit
  ([#1832](https://github.com/homeassistant-ai/ha-mcp/pull/1832))
- Strict best-practices acknowledgment gate for write tools
  ([#1820](https://github.com/homeassistant-ai/ha-mcp/pull/1820))
- Drive integration config flows and options flows from ha_set_integration
  ([#1822](https://github.com/homeassistant-ai/ha-mcp/pull/1822))
- Ha_search optimization + component-backed read API
  ([#1812](https://github.com/homeassistant-ai/ha-mcp/pull/1812))
- Expose the in-process server toolset as a Home Assistant LLM API (#1745)
  ([#1782](https://github.com/homeassistant-ai/ha-mcp/pull/1782))
- Add options to disable the server startup notification and sidebar panel
  ([#1815](https://github.com/homeassistant-ai/ha-mcp/pull/1815))

### Changed

- **settings_ui**: Split __init__.py into focused modules; de-grandfather C901
  ([#1867](https://github.com/homeassistant-ai/ha-mcp/pull/1867))
- Fix 8 more files below C901 threshold, remove from grandfather list
  ([#1866](https://github.com/homeassistant-ai/ha-mcp/pull/1866))
- **c901**: Fix 8 files below C901 threshold, remove from grandfather list
  ([#1835](https://github.com/homeassistant-ai/ha-mcp/pull/1835))
- Clarify external URL guidance — point at HA, no port
  ([#1868](https://github.com/homeassistant-ai/ha-mcp/pull/1868))
- Restore Star History chart
  ([#1831](https://github.com/homeassistant-ai/ha-mcp/pull/1831))
- Reference community OpenAI Tunnel integration for firewalled ChatGPT access
  ([#1828](https://github.com/homeassistant-ai/ha-mcp/pull/1828))
- Clarify saved custom tools are shared, out of scope
  ([#1816](https://github.com/homeassistant-ai/ha-mcp/pull/1816))

### Fixed

- Reject malformed feature-flags payload instead of silent no-op
  ([#1865](https://github.com/homeassistant-ai/ha-mcp/pull/1865))
- Correct Tools-tab group master + LLM API toggle rendering
  ([#1859](https://github.com/homeassistant-ai/ha-mcp/pull/1859))
- Recover legacy embedded server upgrades
  ([#1838](https://github.com/homeassistant-ai/ha-mcp/pull/1838))
- Persist cleared override fields in the in-process server options flow
  ([#1834](https://github.com/homeassistant-ai/ha-mcp/pull/1834))
- Pooled-WS connection-error residue and verify_ssl pool-key split
  ([#1833](https://github.com/homeassistant-ai/ha-mcp/pull/1833))
- Join entity registry in ha_config_list_helpers so renamed helpers show current entity_id/name
  ([#1818](https://github.com/homeassistant-ai/ha-mcp/pull/1818))
- Preserve malformed JSON decoder details
  ([#1825](https://github.com/homeassistant-ai/ha-mcp/pull/1825))
- Warn about ignored config keys in subentry flows
  ([#1817](https://github.com/homeassistant-ai/ha-mcp/pull/1817))
- Classify deep-search per-id fetch timeouts distinctly and settings-back the Attempt-C knobs
  ([#1810](https://github.com/homeassistant-ai/ha-mcp/pull/1810))
- Surface beta flag and missing-tool refresh hint in ha_report_issue
  ([#1809](https://github.com/homeassistant-ai/ha-mcp/pull/1809))

---
<details>
<summary>Internal Changes</summary>


### Changed

- Clarify component version bump follows the stable release cycle
  ([#1842](https://github.com/homeassistant-ai/ha-mcp/pull/1842))

### Fixed

- Make demo server setup self-healing
  ([#1826](https://github.com/homeassistant-ai/ha-mcp/pull/1826))

### Chores

- Sync tool docs after merge [skip ci]
  ([`ea5d053`](https://github.com/homeassistant-ai/ha-mcp/commit/ea5d053a583ba6fab26c68c06e86637098b3a80a))
- Sync tool docs after merge [skip ci]
  ([`5bfaf4a`](https://github.com/homeassistant-ai/ha-mcp/commit/5bfaf4acba93b7e270fac08a76f93dd469e65816))
- Sync tool docs after merge [skip ci]
  ([`dee89d7`](https://github.com/homeassistant-ai/ha-mcp/commit/dee89d73909963cd31834f754a5922d4d3d21280))
- Sync tool docs after merge [skip ci]
  ([`fb18207`](https://github.com/homeassistant-ai/ha-mcp/commit/fb1820735188c2551a824085d7de113df9e1dffd))
- Sync tool docs after merge [skip ci]
  ([`4403f6a`](https://github.com/homeassistant-ai/ha-mcp/commit/4403f6ab0056742f51171e543cb32dc4f90ca078))
- Sync tool docs after merge [skip ci]
  ([`6ce33f5`](https://github.com/homeassistant-ai/ha-mcp/commit/6ce33f5785d74225cbbddf027104e9315cfef3b4))
- **deps**: Update dependency home-assistant/operating-system to v18.1
  ([#1878](https://github.com/homeassistant-ai/ha-mcp/pull/1878))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.28
  ([#1877](https://github.com/homeassistant-ai/ha-mcp/pull/1877))
- **addon**: Promote webhook-proxy dev -> stable 2.0.2
  ([#1873](https://github.com/homeassistant-ai/ha-mcp/pull/1873))
- Sync tool docs after merge [skip ci]
  ([`dc7b794`](https://github.com/homeassistant-ai/ha-mcp/commit/dc7b794ebf5f1d41bdfa17ab3c9e4a43fc67191c))
- Sync tool docs after merge [skip ci]
  ([`d2c0434`](https://github.com/homeassistant-ai/ha-mcp/commit/d2c0434389c0ab8497341e2ae5f20672138a260e))
- Sync tool docs after merge [skip ci]
  ([`7948fed`](https://github.com/homeassistant-ai/ha-mcp/commit/7948fed8f2f82dfcce1c3a586428dfd49a880a75))
- Sync tool docs after merge [skip ci]
  ([`be34262`](https://github.com/homeassistant-ai/ha-mcp/commit/be34262d537bc0ca899594e1d1154536637080e0))
- Sync tool docs after merge [skip ci]
  ([`5285a65`](https://github.com/homeassistant-ai/ha-mcp/commit/5285a65ae87438965a2bbfeb97cd298014a49dad))
- Sync tool docs after merge [skip ci]
  ([`b0f29e5`](https://github.com/homeassistant-ai/ha-mcp/commit/b0f29e5947fbcb82b3a8bcf62a74f5fdb9da63f4))
- Sync tool docs after merge [skip ci]
  ([`efcecad`](https://github.com/homeassistant-ai/ha-mcp/commit/efcecad470200a463d966f4746c6dc5230db302d))
- **addon**: Publish hotfix version 7.12.3 [skip ci]
  ([`3606f24`](https://github.com/homeassistant-ai/ha-mcp/commit/3606f2469e801efbc5c0157d75f19e92d1e3f33a))

### Continuous Integration

- **deps**: Bump the github-actions group with 3 updates
  ([#1847](https://github.com/homeassistant-ai/ha-mcp/pull/1847))
- Skip Codex reviews for dependency bots
  ([#1856](https://github.com/homeassistant-ai/ha-mcp/pull/1856))
- Rate limit Codex review requests
  ([#1830](https://github.com/homeassistant-ai/ha-mcp/pull/1830))
- Secure manual Codex review requests
  ([#1829](https://github.com/homeassistant-ai/ha-mcp/pull/1829))
- Avoid duplicate Codex reviews
  ([#1827](https://github.com/homeassistant-ai/ha-mcp/pull/1827))
- Request Codex reviews from PR events
  ([#1824](https://github.com/homeassistant-ai/ha-mcp/pull/1824))
- Publish dev component builds as HACS pre-releases on the mirror
  ([#1819](https://github.com/homeassistant-ai/ha-mcp/pull/1819))
</details>


## v7.12.3 (2026-07-10)

### Fixed

- Sidebar settings panel 503s forever when webhook access is disabled
  ([#1806](https://github.com/homeassistant-ai/ha-mcp/pull/1806))

---
<details>
<summary>Internal Changes</summary>


### Chores

- **addon**: Publish version 7.12.2 [skip ci]
  ([`51f4ae5`](https://github.com/homeassistant-ai/ha-mcp/commit/51f4ae5ffc6d35c9bcfa92b20965b533c064c78e))

### Continuous Integration

- Gate the mirror version tag on the release job, not the whole run
  ([#1805](https://github.com/homeassistant-ai/ha-mcp/pull/1805))
</details>


## v7.12.2 (2026-07-10)

### Fixed

- Stop the HA-MCP sidebar panel trapping iOS navigation and tripping http.ban
  ([#1801](https://github.com/homeassistant-ai/ha-mcp/pull/1801))

---
<details>
<summary>Internal Changes</summary>


### Chores

- **addon**: Publish version 7.12.1 [skip ci]
  ([`2b77d23`](https://github.com/homeassistant-ai/ha-mcp/commit/2b77d23791dd47594ae81e61e29969691dfbf3a8))

### Continuous Integration

- Use the release app token for hotfix-release's master push
  ([#1800](https://github.com/homeassistant-ai/ha-mcp/pull/1800))
</details>


## v7.12.1 (2026-07-10)

### Changed

- **c901**: Fix 8 files below C901 threshold, remove from grandfather list
  ([#1793](https://github.com/homeassistant-ai/ha-mcp/pull/1793))
- **c901**: Tools_config_dashboards.py below C901 threshold, enable C90 lint
  ([#1790](https://github.com/homeassistant-ai/ha-mcp/pull/1790))

### Fixed

- Stop the auto-updated in-process server crashing under an older custom component
  ([#1792](https://github.com/homeassistant-ai/ha-mcp/pull/1792))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Isolate 3 beta-gate tests from real ~/.ha-mcp config
  ([#1791](https://github.com/homeassistant-ai/ha-mcp/pull/1791))
- Split mypy-webhook-proxy lefthook job to avoid module collision
  ([#1789](https://github.com/homeassistant-ai/ha-mcp/pull/1789))

### Chores

- **addon**: Publish version 7.12.0 [skip ci]
  ([`adcbd61`](https://github.com/homeassistant-ai/ha-mcp/commit/adcbd61c88633f1d3b12c2f713b6971cd82313d3))

### Testing

- Add an update-path e2e lane covering released-component x new-server installs
  ([#1796](https://github.com/homeassistant-ai/ha-mcp/pull/1796))
</details>


## v7.12.0 (2026-07-09)

### Added

- Dev mode tools, embedded server update fixes, and version diagnosability
  ([#1780](https://github.com/homeassistant-ai/ha-mcp/pull/1780))

---
<details>
<summary>Internal Changes</summary>


### Chores

- Sync tool docs after merge [skip ci]
  ([`9c63441`](https://github.com/homeassistant-ai/ha-mcp/commit/9c63441645494114f0f615dd3b05f9f2e62ae5db))
- **addon**: Publish version 7.11.0 [skip ci]
  ([`2913104`](https://github.com/homeassistant-ai/ha-mcp/commit/2913104bf706adccc53c06a2c31e82deb82479bc))

### Continuous Integration

- Fix release push race and hotfix mirror tagging
  ([#1781](https://github.com/homeassistant-ai/ha-mcp/pull/1781))
</details>


## v7.11.0 (2026-07-08)

### Added

- Server update entity, auto-update notifications, and real HACS release notes (#1760)
  ([#1776](https://github.com/homeassistant-ai/ha-mcp/pull/1776))

### Fixed

- Show the browser landing page on the in-process server's MCP endpoint
  ([#1774](https://github.com/homeassistant-ai/ha-mcp/pull/1774))
- Surface WebSocket close to tool calls instead of hanging
  ([#1773](https://github.com/homeassistant-ai/ha-mcp/pull/1773))

---
<details>
<summary>Internal Changes</summary>


### Chores

- **deps**: Update src/ha_mcp/resources/skills-vendor digest to 5c023ed
  ([#1771](https://github.com/homeassistant-ai/ha-mcp/pull/1771))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.27
  ([#1772](https://github.com/homeassistant-ai/ha-mcp/pull/1772))
- **addon**: Publish version 7.10.0 [skip ci]
  ([`ada29d0`](https://github.com/homeassistant-ai/ha-mcp/commit/ada29d06e2fdbee70ddf1090d41b5e1963a042a2))

### Continuous Integration

- **security**: Pin third-party actions & verify release binaries (#1762)
  ([#1770](https://github.com/homeassistant-ai/ha-mcp/pull/1770))
- Route untrusted PR branch name through env in hotfix workflows
  ([#1769](https://github.com/homeassistant-ai/ha-mcp/pull/1769))
- Tag the HACS mirror on workflow_run after SemVer Release, not the release event
  ([#1761](https://github.com/homeassistant-ai/ha-mcp/pull/1761))
</details>


## v7.10.0 (2026-07-06)

### Added

- Custom component first — installation overhaul and self-updating in-process server (#1715)
  ([#1751](https://github.com/homeassistant-ai/ha-mcp/pull/1751))
- Remove the add-on bootstrap from the tools config flow
  ([#1750](https://github.com/homeassistant-ai/ha-mcp/pull/1750))
- Opt-in entity visibility filter for collection read tools (#1728)
  ([#1736](https://github.com/homeassistant-ai/ha-mcp/pull/1736))
- In-process MCP server as a standalone ha_mcp_server custom integration (#1527)
  ([#1741](https://github.com/homeassistant-ai/ha-mcp/pull/1741))
- Align validator, traces, and update tools with HA 2026.7
  ([#1735](https://github.com/homeassistant-ai/ha-mcp/pull/1735))
- **addon**: Webhook Proxy dev channel + release flow, with OAuth collision guard & restart Repair
  ([#1719](https://github.com/homeassistant-ai/ha-mcp/pull/1719))
- Keep ha_manage_radio reads available in read-only mode
  ([#1699](https://github.com/homeassistant-ai/ha-mcp/pull/1699))
- Matter & Thread support + unified ha_manage_radio management tool
  ([#1696](https://github.com/homeassistant-ai/ha-mcp/pull/1696))

### Changed

- FAQ for parentheses in mcpServers key dropping Claude Desktop tools
  ([#1746](https://github.com/homeassistant-ai/ha-mcp/pull/1746))
- Update contributors list [contributors-updated]
  ([`69bc4d7`](https://github.com/homeassistant-ai/ha-mcp/commit/69bc4d755b0e5d7da07f0d061ae8c8d812f095c4))
- Cover Cloudflare "Block AI training bots" + geo-blocking on every remote-access surface
  ([#1724](https://github.com/homeassistant-ai/ha-mcp/pull/1724))
- Clarify helper initial restore behavior
  ([#1690](https://github.com/homeassistant-ai/ha-mcp/pull/1690))
- Promote the Home Assistant App (add-on) and steer users off stdio
  ([#1712](https://github.com/homeassistant-ai/ha-mcp/pull/1712))
- Normalize MCP server name to home-assistant in setup wizard
  ([#1653](https://github.com/homeassistant-ai/ha-mcp/pull/1653))
- **settings-ui**: HA-faithful redesign, auto-save, dedicated package
  ([#1695](https://github.com/homeassistant-ai/ha-mcp/pull/1695))
- **c901**: Tools_entities.py below C901 threshold
  ([#1692](https://github.com/homeassistant-ai/ha-mcp/pull/1692))
- **c901**: Tools_registry.py below C901 threshold
  ([#1691](https://github.com/homeassistant-ai/ha-mcp/pull/1691))
- **c901**: Tools_search.py below C901 threshold
  ([#1665](https://github.com/homeassistant-ai/ha-mcp/pull/1665))

### Fixed

- Catch fastmcp 3.4.3's wrapped ValidationError in arg-validation middleware
  ([#1757](https://github.com/homeassistant-ai/ha-mcp/pull/1757))
- Keep HTTP servers reachable under fastmcp 3.4.3 Host/Origin guard
  ([#1756](https://github.com/homeassistant-ai/ha-mcp/pull/1756))
- **addon**: Honor web-UI log level; arm kill-signal diagnostics at DEBUG; plumb journald log window
  ([#1734](https://github.com/homeassistant-ai/ha-mcp/pull/1734))
- Bump bundled skills to v12 + enable Renovate git-submodules manager (closes #1729)
  ([#1731](https://github.com/homeassistant-ai/ha-mcp/pull/1731))
- Stop ha_config_set_yaml corrupting untouched content; add diff preview + confirm flow
  ([#1727](https://github.com/homeassistant-ai/ha-mcp/pull/1727))
- Remove slash from webhook proxy add-on name (#1707)
  ([#1709](https://github.com/homeassistant-ai/ha-mcp/pull/1709))
- Add-on auth-error guidance + webhook proxy diagnosability (#1694)
  ([#1700](https://github.com/homeassistant-ai/ha-mcp/pull/1700))
- Settings UI regressions, accessibility, and copy after the #1695 redesign
  ([#1698](https://github.com/homeassistant-ai/ha-mcp/pull/1698))
- **entities**: Refetch entity state after exposure so the response reflects new should_expose
  ([#1697](https://github.com/homeassistant-ai/ha-mcp/pull/1697))

---
<details>
<summary>Internal Changes</summary>


### Added

- **addon-dev**: Log the ha_auth bearer rejection reason in the inbound debug log
  ([#1740](https://github.com/homeassistant-ai/ha-mcp/pull/1740))
- **addon-dev**: Add HA-native OAuth mode (ha_auth) to the Webhook Proxy
  ([#1730](https://github.com/homeassistant-ai/ha-mcp/pull/1730))
- **addon-dev**: Serve OAuth metadata at the RFC 8414/9728/OIDC well-known locations
  ([#1723](https://github.com/homeassistant-ai/ha-mcp/pull/1723))

### Fixed

- **ci**: Authenticate HACS in the HAOS e2e suite (GitHub rate-limit flake)
  ([#1722](https://github.com/homeassistant-ai/ha-mcp/pull/1722))

### Chores

- **addon**: Promote webhook-proxy dev -> stable 2.0.1
  ([#1744](https://github.com/homeassistant-ai/ha-mcp/pull/1744))
- Sync tool docs after merge [skip ci]
  ([`9c07f0f`](https://github.com/homeassistant-ai/ha-mcp/commit/9c07f0f6c18b86673f3f8c7cec24190ffd0b9387))
- **addon**: Promote webhook-proxy dev -> stable 2.0.0
  ([#1739](https://github.com/homeassistant-ai/ha-mcp/pull/1739))
- **deps**: Update src/ha_mcp/resources/skills-vendor digest to 441d077
  ([#1732](https://github.com/homeassistant-ai/ha-mcp/pull/1732))
- Sync tool docs after merge [skip ci]
  ([`8b1c313`](https://github.com/homeassistant-ai/ha-mcp/commit/8b1c3136d9b6fc41f8f61c556e2d15a28c92cbe0))
- Sync tool docs after merge [skip ci]
  ([`279d7eb`](https://github.com/homeassistant-ai/ha-mcp/commit/279d7ebefd3ed8ac7e8c120a426b581bbcc9df8b))
- **deps**: Update dependency home-assistant/operating-system to v18
  ([#1718](https://github.com/homeassistant-ai/ha-mcp/pull/1718))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.26
  ([#1717](https://github.com/homeassistant-ai/ha-mcp/pull/1717))
- Sync tool docs after merge [skip ci]
  ([`0072154`](https://github.com/homeassistant-ai/ha-mcp/commit/00721543229587ee9fc16f0f4c86fad608e12a69))
- Sync tool docs after merge [skip ci]
  ([`605ccde`](https://github.com/homeassistant-ai/ha-mcp/commit/605ccde088ba0550c63bc8245043c7787ec6b71b))
- **addon**: Publish version 7.9.0 [skip ci]
  ([`4b1bc47`](https://github.com/homeassistant-ai/ha-mcp/commit/4b1bc4723ed7cb459222ad77fbfa2361f337cb18))

### Continuous Integration

- **deps**: Bump the github-actions group with 4 updates
  ([#1755](https://github.com/homeassistant-ai/ha-mcp/pull/1755))
- Write the mirror deploy key with a trailing newline
  ([#1748](https://github.com/homeassistant-ai/ha-mcp/pull/1748))
- **addon**: Install pytest-asyncio in the webhook-proxy sync workflows
  ([#1738](https://github.com/homeassistant-ai/ha-mcp/pull/1738))
- **deps**: Bump the github-actions group with 3 updates
  ([#1705](https://github.com/homeassistant-ai/ha-mcp/pull/1705))

### Testing

- **addon**: Gate the dev5 validator tests on the capability, not file existence
  ([#1742](https://github.com/homeassistant-ai/ha-mcp/pull/1742))
</details>


## v7.9.0 (2026-06-24)

### Added

- Add include_knx_project to ha_get_integration for KNX group addresses
  ([`8c0c39d`](https://github.com/homeassistant-ai/ha-mcp/commit/8c0c39d1d83fab7498921e07eaaa3d8b729eac82))
- **backup**: Fold file & YAML writes into the edits auto-backup layer
  ([#1649](https://github.com/homeassistant-ai/ha-mcp/pull/1649))
- Notify when a newer ha-mcp release is available
  ([#1652](https://github.com/homeassistant-ai/ha-mcp/pull/1652))
- Add advanced setting to pin the stdio settings-UI sidecar port
  ([#1642](https://github.com/homeassistant-ai/ha-mcp/pull/1642))
- Default ha_get_logs to newest-first with an order toggle
  ([#1640](https://github.com/homeassistant-ai/ha-mcp/pull/1640))
- Add diff action to ha_manage_backup(scope="edits")
  ([#1632](https://github.com/homeassistant-ai/ha-mcp/pull/1632))
- **webhook-proxy**: Inbound-request debug logging + connector docs
  ([#1633](https://github.com/homeassistant-ai/ha-mcp/pull/1633))
- Add dead_entities section to ha_get_system_health
  ([#1615](https://github.com/homeassistant-ai/ha-mcp/pull/1615))
- Allow filesystem tools to access HAOS sibling volumes
  ([#1626](https://github.com/homeassistant-ai/ha-mcp/pull/1626))
- **site**: Accessibility CI gate + web UI a11y fixes (#1595, #1596, #1597)
  ([#1621](https://github.com/homeassistant-ai/ha-mcp/pull/1621))

### Changed

- Explain reverse-proxy geo-blocking on the browser landing page
  ([#1687](https://github.com/homeassistant-ai/ha-mcp/pull/1687))
- Add smaller/local-LLM tool-search guidance
  ([#1654](https://github.com/homeassistant-ai/ha-mcp/pull/1654))

### Fixed

- Await pre-restore safety backup and forward password on snapshot restore
  ([#1684](https://github.com/homeassistant-ai/ha-mcp/pull/1684))
- **ha_restart**: Extend known-good error patterns to cover 502/503 from proxies
  ([#1670](https://github.com/homeassistant-ai/ha-mcp/pull/1670))
- Run post-write config check via async_check_ha_config_file (#1660)
  ([#1661](https://github.com/homeassistant-ai/ha-mcp/pull/1661))
- Auto-backup silently skipped capture on several write paths
  ([#1643](https://github.com/homeassistant-ai/ha-mcp/pull/1643))
- Convert UTC timestamps to local timezone in add_timezone_metadata
  ([#1592](https://github.com/homeassistant-ai/ha-mcp/pull/1592))
- Stop logging confusing "Terminating session: None" noise in stateless HTTP
  ([#1634](https://github.com/homeassistant-ai/ha-mcp/pull/1634))

---
<details>
<summary>Internal Changes</summary>


### Changed

- Document the custom-component version-bump + post-merge test rule
  ([#1659](https://github.com/homeassistant-ai/ha-mcp/pull/1659))
- Refresh ha_manage_addon ESPHome dashboard guidance
  ([#1656](https://github.com/homeassistant-ai/ha-mcp/pull/1656))

### Fixed

- **deps**: Bump vite to 7.3.5 in site/ (Dependabot #62 #63)
  ([#1664](https://github.com/homeassistant-ai/ha-mcp/pull/1664))

### Build System

- **deps**: Bump astro from 6.1.10 to 6.4.6 in /site
  ([#1647](https://github.com/homeassistant-ai/ha-mcp/pull/1647))
- **deps**: Bump js-yaml from 4.1.1 to 4.2.0 in /site
  ([#1639](https://github.com/homeassistant-ai/ha-mcp/pull/1639))
- **deps**: Bump form-data from 4.0.5 to 4.0.6 in /tests/js
  ([#1638](https://github.com/homeassistant-ai/ha-mcp/pull/1638))

### Chores

- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.6.4
  ([#1686](https://github.com/homeassistant-ai/ha-mcp/pull/1686))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.23
  ([#1685](https://github.com/homeassistant-ai/ha-mcp/pull/1685))
- Sync tool docs after merge [skip ci]
  ([`86a19eb`](https://github.com/homeassistant-ai/ha-mcp/commit/86a19eb68ef44aa580920938717fffb8dd587076))
- Sync tool docs after merge [skip ci]
  ([`252320f`](https://github.com/homeassistant-ai/ha-mcp/commit/252320f1f7b297135fec2fc5faef673e5c15579c))
- Sync tool docs after merge [skip ci]
  ([`484fa02`](https://github.com/homeassistant-ai/ha-mcp/commit/484fa0216973613cb0eab9de0d054650f89762c8))
- Sync pyproject version to the published 7.8.1
  ([#1650](https://github.com/homeassistant-ai/ha-mcp/pull/1650))
- **addon**: Publish hotfix version 7.8.1 [skip ci]
  ([`dbc0d1e`](https://github.com/homeassistant-ai/ha-mcp/commit/dbc0d1e2eca37b01e17567a3d19996f96b364434))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.21
  ([#1645](https://github.com/homeassistant-ai/ha-mcp/pull/1645))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.6.3
  ([#1646](https://github.com/homeassistant-ai/ha-mcp/pull/1646))
- Sync tool docs after merge [skip ci]
  ([`c5c8ba3`](https://github.com/homeassistant-ai/ha-mcp/commit/c5c8ba3c2af0d36226588a63316b4e491beb8ed6))
- Sync tool docs after merge [skip ci]
  ([`c5704f6`](https://github.com/homeassistant-ai/ha-mcp/commit/c5704f6ef6be18401bf4a35267a94c191a5f8db1))
- Sync tool docs after merge [skip ci]
  ([`25dae72`](https://github.com/homeassistant-ai/ha-mcp/commit/25dae72aabcec787e958934ab576377a5937874d))
- Sync tool docs after merge [skip ci]
  ([`3f18d56`](https://github.com/homeassistant-ai/ha-mcp/commit/3f18d56c8e6a585212b3e66ce01b216c82695756))
- **addon**: Publish version 7.8.0 [skip ci]
  ([`3163fa6`](https://github.com/homeassistant-ai/ha-mcp/commit/3163fa62a5ec0f5f9661b550f7eb1f66ae509750))

### Continuous Integration

- Add HACS + Hassfest validation workflow
  ([#1671](https://github.com/homeassistant-ai/ha-mcp/pull/1671))
- **deps**: Bump the github-actions group with 2 updates
  ([#1676](https://github.com/homeassistant-ai/ha-mcp/pull/1676))
- Stop auto-applying "good first issue" label in triage bot
  ([#1675](https://github.com/homeassistant-ai/ha-mcp/pull/1675))

### Testing

- Lock config-check guard against behavioral recurrence (#1660)
  ([#1662](https://github.com/homeassistant-ai/ha-mcp/pull/1662))
- Cover calendar/todo fetcher happy path
  ([#1641](https://github.com/homeassistant-ai/ha-mcp/pull/1641))
</details>


## v7.8.0 (2026-06-15)

### Added

- Support recurring calendar events via rrule in ha_config_set_calendar_event
  ([#1585](https://github.com/homeassistant-ai/ha-mcp/pull/1585))
- Add ha_manage_theme tool and themes section in ha_get_system_health (closes #1580)
  ([#1588](https://github.com/homeassistant-ai/ha-mcp/pull/1588))
- Accessibility controls and theme presets for settings UI and docs site
  ([#1574](https://github.com/homeassistant-ai/ha-mcp/pull/1574))
- Add Read Only Mode toggle (web UI + addon config)
  ([#1573](https://github.com/homeassistant-ai/ha-mcp/pull/1573))

### Fixed

- SSE stateless_http startup, plus transient-5xx retry and HACS add timeout
  ([#1623](https://github.com/homeassistant-ai/ha-mcp/pull/1623))
- Correct setup-wizard transport config and quiet benign access-log noise
  ([#1620](https://github.com/homeassistant-ai/ha-mcp/pull/1620))
- **helpers**: Apply icon to flow/template helpers via the entity registry
  ([#1618](https://github.com/homeassistant-ai/ha-mcp/pull/1618))
- **toolsearch**: Actionable error + refresh guidance for stale tool-search catalog
  ([#1617](https://github.com/homeassistant-ai/ha-mcp/pull/1617))
- Coerce JSON-string dict/list args on all MCP container params
  ([#1613](https://github.com/homeassistant-ai/ha-mcp/pull/1613))
- **dashboards**: Recurse into nested cards in find_card + usable python_path
  ([#1610](https://github.com/homeassistant-ai/ha-mcp/pull/1610))
- Gate installer uv check on uvx, not uv
  ([#1606](https://github.com/homeassistant-ai/ha-mcp/pull/1606))
- Coerce JSON-encoded strings on dict/list tool params
  ([#1582](https://github.com/homeassistant-ai/ha-mcp/pull/1582))
- Rename config-entry-flow machinery out of tools_* namespace
  ([#1584](https://github.com/homeassistant-ai/ha-mcp/pull/1584))
- Report stored option values instead of schema defaults in options-flow reads
  ([#1577](https://github.com/homeassistant-ai/ha-mcp/pull/1577))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- **haos-e2e**: Apps/local refresh, Supervisor-update wait, cache-miss local build
  ([#1600](https://github.com/homeassistant-ai/ha-mcp/pull/1600))

### Chores

- Sync tool docs after merge [skip ci]
  ([`3193ce4`](https://github.com/homeassistant-ai/ha-mcp/commit/3193ce404bedfb6c00352fc4027e8ce4baca9222))
- Sync tool docs after merge [skip ci]
  ([`f21970d`](https://github.com/homeassistant-ai/ha-mcp/commit/f21970de1e7b74d2baa39d72dee848c2feaf348d))
- Sync tool docs after merge [skip ci]
  ([`ae02fe9`](https://github.com/homeassistant-ai/ha-mcp/commit/ae02fe9561a45201aa63c7c3250e59c81ec3012c))
- Sync tool docs after merge [skip ci]
  ([`1e3c18c`](https://github.com/homeassistant-ai/ha-mcp/commit/1e3c18c6e92cb502ef7e4212b8484175b9a9567c))
- Sync tool docs after merge [skip ci]
  ([`9967d50`](https://github.com/homeassistant-ai/ha-mcp/commit/9967d501e6739630bfda8b3a55ed559ac6fe95a1))
- **addon**: Publish version 7.7.0 [skip ci]
  ([`edc74fc`](https://github.com/homeassistant-ai/ha-mcp/commit/edc74fc0e8a52d5eaf02c2424cb3a3e57e393b17))

### Continuous Integration

- Fix HAOS bake with a mock screenshot engine; make e2e lanes safe to require
  ([#1611](https://github.com/homeassistant-ai/ha-mcp/pull/1611))
- **deps**: Bump esbuild from 0.25.0 to 0.28.1 in /tests/js
  ([#1591](https://github.com/homeassistant-ai/ha-mcp/pull/1591))
- Add abandoned PR policy and automated stale-PR reminders
  ([#1589](https://github.com/homeassistant-ai/ha-mcp/pull/1589))

### Testing

- Cover rrule error paths in ha_config_set_calendar_event
  ([#1616](https://github.com/homeassistant-ai/ha-mcp/pull/1616))
</details>


## v7.7.0 (2026-06-10)

### Added

- User-configurable custom filesystem directories for the file tools (closes #1567)
  ([#1568](https://github.com/homeassistant-ai/ha-mcp/pull/1568))
- Expose all user-tunable env vars in the settings UI (add-on parity)
  ([#1554](https://github.com/homeassistant-ai/ha-mcp/pull/1554))
- Warn when default MCP_SECRET_PATH is bound non-loopback
  ([#1472](https://github.com/homeassistant-ai/ha-mcp/pull/1472))
- **search**: Consolidate ha_search_entities + ha_deep_search into ha_search
  ([#1529](https://github.com/homeassistant-ai/ha-mcp/pull/1529))
- **addon**: Install the MCP Server add-on from the ha_mcp_tools integration
  ([#1528](https://github.com/homeassistant-ai/ha-mcp/pull/1528))
- Add opt-in dashboard screenshot mode
  ([#1510](https://github.com/homeassistant-ai/ha-mcp/pull/1510))
- Add Linux support for install & docs
  ([#1096](https://github.com/homeassistant-ai/ha-mcp/pull/1096))
- Surface the web settings page for non-add-on installs (#1458)
  ([#1511](https://github.com/homeassistant-ai/ha-mcp/pull/1511))
- Convert Pydantic arg-validation errors to actionable ToolErrors
  ([#1491](https://github.com/homeassistant-ai/ha-mcp/pull/1491))
- Per-key toggles for automation/script/scene in packages/*.yaml
  ([#1476](https://github.com/homeassistant-ai/ha-mcp/pull/1476))
- Direct skills retrieval for write tools + improved  best practice checker warnings with embedded skills responses (#1182)
  ([#1448](https://github.com/homeassistant-ai/ha-mcp/pull/1448))
- Detect last_changed/last_updated duration math and suggest for: field (#1157)
  ([#1264](https://github.com/homeassistant-ai/ha-mcp/pull/1264))
- Advanced settings panel + nested beta master toggle (#1164)
  ([#1431](https://github.com/homeassistant-ai/ha-mcp/pull/1431))
- Allow automation/script/scene yaml_path in packages/*.yaml only
  ([#1452](https://github.com/homeassistant-ai/ha-mcp/pull/1452))
- Restrict ha_mcp_tools services to ha-mcp callers (caller token + ha_call_service refusal)
  ([#1459](https://github.com/homeassistant-ai/ha-mcp/pull/1459))

### Changed

- **security**: Soften vulnerability-response SLA to best-effort
  ([`a24e7da`](https://github.com/homeassistant-ai/ha-mcp/commit/a24e7da4787aad570ac2998a5732de06229fa160))
- Update contributors list [contributors-updated]
  ([`7cc187f`](https://github.com/homeassistant-ai/ha-mcp/commit/7cc187f4de2f6eb58cafaa4fb21f8016cb180a61))
- Telemetry wording — follow HA analytics setting, not opt-in-only
  ([#1481](https://github.com/homeassistant-ai/ha-mcp/pull/1481))
- **tools**: Sharpen ha_eval_template usage routing for compute-from-state queries
  ([#1550](https://github.com/homeassistant-ai/ha-mcp/pull/1550))
- Differentiate ha-mcp from Home Assistant's built-in MCP Server
  ([#1542](https://github.com/homeassistant-ai/ha-mcp/pull/1542))
- Update advanced mode notes
  ([#1533](https://github.com/homeassistant-ai/ha-mcp/pull/1533))
- **#1157**: Scripts native-for: guidance + fix numeric_state-condition for: overclaim
  ([#1480](https://github.com/homeassistant-ai/ha-mcp/pull/1480))
- Clarify add-on vs uvx; add Codex + HTTP-native client setup
  ([#1478](https://github.com/homeassistant-ai/ha-mcp/pull/1478))
- **security**: Explicit threat model for trusted clients, LAN, sandbox, and OAuth tokens
  ([#1463](https://github.com/homeassistant-ai/ha-mcp/pull/1463))
- Clarify telemetry is a planned future feature, not implemented
  ([#1469](https://github.com/homeassistant-ai/ha-mcp/pull/1469))
- Fast-fail HACS not-found lookups + batch-verify deep-search E2E fixtures (#1515)
  ([#1552](https://github.com/homeassistant-ai/ha-mcp/pull/1552))
- Add display title to ha_get_skill_guide tool
  ([#1543](https://github.com/homeassistant-ai/ha-mcp/pull/1543))
- Fold ha_check_config into ha_get_system_health include="config_check"
  ([#1516](https://github.com/homeassistant-ai/ha-mcp/pull/1516))
- **c901**: Smart_search.py below C901 threshold
  ([#1507](https://github.com/homeassistant-ai/ha-mcp/pull/1507))
- **hacs**: Consolidate HACS tools into ha_get_hacs + ha_manage_hacs (#1045)
  ([#1502](https://github.com/homeassistant-ai/ha-mcp/pull/1502))
- **c901**: Tools_config_helpers.py below C901 threshold
  ([#1498](https://github.com/homeassistant-ai/ha-mcp/pull/1498))
- **complexity**: Reduce C901 in tools_addons.py via class-based pattern
  ([#1432](https://github.com/homeassistant-ai/ha-mcp/pull/1432))

### Fixed

- **tools**: Bring HA API field names to 2026.6 — stale docstrings, automation plural canonicalization, fan speed (closes #1540)
  ([#1566](https://github.com/homeassistant-ai/ha-mcp/pull/1566))
- **energy**: Accept "water" energy source in ha_manage_energy_prefs
  ([#1553](https://github.com/homeassistant-ai/ha-mcp/pull/1553))
- **tools**: Use action: not service: in automation docstring examples
  ([#1539](https://github.com/homeassistant-ai/ha-mcp/pull/1539))
- **security**: Narrow GHSA-mc92-ww4q-6fg4 to the masker and log redaction
  ([#1512](https://github.com/homeassistant-ai/ha-mcp/pull/1512))
- **addon**: Restrict settings UI root routes to HA ingress
  ([#1508](https://github.com/homeassistant-ai/ha-mcp/pull/1508))
- Add name attributes to generated settings-UI form controls (a11y)
  ([#1497](https://github.com/homeassistant-ai/ha-mcp/pull/1497))
- Add-on-aware code-mode locked note + suppress settings-UI favicon 404
  ([#1494](https://github.com/homeassistant-ai/ha-mcp/pull/1494))
- Surface the real reason a WebSocket connection failed
  ([#1495](https://github.com/homeassistant-ai/ha-mcp/pull/1495))
- Remove str from ha_bulk_control.operations schema + fix wrong-reason test
  ([#1492](https://github.com/homeassistant-ai/ha-mcp/pull/1492))
- Remove str from bool/int param schemas across all tools
  ([#1490](https://github.com/homeassistant-ai/ha-mcp/pull/1490))
- **addon**: Expose non-beta tool options on the stable add-on
  ([#1488](https://github.com/homeassistant-ai/ha-mcp/pull/1488))
- **addon**: Enable ingress so the stable add-on shows the Open Web UI / Settings UI
  ([#1486](https://github.com/homeassistant-ai/ha-mcp/pull/1486))
- Refine last_changed/last_updated duration-math detector (#1157)
  ([#1483](https://github.com/homeassistant-ai/ha-mcp/pull/1483))
- Remove str from config param schema on service and entity tools
  ([#1487](https://github.com/homeassistant-ai/ha-mcp/pull/1487))
- Surface flow-helper config to agents reading UI-created templates
  ([#1474](https://github.com/homeassistant-ai/ha-mcp/pull/1474))
- Remove str from config param schema on set tools
  ([#1485](https://github.com/homeassistant-ai/ha-mcp/pull/1485))
- Route malformed ha_mcp_tools version to a distinct reinstall error
  ([#1484](https://github.com/homeassistant-ai/ha-mcp/pull/1484))
- Persist DCR client registrations and HMAC secret across restarts (#1261)
  ([#1265](https://github.com/homeassistant-ai/ha-mcp/pull/1265))
- Stop dev builds from publishing the :latest Docker tag
  ([#1477](https://github.com/homeassistant-ai/ha-mcp/pull/1477))
- Subscribe to HACS dispatch signal instead of 10x1s blind poll
  ([#1455](https://github.com/homeassistant-ai/ha-mcp/pull/1455))
- Reject python_transform while loops
  ([#1462](https://github.com/homeassistant-ai/ha-mcp/pull/1462))

---
<details>
<summary>Internal Changes</summary>


### Changed

- **security**: Note that security-advisory disposition is API-blind (UI-only)
  ([#1561](https://github.com/homeassistant-ai/ha-mcp/pull/1561))
- Trim AGENTS.md below 40k + improve subdirectory CLAUDE.md files
  ([#1499](https://github.com/homeassistant-ai/ha-mcp/pull/1499))
- Extract settings-UI JavaScript and CSS to separate files
  ([#1505](https://github.com/homeassistant-ai/ha-mcp/pull/1505))
- Collapse settings-UI route registration into one table
  ([#1504](https://github.com/homeassistant-ai/ha-mcp/pull/1504))

### Chores

- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.6.1
  ([#1571](https://github.com/homeassistant-ai/ha-mcp/pull/1571))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.19
  ([#1570](https://github.com/homeassistant-ai/ha-mcp/pull/1570))
- Sync tool docs after merge [skip ci]
  ([`8f5e037`](https://github.com/homeassistant-ai/ha-mcp/commit/8f5e037b89296fd483888d158d8f72f4b1b7dfa8))
- Sync tool docs after merge [skip ci]
  ([`4341e95`](https://github.com/homeassistant-ai/ha-mcp/commit/4341e9574b3270c67bc016676f854f585fec816f))
- Sync tool docs after merge [skip ci]
  ([`b642b8e`](https://github.com/homeassistant-ai/ha-mcp/commit/b642b8eba67d8015d8fe5a1c1f88c9dd72ffc45f))
- Sync tool docs after merge [skip ci]
  ([`dd72535`](https://github.com/homeassistant-ai/ha-mcp/commit/dd725357f5d48a00959aaa7eceb4bad67a7ba202))
- Sync tool docs after merge [skip ci]
  ([`7464277`](https://github.com/homeassistant-ai/ha-mcp/commit/746427745d3debccea57c844278d1ba834d7ebfb))
- Sync tool docs after merge [skip ci]
  ([`85f3935`](https://github.com/homeassistant-ai/ha-mcp/commit/85f3935f80d9677f791b3098b201ec9c27e643be))
- Sync tool docs after merge [skip ci]
  ([`d9adbd2`](https://github.com/homeassistant-ai/ha-mcp/commit/d9adbd2c27750c9bc8faa55470d24564c48b3dbf))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.18
  ([#1523](https://github.com/homeassistant-ai/ha-mcp/pull/1523))
- Sync tool docs after merge [skip ci]
  ([`e2c08ff`](https://github.com/homeassistant-ai/ha-mcp/commit/e2c08ff797ffabf7b39bfc4b22b498b1c02ce1fa))
- Sync tool docs after merge [skip ci]
  ([`e70a050`](https://github.com/homeassistant-ai/ha-mcp/commit/e70a050efcb6edeb65d51dabd5307a192a22e66c))
- Sync tool docs after merge [skip ci]
  ([`6df7987`](https://github.com/homeassistant-ai/ha-mcp/commit/6df79877669686a9fee3ec04979dff3960bc5e3f))
- Sync tool docs after merge [skip ci]
  ([`6f88e7e`](https://github.com/homeassistant-ai/ha-mcp/commit/6f88e7e0c8dcb7fee27792e0f87f6acd8aa892c4))
- Sync tool docs after merge [skip ci]
  ([`ac2c7d0`](https://github.com/homeassistant-ai/ha-mcp/commit/ac2c7d038a90fb44466afa8f047378d7e8daed94))
- Flag untrusted third-party content in HACS and add-on tool responses
  ([#1509](https://github.com/homeassistant-ai/ha-mcp/pull/1509))
- Sync tool docs after merge [skip ci]
  ([`94fcbeb`](https://github.com/homeassistant-ai/ha-mcp/commit/94fcbeb14bd9cfa3bef274182f95e4562a379971))
- Drop dead entity_cache attr + ruff-format fuzzy_search.py
  ([#1503](https://github.com/homeassistant-ai/ha-mcp/pull/1503))
- Sync tool docs after merge [skip ci]
  ([`f36cbb7`](https://github.com/homeassistant-ai/ha-mcp/commit/f36cbb7e620839b16808b37b61dbc7602557b409))
- Sync tool docs after merge [skip ci]
  ([`50d76b0`](https://github.com/homeassistant-ai/ha-mcp/commit/50d76b04a0691c565fafaabc76fd4bd3cf321953))
- Sync tool docs after merge [skip ci]
  ([`1f03967`](https://github.com/homeassistant-ai/ha-mcp/commit/1f03967160156cbffdfc9fc7deb5ed9a2b25f2ba))
- Sync tool docs after merge [skip ci]
  ([`ba8fae3`](https://github.com/homeassistant-ai/ha-mcp/commit/ba8fae3cc4f451dc65022756a86cce5275f6e35f))
- Sync tool docs after merge [skip ci]
  ([`f9b55ad`](https://github.com/homeassistant-ai/ha-mcp/commit/f9b55ad59c6f03d6a98c3d3f6dd52ba9ff7c8a7a))
- Sync tool docs after merge [skip ci]
  ([`ef16a8a`](https://github.com/homeassistant-ai/ha-mcp/commit/ef16a8a197c76e51a49ae6ac69eecb15d202a194))
- Sync tool docs after merge [skip ci]
  ([`d0a4482`](https://github.com/homeassistant-ai/ha-mcp/commit/d0a448276306a08f8a52c4645f1b9c5aeaafd68e))
- Sync tool docs after merge [skip ci]
  ([`4e3fd5f`](https://github.com/homeassistant-ai/ha-mcp/commit/4e3fd5f331ce5fefa8e0e5560da411de17f2a8db))
- Sync tool docs after merge [skip ci]
  ([`5307b60`](https://github.com/homeassistant-ai/ha-mcp/commit/5307b608879154fccf67bc1894f61e97f26719c6))
- Sync tool docs after merge [skip ci]
  ([`be864b5`](https://github.com/homeassistant-ai/ha-mcp/commit/be864b5d685f167334487d4fe83324afbb4999ca))
- **addon**: Publish version 7.6.0 [skip ci]
  ([`086d75d`](https://github.com/homeassistant-ai/ha-mcp/commit/086d75d72dd7d1c735413d96ef2fd38ae57b90ca))

### Continuous Integration

- Vendor Puppet add-on as a pinned submodule + retry transient add-on builds
  ([#1565](https://github.com/homeassistant-ai/ha-mcp/pull/1565))
- Make sync-tool-docs push resilient to concurrent master advances
  ([#1564](https://github.com/homeassistant-ai/ha-mcp/pull/1564))
- Fix stale token-cap comment and harden triage budget tests (#1514)
  ([#1560](https://github.com/homeassistant-ai/ha-mcp/pull/1560))
- **deps**: Bump the github-actions group with 2 updates
  ([#1556](https://github.com/homeassistant-ai/ha-mcp/pull/1556))
- Budget triage prompt dynamically under the GitHub Models cap (#1514)
  ([#1522](https://github.com/homeassistant-ai/ha-mcp/pull/1522))
- Reduce Docker Hub pulls in performance-tests workflow
  ([#1549](https://github.com/homeassistant-ai/ha-mcp/pull/1549))
- Add JavaScript to the CodeQL code-quality gate
  ([#1548](https://github.com/homeassistant-ai/ha-mcp/pull/1548))
- Add CodeQL code-quality CI gate and clear all code-quality findings
  ([#1526](https://github.com/homeassistant-ai/ha-mcp/pull/1526))
- Drop tool list from evaluate prompt + tighten caps to fit 8K token limit
  ([`6f47c92`](https://github.com/homeassistant-ai/ha-mcp/commit/6f47c92507ef0d854153632107c0c702812f0c3c))
- Remove broken maintainer check (GITHUB_TOKEN lacks read:org)
  ([`010ec58`](https://github.com/homeassistant-ai/ha-mcp/commit/010ec58d1f3b6d46b20aa83c3caee373dd826694))
- Switch evaluate step to gpt-4o-mini (16K token free tier vs 8K on gpt-4.1)
  ([#1496](https://github.com/homeassistant-ai/ha-mcp/pull/1496))
- Issue bot v2 — GitHub Models triage, needs-info auto-close, duplicate detection
  ([#1442](https://github.com/homeassistant-ai/ha-mcp/pull/1442))

### Testing

- **oauth**: Add HTTP smoke tests for OAuth metadata-discovery endpoints
  ([#1562](https://github.com/homeassistant-ai/ha-mcp/pull/1562))
- **uat**: Relabel c01 as a mode-discrimination probe (taxonomy consistency)
  ([#1563](https://github.com/homeassistant-ai/ha-mcp/pull/1563))
- **uat**: Rework c01 routing probe to ha_search registry-listing mode after #1529
  ([#1559](https://github.com/homeassistant-ai/ha-mcp/pull/1559))
- **uat**: Add response_contains_any check and always log agent responses
  ([#1537](https://github.com/homeassistant-ai/ha-mcp/pull/1537))
- **uat**: Treat request timeout as per-story failure, not suite abort
  ([#1536](https://github.com/homeassistant-ai/ha-mcp/pull/1536))
- **uat**: Harden BAT story runner against agent crashes
  ([#1535](https://github.com/homeassistant-ai/ha-mcp/pull/1535))
- **uat**: Log model and quantization in BAT story results
  ([#1525](https://github.com/homeassistant-ai/ha-mcp/pull/1525))
- Capture reasoning tokens and detect inert --no-think in BAT openai agent
  ([#1524](https://github.com/homeassistant-ai/ha-mcp/pull/1524))
</details>


## v7.6.0 (2026-05-27)

### Added

- Make HTTP bind host configurable via MCP_HOST (closes #1434)
  ([#1436](https://github.com/homeassistant-ai/ha-mcp/pull/1436))
- Tool Security Policies — per-tool approval gating (#966)
  ([#1421](https://github.com/homeassistant-ai/ha-mcp/pull/1421))
- Rename ha_delete_helpers_integrations → ha_remove_helpers_integrations + raise on missing target
  ([#1424](https://github.com/homeassistant-ai/ha-mcp/pull/1424))
- Auto-backup edited entities before write/destructive tool calls (closes #1288)
  ([#1403](https://github.com/homeassistant-ai/ha-mcp/pull/1403))
- Persistent settings UI for stdio mode
  ([#1381](https://github.com/homeassistant-ai/ha-mcp/pull/1381))
- Add fields= projection to ha_search_entities, ha_get_overview, ha_get_state, ha_get_history, ha_config_list_areas, ha_list_services (#1199)
  ([#1225](https://github.com/homeassistant-ai/ha-mcp/pull/1225))
- Route entity-registration wait through WS events (closes #1152)
  ([#1382](https://github.com/homeassistant-ai/ha-mcp/pull/1382))
- Add config subentry support
  ([#1393](https://github.com/homeassistant-ai/ha-mcp/pull/1393))
- Add Assist pipeline management tool
  ([#1392](https://github.com/homeassistant-ai/ha-mcp/pull/1392))
- Add knx to ha_config_set_yaml allowlist
  ([#1374](https://github.com/homeassistant-ai/ha-mcp/pull/1374))
- Extend automation_id parity to set/remove automation responses
  ([#1343](https://github.com/homeassistant-ai/ha-mcp/pull/1343))
- Expose integration diagnostics via ha_get_integration and ha_get_system_health (closes #1148)
  ([#1328](https://github.com/homeassistant-ai/ha-mcp/pull/1328))
- Return canonical script_id from ha_config_get_script (#1334)
  ([#1352](https://github.com/homeassistant-ai/ha-mcp/pull/1352))
- Add automation_id parity key to ha_config_get_automation
  ([#1329](https://github.com/homeassistant-ai/ha-mcp/pull/1329))
- Reject empty/whitespace identifiers on registry-metadata writes (closes #1294)
  ([#1312](https://github.com/homeassistant-ai/ha-mcp/pull/1312))
- Add HA brand assets for custom integration
  ([#1317](https://github.com/homeassistant-ai/ha-mcp/pull/1317))
- Unify ha_config_set_helper response shape (closes #1293)
  ([#1303](https://github.com/homeassistant-ai/ha-mcp/pull/1303))
- Mirror create-side validation guards onto update path (closes #1292)
  ([#1304](https://github.com/homeassistant-ai/ha-mcp/pull/1304))
- Add array_patch mode to ha_manage_addon for atomic GET-modify-POST
  ([#1063](https://github.com/homeassistant-ai/ha-mcp/pull/1063))

### Changed

- **service**: Compact ha_call_service result default (#1446)
  ([#1447](https://github.com/homeassistant-ai/ha-mcp/pull/1447))
- Rename ha_update_device → ha_set_device
  ([#1444](https://github.com/homeassistant-ai/ha-mcp/pull/1444))
- Remove duplicate flat area/floor list tools (consolidation followup to #1016)
  ([#1429](https://github.com/homeassistant-ai/ha-mcp/pull/1429))
- **complexity**: Migrate tools_utility.py to class-based pattern
  ([#1423](https://github.com/homeassistant-ai/ha-mcp/pull/1423))
- **complexity**: Reduce C901 violations in tools/ — batch 4
  ([#1408](https://github.com/homeassistant-ai/ha-mcp/pull/1408))
- Route _poll_for_automation_entity through WS event waiter (closes #1395)
  ([#1406](https://github.com/homeassistant-ai/ha-mcp/pull/1406))
- **yaml**: Use threading.local subclass for cached instance
  ([#1396](https://github.com/homeassistant-ai/ha-mcp/pull/1396))
- Align dashboards 404 shape with sibling config tools
  ([#1386](https://github.com/homeassistant-ai/ha-mcp/pull/1386))
- Complete singular warning → warnings list migration repo-wide (closes #1332)
  ([#1341](https://github.com/homeassistant-ai/ha-mcp/pull/1341))
- Complete warnings-list migration for lifecycle-write tools
  ([#1340](https://github.com/homeassistant-ai/ha-mcp/pull/1340))
- Drop redundant identifier echo key from ha_config_get_automation
  ([#1354](https://github.com/homeassistant-ai/ha-mcp/pull/1354))
- Drop logger.error in config-tool except blocks (#1302)
  ([#1353](https://github.com/homeassistant-ai/ha-mcp/pull/1353))
- Extend validate_identifier_not_empty to automations/scripts/dashboards CRUD (closes #1313)
  ([#1321](https://github.com/homeassistant-ai/ha-mcp/pull/1321))
- Migrate tools_config_scenes inline empty-id guards to shared helper
  ([#1320](https://github.com/homeassistant-ai/ha-mcp/pull/1320))
- Remove ha_get_helper_schema (closes #1186)
  ([#1315](https://github.com/homeassistant-ai/ha-mcp/pull/1315))
- Consolidate skill tools; fix stable submodule packaging
  ([#1289](https://github.com/homeassistant-ai/ha-mcp/pull/1289))
- Align tools_config_automations.py error-handling with sibling pattern (#1290)
  ([#1298](https://github.com/homeassistant-ai/ha-mcp/pull/1298))
- Update contributors list [contributors-updated]
  ([`c7665a6`](https://github.com/homeassistant-ai/ha-mcp/commit/c7665a6f5a08737d8fcbebbbe6afdc88ec5c4901))
- **overview**: Enumerate dismissed_repair_count in fields= description + static drift test
  ([#1411](https://github.com/homeassistant-ai/ha-mcp/pull/1411))
- Credit @tomwilkie and six other contributors in README
  ([#1400](https://github.com/homeassistant-ai/ha-mcp/pull/1400))
- **#1157**: Bump skills-vendor + auto-update via Renovate + native for: field + scrub eval_template anti-patterns
  ([#1383](https://github.com/homeassistant-ai/ha-mcp/pull/1383))
- Surface tool-discovery / categorized search
  ([#1123](https://github.com/homeassistant-ai/ha-mcp/pull/1123))
- Fix two stale ha_get_skill_guide references missed in #1289
  ([#1305](https://github.com/homeassistant-ai/ha-mcp/pull/1305))
- Clarify setup wizard placeholders need braces removed (#1284)
  ([#1286](https://github.com/homeassistant-ai/ha-mcp/pull/1286))
- Tighten _poll_for_automation_entity first-poll cadence
  ([#1384](https://github.com/homeassistant-ai/ha-mcp/pull/1384))
- Parallelize ha_get_system_health optional sections via asyncio.gather
  ([#1336](https://github.com/homeassistant-ai/ha-mcp/pull/1336))

### Fixed

- Remove counter from ha_reload_core targets (#1453)
  ([#1456](https://github.com/homeassistant-ai/ha-mcp/pull/1456))
- **backup**: Post-timeout match correctness + state-gate (closes #1433)
  ([#1435](https://github.com/homeassistant-ai/ha-mcp/pull/1435))
- Sync addon settings UI with Supervisor options end-to-end
  ([#1420](https://github.com/homeassistant-ai/ha-mcp/pull/1420))
- **calendar**: Switch ha_config_remove_calendar_event to WebSocket (closes #1413, #1416)
  ([#1418](https://github.com/homeassistant-ai/ha-mcp/pull/1418))
- Error-shape consistency for non-entity not-found (closes #1297)
  ([#1397](https://github.com/homeassistant-ai/ha-mcp/pull/1397))
- Guard against silent automation overwrite on id mismatch (#1404)
  ([#1405](https://github.com/homeassistant-ai/ha-mcp/pull/1405))
- Cache YAML instance to prevent CPU spikes in bulk edits (#1370)
  ([#1371](https://github.com/homeassistant-ai/ha-mcp/pull/1371))
- **client**: Route get_error_log via hassio proxy on external-HAOS clients
  ([#1360](https://github.com/homeassistant-ai/ha-mcp/pull/1360))
- Classify dashboard 404s ("unknown config specified") as RESOURCE_NOT_FOUND
  ([#1345](https://github.com/homeassistant-ai/ha-mcp/pull/1345))
- Detect HA addon installs as http transport, not stdio (#1322)
  ([#1327](https://github.com/homeassistant-ai/ha-mcp/pull/1327))
- Actionable 403 suggestion when addon has unmapped container ports (#1319)
  ([#1325](https://github.com/homeassistant-ai/ha-mcp/pull/1325))
- Filter dismissed repairs in overview and system_health (#1307)
  ([#1309](https://github.com/homeassistant-ai/ha-mcp/pull/1309))
- Align ha_config_set_dashboard with sibling re-fetch-after-save pattern (#1291)
  ([#1301](https://github.com/homeassistant-ai/ha-mcp/pull/1301))
- Allow str.replace in python_transform; hint at search mode on IndexError
  ([#1287](https://github.com/homeassistant-ai/ha-mcp/pull/1287))
- **array_patch**: Tighten validation and surface silent failures
  ([#1285](https://github.com/homeassistant-ai/ha-mcp/pull/1285))
- HA Core proxy fallback for ha_get_logs(source=system_service) on non-addon installs
  ([#1283](https://github.com/homeassistant-ai/ha-mcp/pull/1283))

---
<details>
<summary>Internal Changes</summary>


### Added

- **haos-e2e**: Add parallel inaddon test tier (ha-mcp runs inside HAOS addon)
  ([#1361](https://github.com/homeassistant-ai/ha-mcp/pull/1361))

### Changed

- Consolidate lovelace/dashboards/list through shared helper
  ([#1344](https://github.com/homeassistant-ai/ha-mcp/pull/1344))
- **agents**: Drop ha_backup_create + ha_backup_restore from accepted exceptions
  ([#1445](https://github.com/homeassistant-ai/ha-mcp/pull/1445))
- Extend Boy Scout weasel-phrase list with common variants; clarify semantic match
  ([#1373](https://github.com/homeassistant-ai/ha-mcp/pull/1373))
- Merge Boy Scout Rule + Handling Discovered Improvements; tighten deferral gate
  ([#1359](https://github.com/homeassistant-ai/ha-mcp/pull/1359))
- Categorize Issue Labels table and document 6 reverse-drift labels
  ([#1335](https://github.com/homeassistant-ai/ha-mcp/pull/1335))
- Strip stale L-refs from test_identifier_validation_family docstrings
  ([#1324](https://github.com/homeassistant-ai/ha-mcp/pull/1324))
- Align label refs with live label set and fix triaged-removal trigger
  ([#1316](https://github.com/homeassistant-ai/ha-mcp/pull/1316))

### Fixed

- **ci**: Install libguestfs in HAOS publish workflow
  ([#1358](https://github.com/homeassistant-ai/ha-mcp/pull/1358))
- Exit on HA container death + daily reset before CI check
  ([#1295](https://github.com/homeassistant-ai/ha-mcp/pull/1295))

### Build System

- **deps**: Bump esbuild from 0.24.2 to 0.25.0 in /tests/js
  ([#1427](https://github.com/homeassistant-ai/ha-mcp/pull/1427))
- **deps**: Bump devalue from 5.6.4 to 5.8.1 in /site
  ([#1282](https://github.com/homeassistant-ai/ha-mcp/pull/1282))
- **deps**: Bump astro from 6.1.6 to 6.1.10 in /site
  ([#1274](https://github.com/homeassistant-ai/ha-mcp/pull/1274))

### Chores

- Sync tool docs after merge [skip ci]
  ([`9c4984f`](https://github.com/homeassistant-ai/ha-mcp/commit/9c4984fc44d30c15a96372fbc1b401fbd679dc8f))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.5.4
  ([#1450](https://github.com/homeassistant-ai/ha-mcp/pull/1450))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.16
  ([#1449](https://github.com/homeassistant-ai/ha-mcp/pull/1449))
- Sync tool docs after merge [skip ci]
  ([`dc7750d`](https://github.com/homeassistant-ai/ha-mcp/commit/dc7750de16b60f70e40e6e5c8908d9e84e544683))
- Sync tool docs after merge [skip ci]
  ([`8a79837`](https://github.com/homeassistant-ai/ha-mcp/commit/8a798373a7b2f1adb94b93e65d626ab582c7bd5d))
- Sync tool docs after merge [skip ci]
  ([`9e0493d`](https://github.com/homeassistant-ai/ha-mcp/commit/9e0493d248e23a8042841e90847662926395a448))
- Sync tool docs after merge [skip ci]
  ([`7bdb3d4`](https://github.com/homeassistant-ai/ha-mcp/commit/7bdb3d402b60900a1012b8269e52b7cbeb400f84))
- Sync tool docs after merge [skip ci]
  ([`393b354`](https://github.com/homeassistant-ai/ha-mcp/commit/393b354e8a8e1280029eee9521971c693526970a))
- Sync tool docs after merge [skip ci]
  ([`fb35f30`](https://github.com/homeassistant-ai/ha-mcp/commit/fb35f305cbf7de6807c29bd3ada9da2f028ff730))
- Sync tool docs after merge [skip ci]
  ([`f6796ec`](https://github.com/homeassistant-ai/ha-mcp/commit/f6796ec19b4596455ff54b58746bd3e075d5d280))
- Sync tool docs after merge [skip ci]
  ([`7525e93`](https://github.com/homeassistant-ai/ha-mcp/commit/7525e930a872871fed78f9b1461aea8e78eb5ad7))
- Sync tool docs after merge [skip ci]
  ([`9a5bc3c`](https://github.com/homeassistant-ai/ha-mcp/commit/9a5bc3c61372159bb936d7b929166924343dc76f))
- Sync tool docs after merge [skip ci]
  ([`499ebf0`](https://github.com/homeassistant-ai/ha-mcp/commit/499ebf0c9d4c4ec3784dd72782fe301cd9c19d60))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.15
  ([#1376](https://github.com/homeassistant-ai/ha-mcp/pull/1376))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.5.3
  ([#1377](https://github.com/homeassistant-ai/ha-mcp/pull/1377))
- Sync tool docs after merge [skip ci]
  ([`5b7a8aa`](https://github.com/homeassistant-ai/ha-mcp/commit/5b7a8aa0794359a90d7b89a9cc61d73d8cf038d2))
- Sync tool docs after merge [skip ci]
  ([`8567c3a`](https://github.com/homeassistant-ai/ha-mcp/commit/8567c3a6e66e7bebbbad8e96b94058f8456deb14))
- Sync tool docs after merge [skip ci]
  ([`ab68c9a`](https://github.com/homeassistant-ai/ha-mcp/commit/ab68c9a9aa94ac6861af7b59997ec679f1849503))
- Sync tool docs after merge [skip ci]
  ([`a72a4e8`](https://github.com/homeassistant-ai/ha-mcp/commit/a72a4e8c0de99a8516f3820545642c37a31c17e5))
- Sync tool docs after merge [skip ci]
  ([`137e279`](https://github.com/homeassistant-ai/ha-mcp/commit/137e27943b1b704fe1d3e3a5b525b4d82bce33eb))
- Sync tool docs after merge [skip ci]
  ([`9a753d4`](https://github.com/homeassistant-ai/ha-mcp/commit/9a753d49abb4ccd84e874451dbcc563260f9e19d))
- Sync tool docs after merge [skip ci]
  ([`9e6cff8`](https://github.com/homeassistant-ai/ha-mcp/commit/9e6cff8f641bcd8122e94fc8be9c30aa18456e80))
- Sync tool docs after merge [skip ci]
  ([`23789fa`](https://github.com/homeassistant-ai/ha-mcp/commit/23789fa978591b7d3966894082a2683ca6a6ae3b))
- Sync tool docs after merge [skip ci]
  ([`d2329cb`](https://github.com/homeassistant-ai/ha-mcp/commit/d2329cbff39891d209317528fd7b42759c3414a8))
- Sync tool docs after merge [skip ci]
  ([`f70f0e1`](https://github.com/homeassistant-ai/ha-mcp/commit/f70f0e14222b29a8923e706f113d1fafb4a5c23e))
- **addon**: Publish version 7.5.0 [skip ci]
  ([`9c5eb37`](https://github.com/homeassistant-ai/ha-mcp/commit/9c5eb37779236ef19366a2a59a667d3916458e5a))

### Continuous Integration

- **deps**: Bump actions/upload-artifact in the github-actions group
  ([#1437](https://github.com/homeassistant-ai/ha-mcp/pull/1437))
- Share qcow2 cache + GHCR fallback between HAOS lanes
  ([#1407](https://github.com/homeassistant-ai/ha-mcp/pull/1407))
- Add ruff format --check on changed Python files
  ([#1387](https://github.com/homeassistant-ai/ha-mcp/pull/1387))
- Exempt assigned issues from stale bot
  ([#1368](https://github.com/homeassistant-ai/ha-mcp/pull/1368))
- **deps**: Bump the github-actions group with 3 updates
  ([#1362](https://github.com/homeassistant-ai/ha-mcp/pull/1362))

### Testing

- **haos-e2e**: Bake + install webhook-proxy addon and exercise its runtime
  ([#1443](https://github.com/homeassistant-ai/ha-mcp/pull/1443))
- **config-subentry**: Mark forecast_solar e2e as known flaky + relative-import sweep
  ([#1430](https://github.com/homeassistant-ai/ha-mcp/pull/1430))
- **haos-e2e**: Trim cache-save race, compress GHCR qcow2, eval boot snapshot
  ([#1428](https://github.com/homeassistant-ai/ha-mcp/pull/1428))
- JSDOM behaviour harness + auto-discovery parse coverage for every rendered <script>
  ([#1425](https://github.com/homeassistant-ai/ha-mcp/pull/1425))
- **hacs**: Retry TestMcpToolsInstallation on flake
  ([#1426](https://github.com/homeassistant-ai/ha-mcp/pull/1426))
- **e2e**: Drop redundant lifecycle roundtrips, keep only Matter Server (#1414)
  ([#1419](https://github.com/homeassistant-ai/ha-mcp/pull/1419))
- **e2e**: Assert backend dispatch matches workflow env on every lane
  ([#1409](https://github.com/homeassistant-ai/ha-mcp/pull/1409))
- Escape ideographic space and format file (#1237)
  ([#1410](https://github.com/homeassistant-ai/ha-mcp/pull/1410))
- **e2e**: Measure _POLL_CADENCE p50/p99 to validate or retune (closes #1389)
  ([#1398](https://github.com/homeassistant-ai/ha-mcp/pull/1398))
- **e2e**: Wait for addon state=started in haos proxy header test
  ([#1402](https://github.com/homeassistant-ai/ha-mcp/pull/1402))
- Pin remaining _classify_by_message branches
  ([#1385](https://github.com/homeassistant-ai/ha-mcp/pull/1385))
- **haos-e2e**: Slim addon set + real-addon ha_manage_addon coverage (closes #1350)
  ([#1379](https://github.com/homeassistant-ai/ha-mcp/pull/1379))
- **haos-e2e**: Close out #1349 — lifecycle, integrations, supervisor_mock migration, no more skips
  ([#1375](https://github.com/homeassistant-ai/ha-mcp/pull/1375))
- **e2e**: Consolidate readiness gates onto /api/core/state (refs #366)
  ([#1372](https://github.com/homeassistant-ai/ha-mcp/pull/1372))
- **e2e**: Tighten 5 readiness-gate budgets with 2-63x headroom (refs #366)
  ([#1369](https://github.com/homeassistant-ai/ha-mcp/pull/1369))
- Scaffold HAOS E2E tier image-build pipeline (refs #1281)
  ([#1326](https://github.com/homeassistant-ai/ha-mcp/pull/1326))
- **e2e**: Instrument HA_MCP_TOOLS_WAIT readiness gate (refs #366)
  ([#1346](https://github.com/homeassistant-ai/ha-mcp/pull/1346))
- **e2e**: Centralize wait_for_entity_registration helper (refs #366)
  ([#1308](https://github.com/homeassistant-ai/ha-mcp/pull/1308))
- **e2e**: Unify dict-error message extraction across e2e tests (refs #366)
  ([#1311](https://github.com/homeassistant-ai/ha-mcp/pull/1311))
- **e2e**: Surface readiness-gate elapsed times in CI logs (refs #366)
  ([#1310](https://github.com/homeassistant-ai/ha-mcp/pull/1310))
</details>


## v7.5.0 (2026-05-13)

### Added

- Add ENABLE_LITE_DOCSTRINGS beta toggle
  ([#1259](https://github.com/homeassistant-ai/ha-mcp/pull/1259))
- Add ha_call_event tool for publishing events on the HA event bus (#996)
  ([#1239](https://github.com/homeassistant-ai/ha-mcp/pull/1239))
- Pinpoint backslash-escape mistake in python_sandbox errors
  ([#1204](https://github.com/homeassistant-ai/ha-mcp/pull/1204))
- Reject empty-trigger automations targeting scene.create
  ([#1187](https://github.com/homeassistant-ai/ha-mcp/pull/1187))
- Add scene config tools — ha_config_get/set/remove_scene
  ([#1168](https://github.com/homeassistant-ai/ha-mcp/pull/1168))
- **addon**: Optional OAuth 2.1 mode for webhook proxy (beta)
  ([#1184](https://github.com/homeassistant-ai/ha-mcp/pull/1184))
- Surface helper schema inline in ha_config_set_helper validation errors (#1149)
  ([#1179](https://github.com/homeassistant-ai/ha-mcp/pull/1179))
- Emit progress via FastMCP Context in long-running tools
  ([#1124](https://github.com/homeassistant-ai/ha-mcp/pull/1124))
- Broaden python_transform AST allowlist + improve error UX
  ([#1163](https://github.com/homeassistant-ai/ha-mcp/pull/1163))
- Add ha_manage_custom_tool — sandboxed code execution escape hatch
  ([#854](https://github.com/homeassistant-ai/ha-mcp/pull/854))
- Always-on skills; rename list/read resource tools with ha_ prefix
  ([#1136](https://github.com/homeassistant-ai/ha-mcp/pull/1136))
- Expose device_class + options on ha_set_entity / ha_get_entity (Show As)
  ([#1135](https://github.com/homeassistant-ai/ha-mcp/pull/1135))
- **site**: Inline wizard data into setup.astro, migrate setup nuggets, drop content collections
  ([#1120](https://github.com/homeassistant-ai/ha-mcp/pull/1120))
- Add "Advanced debug logging" toggle for kill-signal diagnostics
  ([#1117](https://github.com/homeassistant-ai/ha-mcp/pull/1117))
- **yaml**: Scoped lovelace.dashboards.<url_path> support (issue #1034)
  ([#1103](https://github.com/homeassistant-ai/ha-mcp/pull/1103))
- Add HA_VERIFY_SSL toggle to disable TLS verification
  ([#1104](https://github.com/homeassistant-ai/ha-mcp/pull/1104))
- Per-top-level-key config_hash for ha_manage_energy_prefs (#1049)
  ([#1098](https://github.com/homeassistant-ai/ha-mcp/pull/1098))
- **site**: Add gemini-cli setup notes + compose hardening to wizard (#1027)
  ([#1087](https://github.com/homeassistant-ai/ha-mcp/pull/1087))
- Add convenience modes to ha_manage_energy_prefs (#1050)
  ([#1073](https://github.com/homeassistant-ai/ha-mcp/pull/1073))
- Surface integration log levels in ha_get_logs/integration/addon (#956)
  ([#1003](https://github.com/homeassistant-ai/ha-mcp/pull/1003))
- Expose allowlist_external_dirs in ha_get_overview full system_info
  ([#1053](https://github.com/homeassistant-ai/ha-mcp/pull/1053))
- **dashboards**: Unify identifier handling in ha_config_*_dashboard tools (#981)
  ([#1075](https://github.com/homeassistant-ai/ha-mcp/pull/1075))
- Include addon container logs in bug reports
  ([#934](https://github.com/homeassistant-ai/ha-mcp/pull/934))
- Add WebSocket response-shaping controls to ha_manage_addon
  ([#1009](https://github.com/homeassistant-ai/ha-mcp/pull/1009))
- Web-based settings UI for per-tool enable/disable/pin
  ([#960](https://github.com/homeassistant-ai/ha-mcp/pull/960))
- **site**: Add OpenCode support to setup wizard
  ([#1080](https://github.com/homeassistant-ai/ha-mcp/pull/1080))

### Changed

- Drop obsolete ha_mcp_tools defensive ruamel.yaml imports (post-#1268)
  ([#1269](https://github.com/homeassistant-ai/ha-mcp/pull/1269))
- Extract shared Supervisor httpx client helper (#1130)
  ([#1203](https://github.com/homeassistant-ai/ha-mcp/pull/1203))
- Surface client identity, AI model, config toggles, and prompt context in ha_report_issue
  ([#1189](https://github.com/homeassistant-ai/ha-mcp/pull/1189))
- Harden Context injection with safe-emit + branch coverage
  ([#1173](https://github.com/homeassistant-ai/ha-mcp/pull/1173))
- Consolidate area/floor set+remove tools (revisit of #813)
  ([#1139](https://github.com/homeassistant-ai/ha-mcp/pull/1139))
- Pass verify_ssl to remaining direct-Supervisor httpx callers
  ([#1128](https://github.com/homeassistant-ai/ha-mcp/pull/1128))
- Validate only new entries on convenience-mode writes (#1086)
  ([#1100](https://github.com/homeassistant-ai/ha-mcp/pull/1100))
- Dedupe lovelace/dashboards/list in ha_config_set_dashboard (#1085)
  ([#1191](https://github.com/homeassistant-ai/ha-mcp/pull/1191))
- Clarify standard-mode HTTP deployment guidance
  ([#1185](https://github.com/homeassistant-ai/ha-mcp/pull/1185))
- Add Cloudflared add-on hostname alternative for tunnel service
  ([#1183](https://github.com/homeassistant-ai/ha-mcp/pull/1183))
- Add brew install option for mcp-proxy on macOS
  ([#1171](https://github.com/homeassistant-ai/ha-mcp/pull/1171))
- Update contributors list [contributors-updated]
  ([`aba01a1`](https://github.com/homeassistant-ai/ha-mcp/commit/aba01a148f900c47eacf3afa7773667fe1ab757a))
- Warn against enable_tool_search on Claude Sonnet/Opus (#1088)
  ([#1140](https://github.com/homeassistant-ai/ha-mcp/pull/1140))
- Address #1094 review nits on OpenCode mirror comments
  ([#1105](https://github.com/homeassistant-ai/ha-mcp/pull/1105))

### Fixed

- **integrations**: Surface ConfigEntry.options via OptionsFlow probe
  ([#1245](https://github.com/homeassistant-ai/ha-mcp/pull/1245))
- **backup**: Discover local agent at call time instead of hardcoding hassio.local
  ([#1244](https://github.com/homeassistant-ai/ha-mcp/pull/1244))
- Triage all 10 ha_search_entities behaviors from #1170
  ([#1195](https://github.com/homeassistant-ai/ha-mcp/pull/1195))
- Improve ha_manage_addon discoverability (BM25 keywords + slug examples)
  ([#1200](https://github.com/homeassistant-ai/ha-mcp/pull/1200))
- Route Supervisor 401s to structured tool errors + add E2E coverage (#1129)
  ([#1192](https://github.com/homeassistant-ai/ha-mcp/pull/1192))
- Harden _validate_category_id gate to cover dict-promoted category
  ([#1190](https://github.com/homeassistant-ai/ha-mcp/pull/1190))
- Broaden template anti-pattern detection + skill discoverability (#1011)
  ([#1181](https://github.com/homeassistant-ai/ha-mcp/pull/1181))
- Return newest automation traces, add offset+order pagination (#1177)
  ([#1178](https://github.com/homeassistant-ai/ha-mcp/pull/1178))
- **security**: Write YAML backups outside www/ (GHSA-g39v-cvjh-8fpf)
  ([#1180](https://github.com/homeassistant-ai/ha-mcp/pull/1180))
- **search**: Apply domain_filter when area_filter is set (#1162)
  ([#1165](https://github.com/homeassistant-ai/ha-mcp/pull/1165))
- **resources**: Reject HA-config YAML in dashboard resource content
  ([#1160](https://github.com/homeassistant-ai/ha-mcp/pull/1160))
- Close 19 bugs in ha_config_set_helper (issue #1150)
  ([#1151](https://github.com/homeassistant-ai/ha-mcp/pull/1151))
- Route addon log fetches directly to supervisor on addon installs
  ([#1126](https://github.com/homeassistant-ai/ha-mcp/pull/1126))
- Survive read-only filesystems at startup
  ([#1138](https://github.com/homeassistant-ai/ha-mcp/pull/1138))
- **helpers**: Clarify name-required-on-create for ha_config_set_helper
  ([#1143](https://github.com/homeassistant-ai/ha-mcp/pull/1143))
- Resolve disabled entities via entity_registry in helper deletion
  ([#1119](https://github.com/homeassistant-ai/ha-mcp/pull/1119))
- Allow unary operators in python_transform sandbox
  ([#1118](https://github.com/homeassistant-ai/ha-mcp/pull/1118))
- **site**: Add github-copilot-agents wizard branch + delete unreferenced data/clients.ts
  ([#1108](https://github.com/homeassistant-ai/ha-mcp/pull/1108))
- **addons**: Route addon API calls through HA Core ingress proxy
  ([#1069](https://github.com/homeassistant-ai/ha-mcp/pull/1069))
- **webhook-proxy**: Surface webhook registration failures instead of silently loading
  ([#1101](https://github.com/homeassistant-ai/ha-mcp/pull/1101))
- **site**: Resolve client display-order collisions and anchor OpenCode shape
  ([#1094](https://github.com/homeassistant-ai/ha-mcp/pull/1094))

---
<details>
<summary>Internal Changes</summary>


### Changed

- Extract _fetch_dashboards_list helper (#1193)
  ([#1207](https://github.com/homeassistant-ai/ha-mcp/pull/1207))
- Align tool naming convention between AGENTS.md and styleguide (#943)
  ([#1174](https://github.com/homeassistant-ai/ha-mcp/pull/1174))
- **addon**: Note tool-list (#985)/runtime divergence; fix #1139/#1162 test conflict
  ([#1172](https://github.com/homeassistant-ai/ha-mcp/pull/1172))

### Fixed

- **ci**: Align pr.yml E2E with --dist loadscope (#1206)
  ([#1247](https://github.com/homeassistant-ai/ha-mcp/pull/1247))
- **ci**: Switch Renovate to a GitHub App token to allow workflow-file pushes
  ([#1229](https://github.com/homeassistant-ai/ha-mcp/pull/1229))
- Replace cron with systemd for demo server (prevents process leak)
  ([#1110](https://github.com/homeassistant-ai/ha-mcp/pull/1110))
- **ci**: Break gemini-triage retrigger loop and bump turn budget
  ([#1131](https://github.com/homeassistant-ai/ha-mcp/pull/1131))
- **ci**: Harden gemini-triage so failures stop spamming user issues
  ([#1122](https://github.com/homeassistant-ai/ha-mcp/pull/1122))
- **ci**: Unbreak hotfix-release semantic-release run
  ([#1091](https://github.com/homeassistant-ai/ha-mcp/pull/1091))

### Chores

- Sync tool docs after merge [skip ci]
  ([`6823c47`](https://github.com/homeassistant-ai/ha-mcp/commit/6823c473494802011ac0c4ccd60bc701b2b4a978))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.5.1
  ([#1236](https://github.com/homeassistant-ai/ha-mcp/pull/1236))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.13
  ([#1233](https://github.com/homeassistant-ai/ha-mcp/pull/1233))
- Sync tool docs after merge [skip ci]
  ([`d2ff93b`](https://github.com/homeassistant-ai/ha-mcp/commit/d2ff93b7ae756d285108012ec1d594806e2c6205))
- Sync tool docs after merge [skip ci]
  ([`c7e2066`](https://github.com/homeassistant-ai/ha-mcp/commit/c7e2066d3c9df53402e500e07fa3013db3bf621f))
- Sync tool docs after merge [skip ci]
  ([`093fd74`](https://github.com/homeassistant-ai/ha-mcp/commit/093fd743a7ffdc26c1768239eca6a0cdba712fa6))
- Sync tool docs after merge [skip ci]
  ([`7810c95`](https://github.com/homeassistant-ai/ha-mcp/commit/7810c95fc81b499481d552a24b86e6c32318bc4b))
- Sync tool docs after merge [skip ci]
  ([`a73dc81`](https://github.com/homeassistant-ai/ha-mcp/commit/a73dc8163aa6fca66f8a2cfdbc9723ea8fdb60fe))
- Sync tool docs after merge [skip ci]
  ([`a587be0`](https://github.com/homeassistant-ai/ha-mcp/commit/a587be0beac0ebcbe98c44f326fd1c54fc1bb374))
- Sync tool docs after merge [skip ci]
  ([`1210725`](https://github.com/homeassistant-ai/ha-mcp/commit/1210725557ac9322e6ea0ca684507d41b2ebde8b))
- Sync tool docs after merge [skip ci]
  ([`e03f5d2`](https://github.com/homeassistant-ai/ha-mcp/commit/e03f5d2d005dae14379530d9d561fb4772a0841c))
- Sync tool docs after merge [skip ci]
  ([`8a2bd1a`](https://github.com/homeassistant-ai/ha-mcp/commit/8a2bd1acde6560afe03a19587b10aaf753f72ed1))
- Sync tool docs after merge [skip ci]
  ([`0e6b54f`](https://github.com/homeassistant-ai/ha-mcp/commit/0e6b54f5ccf338788434f7066bfb2846f6b33136))
- Sync tool docs after merge [skip ci]
  ([`2702a0f`](https://github.com/homeassistant-ai/ha-mcp/commit/2702a0fe905a8f9b1fd0a12e486c73a310cbfd21))
- Sync tool docs after merge [skip ci]
  ([`c1f24b5`](https://github.com/homeassistant-ai/ha-mcp/commit/c1f24b5bd5b8a268e5f1ecf6619393897fc9997a))
- Sync tool docs after merge [skip ci]
  ([`c2ed2d3`](https://github.com/homeassistant-ai/ha-mcp/commit/c2ed2d30c722d2658c5fc257fa4cf57621261208))
- Sync tool docs after merge [skip ci]
  ([`085bd8a`](https://github.com/homeassistant-ai/ha-mcp/commit/085bd8a5f984dc0fac6fac25935fa009c9c2ab0a))
- Convert agents to skills
  ([#1084](https://github.com/homeassistant-ai/ha-mcp/pull/1084))
- Sync tool docs after merge [skip ci]
  ([`0f6d41e`](https://github.com/homeassistant-ai/ha-mcp/commit/0f6d41ea8b863b7dec68b14036746976ee2ac6f6))
- Sync tool docs after merge [skip ci]
  ([`9fa0aea`](https://github.com/homeassistant-ai/ha-mcp/commit/9fa0aea8ca5b38b48a5f958945dff6bca31d80c7))
- Sync tool docs after merge [skip ci]
  ([`ec7413f`](https://github.com/homeassistant-ai/ha-mcp/commit/ec7413feb7507df511da412327de57bf208218f6))
- Sync tool docs after merge [skip ci]
  ([`726f0a5`](https://github.com/homeassistant-ai/ha-mcp/commit/726f0a505bdbbb11853d463790d3d70932ddb4d8))
- Sync tool docs after merge [skip ci]
  ([`36719c3`](https://github.com/homeassistant-ai/ha-mcp/commit/36719c32704b829651f9b502ac2925179d7414df))
- Sync tool docs after merge [skip ci]
  ([`add66e3`](https://github.com/homeassistant-ai/ha-mcp/commit/add66e3310b48ad4898d37bd13f4fbcc14abc0f0))
- Sync tool docs after merge [skip ci]
  ([`0ca41af`](https://github.com/homeassistant-ai/ha-mcp/commit/0ca41afc8ae3cf0e694c2c93a0b2d705cdff4193))
- Bump package version to 7.4.1 to match released addon
  ([`4f65497`](https://github.com/homeassistant-ai/ha-mcp/commit/4f654975ecafe9cc00b8e5fd92e9b55d15875c7b))
- **addon**: Publish hotfix version 7.4.1
  ([`bda75e6`](https://github.com/homeassistant-ai/ha-mcp/commit/bda75e6efb20ec97109c077361bb3f9da1c25897))

### Continuous Integration

- **deps**: Bump renovatebot/github-action in the github-actions group
  ([#1218](https://github.com/homeassistant-ai/ha-mcp/pull/1218))
- **deps**: Bump renovatebot/github-action in the github-actions group
  ([#1111](https://github.com/homeassistant-ai/ha-mcp/pull/1111))

### Testing

- **e2e**: Module-scope bulk_automations + bulk_scripts fixtures (refs #366)
  ([#1275](https://github.com/homeassistant-ai/ha-mcp/pull/1275))
- **e2e**: Lower INPUT_BOOLEAN_WAIT from 30s to 10s (refs #366)
  ([#1273](https://github.com/homeassistant-ai/ha-mcp/pull/1273))
- **e2e**: Generalize readiness-gate diagnostics helper (closes #1267)
  ([#1271](https://github.com/homeassistant-ai/ha-mcp/pull/1271))
- **e2e**: Narrow except clauses in e2e polling helpers (closes #1266)
  ([#1270](https://github.com/homeassistant-ai/ha-mcp/pull/1270))
- **e2e**: Drop ha_mcp_tools retry-path + pre-install manifest requirements
  ([#1268](https://github.com/homeassistant-ai/ha-mcp/pull/1268))
- **e2e**: Instrument and retry ha_mcp_tools readiness wait
  ([#1262](https://github.com/homeassistant-ai/ha-mcp/pull/1262))
- Use time.monotonic() in UAT runner and test_env_manager
  ([#1254](https://github.com/homeassistant-ai/ha-mcp/pull/1254))
- **e2e**: Detect partial/corrupt hacs_frontend dir in fast-path guard
  ([#1253](https://github.com/homeassistant-ai/ha-mcp/pull/1253))
- **e2e**: Remove unused wait/assert helpers (post-#1249 audit)
  ([#1256](https://github.com/homeassistant-ai/ha-mcp/pull/1256))
- **e2e**: Clear stale .hacs_frontend.lock from prior crashed runs
  ([#1252](https://github.com/homeassistant-ai/ha-mcp/pull/1252))
- **e2e**: Use time.monotonic() in workflow polling loops
  ([#1258](https://github.com/homeassistant-ai/ha-mcp/pull/1258))
- **e2e**: Use time.monotonic() for duration polling (#1234)
  ([#1249](https://github.com/homeassistant-ai/ha-mcp/pull/1249))
- **e2e**: Close ARM ha_mcp_tools readiness race under loadscope
  ([#1208](https://github.com/homeassistant-ai/ha-mcp/pull/1208))
- **hacs**: Tighten is_hacs_unavailable to not match legitimate "Repository not found"
  ([#1246](https://github.com/homeassistant-ai/ha-mcp/pull/1246))
- **seed**: Unblock 3 silent-skip pagination/state tests via baked recorder DB
  ([#1240](https://github.com/homeassistant-ai/ha-mcp/pull/1240))
- **seed**: Register a writable local_calendar to unblock event-creation test
  ([#1243](https://github.com/homeassistant-ai/ha-mcp/pull/1243))
- **addon**: Fix base64 padding-bit flake in token tamper tests (#1238)
  ([#1241](https://github.com/homeassistant-ai/ha-mcp/pull/1241))
- **seed**: Add a writable scene for test_call_service_scene_turn_on
  ([#1231](https://github.com/homeassistant-ai/ha-mcp/pull/1231))
- **seed**: Assign demo device to living_room area for filter test
  ([#1230](https://github.com/homeassistant-ai/ha-mcp/pull/1230))
- **e2e**: Drop nonexistent sun service from session readiness wait
  ([#1227](https://github.com/homeassistant-ai/ha-mcp/pull/1227))
- **e2e**: Self-contain dashboard register/remove to fix ARM xdist race (#1196)
  ([#1201](https://github.com/homeassistant-ai/ha-mcp/pull/1201))
- Fix EN dash in docstring causing RUF002 lint failure
  ([`eac5916`](https://github.com/homeassistant-ai/ha-mcp/commit/eac5916e76cba9516c704369cd2a66c0c1f2ebeb))
- Address Gemini review feedback on host detection and port allocation
  ([`960305e`](https://github.com/homeassistant-ai/ha-mcp/commit/960305ec84a78d0da04e4f66c7a6d63b5870574c))
- Fix three categories of E2E test flakiness
  ([`39417ff`](https://github.com/homeassistant-ai/ha-mcp/commit/39417ffdcd6f420772b9a1075d391248a9ef7343))
- **e2e**: Pin config_hash stability for dashboards
  ([#1132](https://github.com/homeassistant-ai/ha-mcp/pull/1132))
</details>


## v7.4.1 (2026-05-02)

### Fixed

- **addon**: Propagate BUILD_VERSION so startup logs report correct version
  ([#1090](https://github.com/homeassistant-ai/ha-mcp/pull/1090))

---
<details>
<summary>Internal Changes</summary>


### Chores

- **addon**: Publish version 7.4.0 [skip ci]
  ([`fb7f1a1`](https://github.com/homeassistant-ai/ha-mcp/commit/fb7f1a1d2304ba25e41a9661aa7916094ad17e64))
</details>


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
- **search**: Make ha_search_entities query optional, clarify docs
  ([#1004](https://github.com/homeassistant-ai/ha-mcp/pull/1004))

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

---
<details>
<summary>Internal Changes</summary>


### Changed

- Recommend RFC 2119 terms for tool docstring constraints
  ([#947](https://github.com/homeassistant-ai/ha-mcp/pull/947))
- Fix broken resolve-thread example in AGENTS.md
  ([#949](https://github.com/homeassistant-ai/ha-mcp/pull/949))
- Inline-mode openai agent for BAT runner
  ([#1017](https://github.com/homeassistant-ai/ha-mcp/pull/1017))
- Reduce C901 complexity in 5 non-tool files
  ([#1000](https://github.com/homeassistant-ai/ha-mcp/pull/1000))

### Fixed

- Improve error guidance for small-model failure modes
  ([#1055](https://github.com/homeassistant-ai/ha-mcp/pull/1055))

### Build System

- **deps**: Bump postcss from 8.5.6 to 8.5.10 in /site
  ([#1052](https://github.com/homeassistant-ai/ha-mcp/pull/1052))
- **deps**: Bump astro from 5.18.1 to 6.1.6 in /site
  ([#1038](https://github.com/homeassistant-ai/ha-mcp/pull/1038))

### Chores

- Sync tool docs after merge [skip ci]
  ([`791279c`](https://github.com/homeassistant-ai/ha-mcp/commit/791279c90cd9a5945679f6cc0ae33346b714dbf4))
- Sync tool docs after merge [skip ci]
  ([`dbcde02`](https://github.com/homeassistant-ai/ha-mcp/commit/dbcde023cf8f3c9cab712d1ec5bd61a2307270ca))
- Sync tool docs after merge [skip ci]
  ([`994cfd6`](https://github.com/homeassistant-ai/ha-mcp/commit/994cfd6ca9a9ed37ae63ec5eac59ab0ccb3f905b))
- Sync tool docs after merge [skip ci]
  ([`e96ddd4`](https://github.com/homeassistant-ai/ha-mcp/commit/e96ddd4fb516c42e9e17f15477c67a543fb0c18e))
- Sync tool docs after merge [skip ci]
  ([`73712b7`](https://github.com/homeassistant-ai/ha-mcp/commit/73712b7644d8ee97ec6ea7cb4a6a02c05e0b75b5))
- Sync tool docs after merge [skip ci]
  ([`b3dec59`](https://github.com/homeassistant-ai/ha-mcp/commit/b3dec59a3cda79ef127f0335f615683d4a67d148))
- Sync tool docs after merge [skip ci]
  ([`13143a2`](https://github.com/homeassistant-ai/ha-mcp/commit/13143a2465f736ba2ec2c4c47a5dca3007dd2beb))
- **addon**: Publish version 7.3.0 [skip ci]
  ([`5e53405`](https://github.com/homeassistant-ai/ha-mcp/commit/5e534053903d68a0a512127ee7f587439908a136))

### Continuous Integration

- **deps**: Bump renovatebot/github-action in the github-actions group
  ([#1064](https://github.com/homeassistant-ai/ha-mcp/pull/1064))
- **deps**: Bump the github-actions group with 2 updates
  ([#1021](https://github.com/homeassistant-ai/ha-mcp/pull/1021))

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
- Document webhook proxy addon in README, setup site, and FAQ
  ([#931](https://github.com/homeassistant-ai/ha-mcp/pull/931))
- Add Windows pywin32 FAQ entry
  ([#933](https://github.com/homeassistant-ai/ha-mcp/pull/933))
- **security**: Add scope, out-of-scope, and OAuth beta warning
  ([#917](https://github.com/homeassistant-ai/ha-mcp/pull/917))
- Clarify ha_config_set_yaml comment preservation scope
  ([#920](https://github.com/homeassistant-ai/ha-mcp/pull/920))
- Update contributors list [contributors-updated]
  ([`934f573`](https://github.com/homeassistant-ai/ha-mcp/commit/934f5738bd89c1743df8fb9963d1caf5b304c363))
- Add macOS troubleshooting for local network connection issues
  ([#897](https://github.com/homeassistant-ai/ha-mcp/pull/897))
- Add custom component documentation and HACS install badge
  ([#877](https://github.com/homeassistant-ai/ha-mcp/pull/877))
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
- Optimize e2e test execution time
  ([#872](https://github.com/homeassistant-ai/ha-mcp/pull/872))

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
- Block registry-disable on automation/script entities (#794)
  ([#796](https://github.com/homeassistant-ai/ha-mcp/pull/796))

---
<details>
<summary>Internal Changes</summary>


### Changed

- Add Tests only type to PR template
  ([#953](https://github.com/homeassistant-ai/ha-mcp/pull/953))
- Clarify tool consolidation guidelines with anti-patterns
  ([#927](https://github.com/homeassistant-ai/ha-mcp/pull/927))
- Trim AGENTS.md to stay under 40k char Claude Code limit
  ([#922](https://github.com/homeassistant-ai/ha-mcp/pull/922))
- Add MCP tool docstring guidelines to AGENTS.md and styleguide
  ([#907](https://github.com/homeassistant-ai/ha-mcp/pull/907))
- Document sync-tool-docs.yml workflow in AGENTS.md
  ([#898](https://github.com/homeassistant-ai/ha-mcp/pull/898))
- Eliminate redundant file reads in check_sync
  ([#888](https://github.com/homeassistant-ai/ha-mcp/pull/888))

### Fixed

- Replace BAT blind sleep with deterministic HA readiness checks
  ([#939](https://github.com/homeassistant-ai/ha-mcp/pull/939))
- **addon**: Use unique version for dev add-on so HA detects updates
  ([#918](https://github.com/homeassistant-ai/ha-mcp/pull/918))
- Init submodules and use portable path in /wt skill
  ([#859](https://github.com/homeassistant-ai/ha-mcp/pull/859))

### Chores

- Sync tool docs after merge [skip ci]
  ([`39fd83e`](https://github.com/homeassistant-ai/ha-mcp/commit/39fd83e8b25185c075238f5f8c312e4f40c26212))
- Sync tool docs after merge [skip ci]
  ([`cdd59ca`](https://github.com/homeassistant-ai/ha-mcp/commit/cdd59cae96828aee36404e51208f8eb35f5f648b))
- Sync tool docs after merge [skip ci]
  ([`3378442`](https://github.com/homeassistant-ai/ha-mcp/commit/337844239428ab56d0e823e32f2b86425d39a022))
- Sync tool docs after merge [skip ci]
  ([`9930a8f`](https://github.com/homeassistant-ai/ha-mcp/commit/9930a8fb1c370729708da843e600b2e5b52778c1))
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

### Changed

- Consolidate 5 redundant tools (merge after #806)
  ([#813](https://github.com/homeassistant-ai/ha-mcp/pull/813))
- Credit @teh-hippo, @smenzer, @The-Greg-O; update @cj-elevate
  ([`66b3bb8`](https://github.com/homeassistant-ai/ha-mcp/commit/66b3bb803fe0fcb4ac7172cce0dcf9f8cfb8979d))
- Document OAuth v7.0.0 breaking change (HOMEASSISTANT_URL required)
  ([#829](https://github.com/homeassistant-ai/ha-mcp/pull/829))

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

---
<details>
<summary>Internal Changes</summary>


### Added

- **uat**: Add --mcp-env flag and tokens_first_input metric
  ([#791](https://github.com/homeassistant-ai/ha-mcp/pull/791))

### Changed

- Replace hardcoded path with <repo-root> placeholder
  ([#797](https://github.com/homeassistant-ai/ha-mcp/pull/797))

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
- Add ha_check_update_notes tool for pre-update impact review
  ([#595](https://github.com/homeassistant-ai/ha-mcp/pull/595))
- Include persistent notifications in ha_get_overview
  ([#642](https://github.com/homeassistant-ai/ha-mcp/pull/642))
- Add Nabu Casa and other generic remote access via webhook proxy
  ([#554](https://github.com/homeassistant-ai/ha-mcp/pull/554))
- Serve bundled HA skills as MCP resources
  ([#679](https://github.com/homeassistant-ai/ha-mcp/pull/679))

### Changed

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
- **uat**: Add ha_checks post-run verification and openai agent improvements
  ([#713](https://github.com/homeassistant-ai/ha-mcp/pull/713))

### Changed

- Always create PRs as draft, mark ready only on user request
  ([#723](https://github.com/homeassistant-ai/ha-mcp/pull/723))

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
- Consolidate redundant dashboard tools (3 tools removed)
  ([#660](https://github.com/homeassistant-ai/ha-mcp/pull/660))

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

---
<details>
<summary>Internal Changes</summary>


### Changed

- Trim AGENTS.md to stay under 40k char limit
  ([#638](https://github.com/homeassistant-ai/ha-mcp/pull/638))

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

- Add ha_get_states tool for bulk entity state retrieval
  ([#588](https://github.com/homeassistant-ai/ha-mcp/pull/588))
- Add offset pagination to ha_search_entities and ha_hacs_search (#605)
  ([#619](https://github.com/homeassistant-ai/ha-mcp/pull/619))
- Add wait parameter to config and service call tools (#381)
  ([#564](https://github.com/homeassistant-ai/ha-mcp/pull/564))

### Changed

- Add @maxperron as contributor for beta testing
  ([`0220708`](https://github.com/homeassistant-ai/ha-mcp/commit/0220708325aeca55c78349cb118423f9bad802ef))
- Update contributors - promote sergeykad and kingpanther13 to maintainers, add airlabno and ryphez
  ([`44f42b9`](https://github.com/homeassistant-ai/ha-mcp/commit/44f42b92de72b5a9e59279c19c8664c0a02b3f2a))
- Add Codex Desktop UI MCP quick setup
  ([#615](https://github.com/homeassistant-ai/ha-mcp/pull/615))
- Improve ruff linter config and fix violations
  ([#624](https://github.com/homeassistant-ai/ha-mcp/pull/624))
- **__main__**: Fix security issues, bugs, and reduce duplication
  ([#609](https://github.com/homeassistant-ai/ha-mcp/pull/609))

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
- Allow editing default dashboard without hyphen in url_path (#591)
  ([#592](https://github.com/homeassistant-ai/ha-mcp/pull/592))

---
<details>
<summary>Internal Changes</summary>


### Added

- Add user acceptance stories for BAT framework
  ([#583](https://github.com/homeassistant-ai/ha-mcp/pull/583))

### Changed

- Classify BAT metrics as primary vs secondary
  ([#639](https://github.com/homeassistant-ai/ha-mcp/pull/639))
- Update safety annotations with correct MCP spec definitions
  ([`59787a2`](https://github.com/homeassistant-ai/ha-mcp/commit/59787a261a60d41dc9e314dd3a851bb4a55d0f14))

### Fixed

- Detect correct PR number when multiple PR refs exist in commit message
  ([#613](https://github.com/homeassistant-ai/ha-mcp/pull/613))
- **tests**: Poll for entity registration in deep search E2E tests
  ([#589](https://github.com/homeassistant-ai/ha-mcp/pull/589))

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
- Add domain filter and options support to ha_get_integration
  ([#542](https://github.com/homeassistant-ai/ha-mcp/pull/542))

### Fixed

- Handle service call timeouts gracefully and add missing @log_tool usage (fixes #550)
  ([#555](https://github.com/homeassistant-ai/ha-mcp/pull/555))
- Optimize Dockerfiles with multi-stage builds
  ([#546](https://github.com/homeassistant-ai/ha-mcp/pull/546))

---
<details>
<summary>Internal Changes</summary>


### Added

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

### Changed

- Rename pr-checker to my-pr-checker for clarity
  ([`a02533c`](https://github.com/homeassistant-ai/ha-mcp/commit/a02533c16a1b7ea8f4f3f0f51cd949f0b1bc01a3))
- Rename UAT to BAT and add progressive disclosure output
  ([`8a6d43e`](https://github.com/homeassistant-ai/ha-mcp/commit/8a6d43e9cd2e20a3d7ca6fbd1be5b986901bd8cf))
- Add comment formatting guidelines to contrib-pr-review
  ([`c014e8a`](https://github.com/homeassistant-ai/ha-mcp/commit/c014e8a08be26421d55e00299648b68f7689d1fb))
- Add contrib-pr-review skill to AGENTS.md
  ([`4aa29c3`](https://github.com/homeassistant-ai/ha-mcp/commit/4aa29c3662942c005336288613a207177091b2c7))
- Add warning to review PRs sequentially, not in parallel
  ([`d69c576`](https://github.com/homeassistant-ai/ha-mcp/commit/d69c576c09b2214a6c5fbf6112bfccfb3d7bd4ae))
- Clarify worktree workflow and symlink convention in AGENTS.md
  ([`9946be5`](https://github.com/homeassistant-ai/ha-mcp/commit/9946be57ee69a267054a7ac31ffb6b408cc3a99b))
- Restructure worktree workflow and documentation
  ([#547](https://github.com/homeassistant-ai/ha-mcp/pull/547))
- Run agents sequentially instead of in parallel
  ([`b3032f4`](https://github.com/homeassistant-ai/ha-mcp/commit/b3032f4fb745516e184ee2278cd900b440afd964))

### Fixed

- Address review comments on UAT runner
  ([`6a2bf04`](https://github.com/homeassistant-ai/ha-mcp/commit/6a2bf0430261e6a07b0738e3a5e98532bccfb636))

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

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Fix YAML frontmatter parsing in agent files
  ([#519](https://github.com/homeassistant-ai/ha-mcp/pull/519))

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
- **entity**: Add ha_update_entity tool for entity registry updates
  ([#469](https://github.com/homeassistant-ai/ha-mcp/pull/469))
- Improve ha_report_issue with title, duplicate check, and markdown formatting
  ([#484](https://github.com/homeassistant-ai/ha-mcp/pull/484))
- Add ha-mcp-dev executable with automatic DEBUG logging
  ([`79a1456`](https://github.com/homeassistant-ai/ha-mcp/commit/79a145680eb24d093bbc0293a7129b814e832c43))

### Changed

- Update contributors section with recent contributions
  ([#492](https://github.com/homeassistant-ai/ha-mcp/pull/492))
- Move OAuth to separate guide, position as beta alternative
  ([#487](https://github.com/homeassistant-ai/ha-mcp/pull/487))
- Add comprehensive dev channel documentation
  ([#476](https://github.com/homeassistant-ai/ha-mcp/pull/476))
- Add uvx cache troubleshooting to FAQ
  ([`f21c431`](https://github.com/homeassistant-ai/ha-mcp/commit/f21c4310235130f77c54bb48e341adcae69ed935))
- Standardize MCP tool error handling and fix test compatibility
  ([#494](https://github.com/homeassistant-ai/ha-mcp/pull/494))

### Fixed

- Update ha_report_issue URLs and improve workflow PR extraction
  ([#505](https://github.com/homeassistant-ai/ha-mcp/pull/505))
- Validate label IDs in ha_manage_entity_labels to prevent silent failures
  ([#486](https://github.com/homeassistant-ai/ha-mcp/pull/486))
- Pin httpx to <1.0 to prevent incompatible prerelease versions
  ([#483](https://github.com/homeassistant-ai/ha-mcp/pull/483))
- Validate operations in ha_bulk_control and report errors (#385)
  ([#473](https://github.com/homeassistant-ai/ha-mcp/pull/473))

---
<details>
<summary>Internal Changes</summary>


### Added

- **workflow**: Clarify Gemini triage is read-only, add diff format for fixes
  ([`3e89988`](https://github.com/homeassistant-ai/ha-mcp/commit/3e899888269135ce36307365ab2d4c9923bcdc31))
- **workflow**: Skip automated triage for julienld's issues
  ([`2b74ee9`](https://github.com/homeassistant-ai/ha-mcp/commit/2b74ee9cff460403c3e8ed1475e1841e112c5a44))
- Add AI-powered issue triage workflow and simplified YAML templates
  ([`69e2fd0`](https://github.com/homeassistant-ai/ha-mcp/commit/69e2fd0de44bdcf037e9e8926f22b9f425233b2c))
- **ci**: Add workflow to notify PRs/issues when merged to dev
  ([#489](https://github.com/homeassistant-ai/ha-mcp/pull/489))
- Publish dev builds to separate ha-mcp-dev package
  ([`f768dd2`](https://github.com/homeassistant-ai/ha-mcp/commit/f768dd21303a4f7b4acf44572ddcdf6328c62926))
- Publish dev builds to PyPI for --pre flag support
  ([`e1e73e1`](https://github.com/homeassistant-ai/ha-mcp/commit/e1e73e1423e118615246ce25f990860a6d8fe587))

### Changed

- Add guidance to resolve review threads with comments
  ([`03ad555`](https://github.com/homeassistant-ai/ha-mcp/commit/03ad5553d9ca8d449f96df2eb77b0b0fd2d79c7a))
- **workflow**: Clarify only gh issue list/view commands available
  ([`2501fde`](https://github.com/homeassistant-ai/ha-mcp/commit/2501fdec4870c7313c18bb762e9dbf17bda8162d))
- Add MCP tool authoring guide to AGENTS.md
  ([#461](https://github.com/homeassistant-ai/ha-mcp/pull/461))
- **agents**: Rebrand level2-triage to issue-analysis workflow
  ([`d2748ab`](https://github.com/homeassistant-ai/ha-mcp/commit/d2748abb8a304e6f8683305f36164b8438b92b00))
- **agents**: Convert triage agent to level2-triaged workflow
  ([`407da6a`](https://github.com/homeassistant-ai/ha-mcp/commit/407da6acfc8ecb65f1f6afedc858006273e2795e))

### Fixed

- **workflow**: Restrict Gemini to read-only gh commands
  ([`5e27889`](https://github.com/homeassistant-ai/ha-mcp/commit/5e27889c41546f2f2e2b1171e6cdf411fa3b64e5))
- **ci**: Support squash merge format in notify workflow
  ([#491](https://github.com/homeassistant-ai/ha-mcp/pull/491))
- Update package name reference in version lookup for ha-mcp-dev
  ([`97df158`](https://github.com/homeassistant-ai/ha-mcp/commit/97df1582cf81a61337b78801ea40c19d56045a03))
- Remove redundant asyncio.sleep calls in E2E helper tests
  ([#470](https://github.com/homeassistant-ai/ha-mcp/pull/470))

### Continuous Integration

- **deps**: Bump renovatebot/github-action from 44.2.4 to 44.2.6
  ([#499](https://github.com/homeassistant-ai/ha-mcp/pull/499))
</details>


## v6.3.1 (2026-01-20)

### Changed

- Add @kingpanther13 and @Raygooo to contributors
  ([`590d0b7`](https://github.com/homeassistant-ai/ha-mcp/commit/590d0b78b3d4b04a260b26bf738e51d97c91b6cf))

### Fixed

- Add socks support to httpx dependency
  ([#450](https://github.com/homeassistant-ai/ha-mcp/pull/450))

---
<details>
<summary>Internal Changes</summary>


### Changed

- **agents**: Add "Leave the Campground Cleaner" principle
  ([`e11d766`](https://github.com/homeassistant-ai/ha-mcp/commit/e11d766b68d63fce34cf5d97a31074526369930f))

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


### Changed

- **deps**: Replace textdistance with stdlib difflib
  ([#432](https://github.com/homeassistant-ai/ha-mcp/pull/432))

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

### Changed

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

### Fixed

- Support blueprint automations in ha_config_set_automation
  ([#364](https://github.com/homeassistant-ai/ha-mcp/pull/364))
- **docs**: Update Docker commands to use ha-mcp-web and remove backslashes
  ([`90822c0`](https://github.com/homeassistant-ai/ha-mcp/commit/90822c087b18cfb68eb2bc23c062a8494356011a))

---
<details>
<summary>Internal Changes</summary>


### Changed

- Document hotfix workflow with stable tag verification and timing
  ([`6bbd782`](https://github.com/homeassistant-ai/ha-mcp/commit/6bbd782ea31fad3e5d4d8aac0a03e26a4ec9a41a))

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
- **docs**: Update AGENTS.md with ha-mcp-web command
  ([`25ddcb7`](https://github.com/homeassistant-ai/ha-mcp/commit/25ddcb7e081bf029022588c82e5aeca260f97179))
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

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Changed

- **ci**: Unify release workflows with reusable build workflow
  ([#348](https://github.com/homeassistant-ai/ha-mcp/pull/348))
- **ci**: Unify release workflows with reusable build workflow
  ([`048a686`](https://github.com/homeassistant-ai/ha-mcp/commit/048a686c904c96cc6cce9bdb52d95dc20da79b29))

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


### Changed

- **ci**: Use draft releases for atomic release creation
  ([`5214097`](https://github.com/homeassistant-ai/ha-mcp/commit/52140979ec71cf6c21b6679a8574585e6ac9e8fb))

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
</details>


## v4.22.0 (2025-12-16)

### Added

- Add all helpers with WebSocket API support
  ([#323](https://github.com/homeassistant-ai/ha-mcp/pull/323))
- Add informational tool for HA configuration access
  ([#322](https://github.com/homeassistant-ai/ha-mcp/pull/322))

### Fixed

- Apply LOG_LEVEL environment variable to Python logging
  ([#321](https://github.com/homeassistant-ai/ha-mcp/pull/321))

---
<details>
<summary>Internal Changes</summary>


### Changed

- Add fact-checking caveat to model knowledge testing
  ([`ea5cb33`](https://github.com/homeassistant-ai/ha-mcp/commit/ea5cb336fa74378136a197a6da2834ca0c4af79a))
- Add no-context sub-agent strategy for testing model knowledge
  ([`9e737a0`](https://github.com/homeassistant-ai/ha-mcp/commit/9e737a0b3a3588e0fa31ed331d58c26e43df27b4))
- Add context engineering & progressive disclosure principles
  ([`40ab2a6`](https://github.com/homeassistant-ai/ha-mcp/commit/40ab2a65f721f65f16f0cb1d48259bb5e4fafeef))

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

- Use --version instead of --help in installer scripts
  ([#310](https://github.com/homeassistant-ai/ha-mcp/pull/310))
- Add --version/-V flag to CLI
  ([#309](https://github.com/homeassistant-ai/ha-mcp/pull/309))
- Update favicon to Home Assistant icon
  ([`02f33db`](https://github.com/homeassistant-ai/ha-mcp/commit/02f33dbade6176a1aeac4e706d1a3544e1acb720))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Add --version/-V flag to CLI
  ([#312](https://github.com/homeassistant-ai/ha-mcp/pull/312))

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

### Changed

- Implement parallel operations for improved performance (#258)
  ([#269](https://github.com/homeassistant-ai/ha-mcp/pull/269))

### Fixed

- Use system CA certificates for SSL verification
  ([#294](https://github.com/homeassistant-ai/ha-mcp/pull/294))
- Preserve voice assistant exposure settings when renaming entities
  ([#271](https://github.com/homeassistant-ai/ha-mcp/pull/271))

---
<details>
<summary>Internal Changes</summary>


### Added

- Weekly stable releases with hotfix support
  ([#292](https://github.com/homeassistant-ai/ha-mcp/pull/292))

### Changed

- Update AGENTS.md with parallel triage workflow
  ([`5239b29`](https://github.com/homeassistant-ai/ha-mcp/commit/5239b295931a2dcc10b841c5d8392c4fa14fe50b))

### Fixed

- Correct cleanup logic to parse tag from gh release list
  ([`e3abb76`](https://github.com/homeassistant-ai/ha-mcp/commit/e3abb7615bfe0863038f0eddb01daa25e4e0e067))

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

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Filter artifact download to avoid Docker buildx cache
  ([`1757e53`](https://github.com/homeassistant-ai/ha-mcp/commit/1757e537a308d43c02df2cbd12f37a6919d40c1a))
</details>


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

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Added

- Add lab setup script with auto-updates and weekly reset
  ([#263](https://github.com/homeassistant-ai/ha-mcp/pull/263))

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

### Fixed

- Add missing py.typed marker file for type hint distribution
  ([#251](https://github.com/homeassistant-ai/ha-mcp/pull/251))

---
<details>
<summary>Internal Changes</summary>


### Changed

- Clarify bug description prompt in template
  ([`96b9bc7`](https://github.com/homeassistant-ai/ha-mcp/commit/96b9bc7cc379f568f9b90ea2b46e708aabd276ab))
- Update bug report template to emphasize ha_bug_report tool
  ([`2e72f16`](https://github.com/homeassistant-ai/ha-mcp/commit/2e72f166d5d9b1dcd6693c2258093743585e2b6b))

### Chores

- Add fastmcp to gitignore
  ([`7db3a66`](https://github.com/homeassistant-ai/ha-mcp/commit/7db3a668235d67213338b6bccb49fdf9116daa2a))
</details>


## v4.11.9 (2025-12-03)

### Fixed

- Improve bug report tool with better diagnostics
  ([#256](https://github.com/homeassistant-ai/ha-mcp/pull/256))


## v4.11.8 (2025-12-03)

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Disable VCS release via GitHub Action input
  ([#257](https://github.com/homeassistant-ai/ha-mcp/pull/257))
</details>


## v4.11.7 (2025-12-03)

### Fixed

- Correct semantic-release v10 config and add release fallback
  ([#255](https://github.com/homeassistant-ai/ha-mcp/pull/255))


## v4.11.6 (2025-12-03)

### Fixed

- Create GitHub release from build-binary workflow
  ([#254](https://github.com/homeassistant-ai/ha-mcp/pull/254))


## v4.11.5 (2025-12-03)

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Use gh release upload to avoid target_commitish conflict
  ([#252](https://github.com/homeassistant-ai/ha-mcp/pull/252))
</details>


## v4.11.4 (2025-12-03)

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Trigger binary builds after SemVer Release via workflow_run
  ([#249](https://github.com/homeassistant-ai/ha-mcp/pull/249))
</details>


## v4.11.3 (2025-12-03)

### Changed

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

### Changed

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

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Added

- Add HA_TEST_PORT env var for custom test container port
  ([`4743ee8`](https://github.com/homeassistant-ai/ha-mcp/commit/4743ee82491f8df82308f80d03565bd6de6909b5))
</details>


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

No user-facing changes.


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
- Reorganize distribution files and add smoke test
  ([`f720e8a`](https://github.com/homeassistant-ai/ha-mcp/commit/f720e8a21d0172d12502a780484309a8e12e16c4))
- Polish mcpb manifest for submission
  ([`9bd2531`](https://github.com/homeassistant-ai/ha-mcp/commit/9bd25315cd0a71ad9954eacdaaea6c4fef985b8d))

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
- Add required destructiveHint to all modifying tools
  ([`8881fcf`](https://github.com/homeassistant-ai/ha-mcp/commit/8881fcf4e894470a618019399853e29c40d886e8))
- Address security scanner warnings and fix privacy policy
  ([`1cba8b6`](https://github.com/homeassistant-ai/ha-mcp/commit/1cba8b6e2f7eebfdaf679a90bd580d0c554736e3))
- Handle Windows encoding in smoke test
  ([`c0b1cca`](https://github.com/homeassistant-ai/ha-mcp/commit/c0b1ccaeb6e1e025b95c59a3546c4cbad5533163))

---
<details>
<summary>Internal Changes</summary>


### Added

- Use light icon (transparent) as main, add 32x32 size
  ([`9ce27c5`](https://github.com/homeassistant-ai/ha-mcp/commit/9ce27c5aa89316099abc4c1bb5de151bc525ac8f))
- **mcpb**: Add tool annotations and update icon
  ([`2d3ea51`](https://github.com/homeassistant-ai/ha-mcp/commit/2d3ea514f71f32c42c2b864324716bd0339859c2))
- Auto-generate mcpb manifest with discovered tools
  ([`4af79a0`](https://github.com/homeassistant-ai/ha-mcp/commit/4af79a01808c6e946b0f196f9c97688c3f6add41))
- Add CD workflow with mcpb packaging and GitHub releases
  ([`d3f8e86`](https://github.com/homeassistant-ai/ha-mcp/commit/d3f8e86a0fb6bf745c17abdea866317228c9ba86))
- Add PyInstaller standalone binary builds
  ([`05630dc`](https://github.com/homeassistant-ai/ha-mcp/commit/05630dca6bae8143ca93d1ab4bfee7e52cf16c15))

### Changed

- Consolidate macOS and Windows into single mcpb bundle
  ([`cde7e36`](https://github.com/homeassistant-ai/ha-mcp/commit/cde7e360b199898ed6c58e37cee43071b78d6570))

### Fixed

- Icon bundle and manifest + add annotation tests
  ([`6d800ef`](https://github.com/homeassistant-ai/ha-mcp/commit/6d800efb275fd1f21a07effa0a2cae871b31dd0e))
- Include all icons in mcpb bundle (dark variants + SVG)
  ([`c22f656`](https://github.com/homeassistant-ai/ha-mcp/commit/c22f65666bd47b097f998a5d235f2b5db346c4d6))
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

- Auto-discover tool modules to prevent merge conflicts
  ([#183](https://github.com/homeassistant-ai/ha-mcp/pull/183))
- Update README and addon docs for new v4.x tools
  ([#178](https://github.com/homeassistant-ai/ha-mcp/pull/178))


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

### Changed

- Update README with all 63 tools
  ([#168](https://github.com/homeassistant-ai/ha-mcp/pull/168))

---
<details>
<summary>Internal Changes</summary>


### Added

- **tests**: Pin Home Assistant container version with Renovate tracking
  ([#167](https://github.com/homeassistant-ai/ha-mcp/pull/167))
</details>


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

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Improve test isolation in test_deep_search_no_results
  ([#80](https://github.com/homeassistant-ai/ha-mcp/pull/80))
</details>


## v3.5.0 (2025-11-17)

### Added

- Add dashboard management tools for Lovelace UI
  ([#75](https://github.com/homeassistant-ai/ha-mcp/pull/75))

---
<details>
<summary>Internal Changes</summary>


### Changed

- Remove Code Refactoring Patterns section from AGENTS.md
  ([`f4612c9`](https://github.com/homeassistant-ai/ha-mcp/commit/f4612c9477f67b50d76b091e740383d816a1981f))
- Update AGENTS.md to reflect registry refactoring architecture
  ([`97111a5`](https://github.com/homeassistant-ai/ha-mcp/commit/97111a59c00537abf38c13fe86e2d38905d04d7a))

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

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Validate server manifest via script
  ([#63](https://github.com/homeassistant-ai/ha-mcp/pull/63))
</details>


## v3.4.1 (2025-11-09)

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Correct release workflow indentation
  ([#62](https://github.com/homeassistant-ai/ha-mcp/pull/62))

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

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Repair codex autofix workflow conditions
  ([#58](https://github.com/homeassistant-ai/ha-mcp/pull/58))
</details>


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

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Added

- Add pypi publish
  ([`bd6d358`](https://github.com/homeassistant-ai/ha-mcp/commit/bd6d358b46212f0102292b56751d9f3f037e673c))

### Changed

- Clarify agent guidance on e2e requirements
  ([#53](https://github.com/homeassistant-ai/ha-mcp/pull/53))

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

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Try multiple codex models per step
  ([#42](https://github.com/homeassistant-ai/ha-mcp/pull/42))
</details>


## v3.2.2 (2025-10-24)

No user-facing changes.

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

### Changed

- Remove redundant static docs
  ([#26](https://github.com/homeassistant-ai/ha-mcp/pull/26))


## v3.1.4 (2025-10-20)

### Changed

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

- Split registry.py into focused modules (2106 → 76 lines)
  ([#21](https://github.com/homeassistant-ai/ha-mcp/pull/21))
- Add ha_deep_search tool to documentation
  ([#20](https://github.com/homeassistant-ai/ha-mcp/pull/20))


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

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Make addon build wait for semantic-release to complete
  ([`2ae666a`](https://github.com/homeassistant-ai/ha-mcp/commit/2ae666a4468370c39c1b7bf25b6dfb34db7ee897))
</details>


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

---
<details>
<summary>Internal Changes</summary>


### Changed

- Remove non-reusable package rename documentation from AGENTS.md
  ([`d0602ba`](https://github.com/homeassistant-ai/ha-mcp/commit/d0602ba800195063ee1f8f9ab85a9983bc154920))
- Add lessons learned from ha_config_* refactoring to AGENTS.md
  ([`25a8f66`](https://github.com/homeassistant-ai/ha-mcp/commit/25a8f66dd3a5c1861fc7f756ba603ac4cb8b67c1))
</details>


## v1.0.3 (2025-10-01)

### Changed

- Split ha_manage_* into ha_config_{get,set,remove}_* tools
  ([#6](https://github.com/homeassistant-ai/ha-mcp/pull/6))
- Fix typos and formatting in README
  ([`ebfa004`](https://github.com/homeassistant-ai/ha-mcp/commit/ebfa004f76143c3c53735bc1834ee17539980e4d))


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

No user-facing changes.

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Remove Docker ecosystem from dependabot config
  ([`b393282`](https://github.com/homeassistant-ai/ha-mcp/commit/b393282f7e5774ea706f364b27ff522e4af800a8))
</details>


## v1.0.0 (2025-09-18)

No user-facing changes.
