# CHANGELOG

<!-- version list -->


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

### Fixed

- Stop the auto-updated in-process server crashing under an older custom component
  ([#1792](https://github.com/homeassistant-ai/ha-mcp/pull/1792))

### Refactoring

- **c901**: Fix 8 files below C901 threshold, remove from grandfather list
  ([#1793](https://github.com/homeassistant-ai/ha-mcp/pull/1793))
- **c901**: Tools_config_dashboards.py below C901 threshold, enable C90 lint
  ([#1790](https://github.com/homeassistant-ai/ha-mcp/pull/1790))

---
<details>
<summary>Internal Changes</summary>


### Fixed

- Isolate 3 beta-gate tests from real ~/.ha-mcp config
  ([#1791](https://github.com/homeassistant-ai/ha-mcp/pull/1791))
- Split mypy-webhook-proxy lefthook job to avoid module collision
  ([#1789](https://github.com/homeassistant-ai/ha-mcp/pull/1789))

### Chores

- **addon**: Publish dev addon version 7.12.0.dev1840 [skip ci]
  ([`d315210`](https://github.com/homeassistant-ai/ha-mcp/commit/d31521044f06212204665155d9b2b0b999199d9b))
- **addon**: Publish dev addon version 7.12.0.dev1837 [skip ci]
  ([`2fd1621`](https://github.com/homeassistant-ai/ha-mcp/commit/2fd1621c01664ef2baf4ce30a4e31a734a567760))
- **addon**: Publish dev addon version 7.12.0.dev1835 [skip ci]
  ([`55ce284`](https://github.com/homeassistant-ai/ha-mcp/commit/55ce284bad809594b4a6472083f512a02b8edc37))
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

- **addon**: Publish dev addon version 7.11.0.dev1828 [skip ci]
  ([`c945d8a`](https://github.com/homeassistant-ai/ha-mcp/commit/c945d8a3b75ccc960eeee32450f8cc7e018262eb))
- **addon**: Publish dev addon version 7.11.0.dev1825 [skip ci]
  ([`ff1dcbf`](https://github.com/homeassistant-ai/ha-mcp/commit/ff1dcbf3c8b95c605c18d504b07fe676d5603786))
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

- **addon**: Publish dev addon version 7.10.0.dev481 [skip ci]
  ([`e2c28a4`](https://github.com/homeassistant-ai/ha-mcp/commit/e2c28a41b77db12f343d2b208972142e3f9007c6))
- **addon**: Publish dev addon version 7.10.0.dev480 [skip ci]
  ([`d7f9c00`](https://github.com/homeassistant-ai/ha-mcp/commit/d7f9c001cb6aa0e6ca1fc53fb0f4b81131b82877))
- **deps**: Update src/ha_mcp/resources/skills-vendor digest to 5c023ed
  ([#1771](https://github.com/homeassistant-ai/ha-mcp/pull/1771))
- **addon**: Publish dev addon version 7.10.0.dev479 [skip ci]
  ([`559d4ef`](https://github.com/homeassistant-ai/ha-mcp/commit/559d4efa342cfb760a830e49fe1153fcee893407))
- **addon**: Publish dev addon version 7.10.0.dev478 [skip ci]
  ([`68d5cf1`](https://github.com/homeassistant-ai/ha-mcp/commit/68d5cf18e8735b5b201d98421ff2eaea45ea3a92))
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

### Refactoring

- **settings-ui**: HA-faithful redesign, auto-save, dedicated package
  ([#1695](https://github.com/homeassistant-ai/ha-mcp/pull/1695))
- **c901**: Tools_entities.py below C901 threshold
  ([#1692](https://github.com/homeassistant-ai/ha-mcp/pull/1692))
- **c901**: Tools_registry.py below C901 threshold
  ([#1691](https://github.com/homeassistant-ai/ha-mcp/pull/1691))
- **c901**: Tools_search.py below C901 threshold
  ([#1665](https://github.com/homeassistant-ai/ha-mcp/pull/1665))

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

- **addon**: Publish dev addon version 7.9.0.dev476 [skip ci]
  ([`f54b32f`](https://github.com/homeassistant-ai/ha-mcp/commit/f54b32fc895aa1cd5f07342579814a41f1b4aa83))
- **addon**: Publish dev addon version 7.9.0.dev475 [skip ci]
  ([`e28d94e`](https://github.com/homeassistant-ai/ha-mcp/commit/e28d94e30140d73933a0a2f1b6b7cf17a752a4b5))
- **addon**: Publish dev addon version 7.9.0.dev474 [skip ci]
  ([`8284017`](https://github.com/homeassistant-ai/ha-mcp/commit/8284017ec4952c7f2e12fd970199a0028d23f170))
- **addon**: Publish dev addon version 7.9.0.dev473 [skip ci]
  ([`e9fe11c`](https://github.com/homeassistant-ai/ha-mcp/commit/e9fe11cfe1e0cdf4014c479900456dcc236d3688))
- **addon**: Publish dev addon version 7.9.0.dev472 [skip ci]
  ([`0d84953`](https://github.com/homeassistant-ai/ha-mcp/commit/0d84953dca704428100d473acf76096540d663b0))
- **addon**: Promote webhook-proxy dev -> stable 2.0.1
  ([#1744](https://github.com/homeassistant-ai/ha-mcp/pull/1744))
- **addon**: Publish dev addon version 7.9.0.dev471 [skip ci]
  ([`05ad454`](https://github.com/homeassistant-ai/ha-mcp/commit/05ad454c87509d42e9ff999972d12a9540cb83b4))
- **addon**: Publish dev addon version 7.9.0.dev470 [skip ci]
  ([`d009274`](https://github.com/homeassistant-ai/ha-mcp/commit/d0092743f68f9992cf475189d4a2ced552ebba6e))
- **addon**: Publish dev addon version 7.9.0.dev469 [skip ci]
  ([`4259058`](https://github.com/homeassistant-ai/ha-mcp/commit/425905884ad0570b57b250496930f9b374af43f0))
- Sync tool docs after merge [skip ci]
  ([`9c07f0f`](https://github.com/homeassistant-ai/ha-mcp/commit/9c07f0f6c18b86673f3f8c7cec24190ffd0b9387))
- **addon**: Promote webhook-proxy dev -> stable 2.0.0
  ([#1739](https://github.com/homeassistant-ai/ha-mcp/pull/1739))
- **deps**: Update src/ha_mcp/resources/skills-vendor digest to 441d077
  ([#1732](https://github.com/homeassistant-ai/ha-mcp/pull/1732))
- **addon**: Publish dev addon version 7.9.0.dev467 [skip ci]
  ([`b3ee380`](https://github.com/homeassistant-ai/ha-mcp/commit/b3ee380ce4ddf4ad0b58b4a6cbd9dbdea87fb805))
- Sync tool docs after merge [skip ci]
  ([`8b1c313`](https://github.com/homeassistant-ai/ha-mcp/commit/8b1c3136d9b6fc41f8f61c556e2d15a28c92cbe0))
- **addon**: Publish dev addon version 7.9.0.dev466 [skip ci]
  ([`ee8f790`](https://github.com/homeassistant-ai/ha-mcp/commit/ee8f790d9a6c42f4c837134533cd22cff207adfc))
- **addon**: Publish dev addon version 7.9.0.dev465 [skip ci]
  ([`3874b95`](https://github.com/homeassistant-ai/ha-mcp/commit/3874b9565f60fbc2d09e2a34a5206b3d798a4197))
- Sync tool docs after merge [skip ci]
  ([`279d7eb`](https://github.com/homeassistant-ai/ha-mcp/commit/279d7ebefd3ed8ac7e8c120a426b581bbcc9df8b))
- **deps**: Update dependency home-assistant/operating-system to v18
  ([#1718](https://github.com/homeassistant-ai/ha-mcp/pull/1718))
- **addon**: Publish dev addon version 7.9.0.dev464 [skip ci]
  ([`c3b28e4`](https://github.com/homeassistant-ai/ha-mcp/commit/c3b28e457158c4f5f2adfa3e6ecc7bc36f1baea6))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.26
  ([#1717](https://github.com/homeassistant-ai/ha-mcp/pull/1717))
- **addon**: Publish dev addon version 7.9.0.dev463 [skip ci]
  ([`312931a`](https://github.com/homeassistant-ai/ha-mcp/commit/312931ac960946fc8e03dc7d804c2a90b7f97a6f))
- **addon**: Publish dev addon version 7.9.0.dev462 [skip ci]
  ([`de45f85`](https://github.com/homeassistant-ai/ha-mcp/commit/de45f8577c218e21f08a20a2a3aca278622f3c9b))
- Sync tool docs after merge [skip ci]
  ([`0072154`](https://github.com/homeassistant-ai/ha-mcp/commit/00721543229587ee9fc16f0f4c86fad608e12a69))
- **addon**: Publish dev addon version 7.9.0.dev461 [skip ci]
  ([`ef59d3c`](https://github.com/homeassistant-ai/ha-mcp/commit/ef59d3c6383be6720ae1943e6fe98d043f2316cd))
- **addon**: Publish dev addon version 7.9.0.dev460 [skip ci]
  ([`3504936`](https://github.com/homeassistant-ai/ha-mcp/commit/350493645b4fea4d0f569f53591d4b7cb3804147))
- Sync tool docs after merge [skip ci]
  ([`605ccde`](https://github.com/homeassistant-ai/ha-mcp/commit/605ccde088ba0550c63bc8245043c7787ec6b71b))
- **addon**: Publish dev addon version 7.9.0.dev459 [skip ci]
  ([`27c804b`](https://github.com/homeassistant-ai/ha-mcp/commit/27c804b24ce62cb723d839fc8db033f9d617f12c))
- **addon**: Publish dev addon version 7.9.0.dev458 [skip ci]
  ([`9c5282e`](https://github.com/homeassistant-ai/ha-mcp/commit/9c5282e1c4dae7e454f1a6aad53e8aafbe1fcdc3))
- **addon**: Publish dev addon version 7.9.0.dev457 [skip ci]
  ([`1193579`](https://github.com/homeassistant-ai/ha-mcp/commit/1193579067a65d8ecbbacc9efb80fc10e27e0700))
- **addon**: Publish dev addon version 7.9.0.dev456 [skip ci]
  ([`9a8e274`](https://github.com/homeassistant-ai/ha-mcp/commit/9a8e274bdf635dd1e15d2dfc3b68092a6c666fca))
- **addon**: Publish dev addon version 7.9.0.dev455 [skip ci]
  ([`e74031e`](https://github.com/homeassistant-ai/ha-mcp/commit/e74031e080f335e248cba008015cd2af90a71272))
- **addon**: Publish dev addon version 7.9.0.dev454 [skip ci]
  ([`fe6bd9f`](https://github.com/homeassistant-ai/ha-mcp/commit/fe6bd9f1b4ccf7c130188898b40059aac15ed063))
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

- **addon**: Publish dev addon version 7.8.1.dev452 [skip ci]
  ([`57edd79`](https://github.com/homeassistant-ai/ha-mcp/commit/57edd797ba1bbde53429d121e931ebafd8e53d99))
- **addon**: Publish dev addon version 7.8.1.dev451 [skip ci]
  ([`90e6229`](https://github.com/homeassistant-ai/ha-mcp/commit/90e6229187941681e7d14cbf81b354c662eeff1c))
- **addon**: Publish dev addon version 7.8.1.dev450 [skip ci]
  ([`3e07aab`](https://github.com/homeassistant-ai/ha-mcp/commit/3e07aab9414a385b6ac83c527d28d20c681ee06a))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.6.4
  ([#1686](https://github.com/homeassistant-ai/ha-mcp/pull/1686))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.23
  ([#1685](https://github.com/homeassistant-ai/ha-mcp/pull/1685))
- **addon**: Publish dev addon version 7.8.1.dev449 [skip ci]
  ([`a34eed9`](https://github.com/homeassistant-ai/ha-mcp/commit/a34eed9aa7575e3cecea13dd1adccb3253734930))
- **addon**: Publish dev addon version 7.8.1.dev448 [skip ci]
  ([`bcf025f`](https://github.com/homeassistant-ai/ha-mcp/commit/bcf025fad214a510bce342c2e60c27c130226598))
- **addon**: Publish dev addon version 7.8.1.dev447 [skip ci]
  ([`8657690`](https://github.com/homeassistant-ai/ha-mcp/commit/865769074f3e6e8878a533fccaa83f83aff84f7d))
- Sync tool docs after merge [skip ci]
  ([`86a19eb`](https://github.com/homeassistant-ai/ha-mcp/commit/86a19eb68ef44aa580920938717fffb8dd587076))
- **addon**: Publish dev addon version 7.8.1.dev446 [skip ci]
  ([`5413ed7`](https://github.com/homeassistant-ai/ha-mcp/commit/5413ed705138ead228964a60724c60f4baa1af43))
- Sync tool docs after merge [skip ci]
  ([`252320f`](https://github.com/homeassistant-ai/ha-mcp/commit/252320f1f7b297135fec2fc5faef673e5c15579c))
- **addon**: Publish dev addon version 7.8.1.dev445 [skip ci]
  ([`d0c0e21`](https://github.com/homeassistant-ai/ha-mcp/commit/d0c0e21815a9594c73bca8de1c95aba275f11e55))
- Sync tool docs after merge [skip ci]
  ([`484fa02`](https://github.com/homeassistant-ai/ha-mcp/commit/484fa0216973613cb0eab9de0d054650f89762c8))
- **addon**: Publish dev addon version 7.8.1.dev444 [skip ci]
  ([`b2a8ead`](https://github.com/homeassistant-ai/ha-mcp/commit/b2a8eadd4ef5acb2411aa27cc95d5b7b9fa9c65f))
- Sync pyproject version to the published 7.8.1
  ([#1650](https://github.com/homeassistant-ai/ha-mcp/pull/1650))
- **addon**: Publish hotfix version 7.8.1 [skip ci]
  ([`dbc0d1e`](https://github.com/homeassistant-ai/ha-mcp/commit/dbc0d1e2eca37b01e17567a3d19996f96b364434))
- **addon**: Publish dev addon version 7.8.0.dev443 [skip ci]
  ([`600032c`](https://github.com/homeassistant-ai/ha-mcp/commit/600032c11a2b83c1c0bcd811e3bec486a870cea5))
- **addon**: Publish dev addon version 7.8.0.dev442 [skip ci]
  ([`b211372`](https://github.com/homeassistant-ai/ha-mcp/commit/b211372165c8e365bcfda9d6d485a4333c223d36))
- **addon**: Publish dev addon version 7.8.0.dev441 [skip ci]
  ([`77f8de1`](https://github.com/homeassistant-ai/ha-mcp/commit/77f8de1ac36437f4766114787c624a492e0c4c87))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.21
  ([#1645](https://github.com/homeassistant-ai/ha-mcp/pull/1645))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.6.3
  ([#1646](https://github.com/homeassistant-ai/ha-mcp/pull/1646))
- **addon**: Publish dev addon version 7.8.0.dev440 [skip ci]
  ([`e6bf506`](https://github.com/homeassistant-ai/ha-mcp/commit/e6bf506e4d06d4eaaa8337d391f47b80c472e503))
- Sync tool docs after merge [skip ci]
  ([`c5c8ba3`](https://github.com/homeassistant-ai/ha-mcp/commit/c5c8ba3c2af0d36226588a63316b4e491beb8ed6))
- **addon**: Publish dev addon version 7.8.0.dev439 [skip ci]
  ([`596169c`](https://github.com/homeassistant-ai/ha-mcp/commit/596169c4c9ad379b2cfb29d935d5b05a56388a07))
- Sync tool docs after merge [skip ci]
  ([`c5704f6`](https://github.com/homeassistant-ai/ha-mcp/commit/c5704f6ef6be18401bf4a35267a94c191a5f8db1))
- **addon**: Publish dev addon version 7.8.0.dev438 [skip ci]
  ([`20ad207`](https://github.com/homeassistant-ai/ha-mcp/commit/20ad2077ac401f13268b822d419677d251ca2cdf))
- **addon**: Publish dev addon version 7.8.0.dev437 [skip ci]
  ([`a55fc0d`](https://github.com/homeassistant-ai/ha-mcp/commit/a55fc0dd0f0bfe658f5a080627a442d1e742db00))
- **addon**: Publish dev addon version 7.8.0.dev436 [skip ci]
  ([`03e70fa`](https://github.com/homeassistant-ai/ha-mcp/commit/03e70fabb0d8ee1b3912aee26b7e0c462760aef4))
- **addon**: Publish dev addon version 7.8.0.dev435 [skip ci]
  ([`b3cd88e`](https://github.com/homeassistant-ai/ha-mcp/commit/b3cd88e165c31f0f6ddab0245491eae68625a018))
- Sync tool docs after merge [skip ci]
  ([`25dae72`](https://github.com/homeassistant-ai/ha-mcp/commit/25dae72aabcec787e958934ab576377a5937874d))
- **addon**: Publish dev addon version 7.8.0.dev434 [skip ci]
  ([`d06e0da`](https://github.com/homeassistant-ai/ha-mcp/commit/d06e0da3151747312e07ce08bcda035b454d866a))
- Sync tool docs after merge [skip ci]
  ([`3f18d56`](https://github.com/homeassistant-ai/ha-mcp/commit/3f18d56c8e6a585212b3e66ce01b216c82695756))
- **addon**: Publish dev addon version 7.8.0.dev433 [skip ci]
  ([`6ce2753`](https://github.com/homeassistant-ai/ha-mcp/commit/6ce27531935ab0883f915f32ee0831ef3d72cd46))
- **addon**: Publish dev addon version 7.8.0.dev432 [skip ci]
  ([`53a9999`](https://github.com/homeassistant-ai/ha-mcp/commit/53a9999416636495adc20e641da01c2044c4c0f2))
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
- **haos-e2e**: Apps/local refresh, Supervisor-update wait, cache-miss local build
  ([#1600](https://github.com/homeassistant-ai/ha-mcp/pull/1600))
- Coerce JSON-encoded strings on dict/list tool params
  ([#1582](https://github.com/homeassistant-ai/ha-mcp/pull/1582))
- Rename config-entry-flow machinery out of tools_* namespace
  ([#1584](https://github.com/homeassistant-ai/ha-mcp/pull/1584))
- Report stored option values instead of schema defaults in options-flow reads
  ([#1577](https://github.com/homeassistant-ai/ha-mcp/pull/1577))

---
<details>
<summary>Internal Changes</summary>


### Chores

- **addon**: Publish dev addon version 7.7.0.dev430 [skip ci]
  ([`5d1ba14`](https://github.com/homeassistant-ai/ha-mcp/commit/5d1ba14146635d109b6a1995e2dde3f80c5d9625))
- **addon**: Publish dev addon version 7.7.0.dev429 [skip ci]
  ([`140ab8a`](https://github.com/homeassistant-ai/ha-mcp/commit/140ab8a75cbf008b0b51fcd38a298544cda83421))
- **addon**: Publish dev addon version 7.7.0.dev428 [skip ci]
  ([`52eeeba`](https://github.com/homeassistant-ai/ha-mcp/commit/52eeeba7112e9fd73be9aad3ef14534fe11564b4))
- **addon**: Publish dev addon version 7.7.0.dev427 [skip ci]
  ([`8dda905`](https://github.com/homeassistant-ai/ha-mcp/commit/8dda905f6e2336f1fa581f1230a141b2f87506e6))
- **addon**: Publish dev addon version 7.7.0.dev426 [skip ci]
  ([`be4863d`](https://github.com/homeassistant-ai/ha-mcp/commit/be4863d8a14de026911a8d30dbd69c61a7cd4ae4))
- Sync tool docs after merge [skip ci]
  ([`3193ce4`](https://github.com/homeassistant-ai/ha-mcp/commit/3193ce404bedfb6c00352fc4027e8ce4baca9222))
- **addon**: Publish dev addon version 7.7.0.dev425 [skip ci]
  ([`9130d0e`](https://github.com/homeassistant-ai/ha-mcp/commit/9130d0edcc83452015931f47f4868870b530c660))
- Sync tool docs after merge [skip ci]
  ([`f21970d`](https://github.com/homeassistant-ai/ha-mcp/commit/f21970de1e7b74d2baa39d72dee848c2feaf348d))
- **addon**: Publish dev addon version 7.7.0.dev424 [skip ci]
  ([`62baf73`](https://github.com/homeassistant-ai/ha-mcp/commit/62baf7380cf134273cdaa80b131f8fa4c168bf22))
- Sync tool docs after merge [skip ci]
  ([`ae02fe9`](https://github.com/homeassistant-ai/ha-mcp/commit/ae02fe9561a45201aa63c7c3250e59c81ec3012c))
- **addon**: Publish dev addon version 7.7.0.dev423 [skip ci]
  ([`118e4e7`](https://github.com/homeassistant-ai/ha-mcp/commit/118e4e7aded032335b45eda5213cd61436deaf8f))
- **addon**: Publish dev addon version 7.7.0.dev422 [skip ci]
  ([`1db9ef8`](https://github.com/homeassistant-ai/ha-mcp/commit/1db9ef88fd4eb0ef1604fafc15422108f9b3796d))
- **addon**: Publish dev addon version 7.7.0.dev421 [skip ci]
  ([`7c8c7ae`](https://github.com/homeassistant-ai/ha-mcp/commit/7c8c7ae72f630574b424b312181edd26973af5cd))
- **addon**: Publish dev addon version 7.7.0.dev420 [skip ci]
  ([`f248ece`](https://github.com/homeassistant-ai/ha-mcp/commit/f248ece51aecf018759ad2a087dde19a27a2ca37))
- Sync tool docs after merge [skip ci]
  ([`1e3c18c`](https://github.com/homeassistant-ai/ha-mcp/commit/1e3c18c6e92cb502ef7e4212b8484175b9a9567c))
- **addon**: Publish dev addon version 7.7.0.dev419 [skip ci]
  ([`f05d155`](https://github.com/homeassistant-ai/ha-mcp/commit/f05d1554170734c718c5483f622e5e1ea73a7419))
- Sync tool docs after merge [skip ci]
  ([`9967d50`](https://github.com/homeassistant-ai/ha-mcp/commit/9967d501e6739630bfda8b3a55ed559ac6fe95a1))
- **addon**: Publish dev addon version 7.7.0.dev417 [skip ci]
  ([`c3bf16a`](https://github.com/homeassistant-ai/ha-mcp/commit/c3bf16af9ca4231ea58046f9363d128afebe923c))
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
- **security**: Note that security-advisory disposition is API-blind (UI-only)
  ([#1561](https://github.com/homeassistant-ai/ha-mcp/pull/1561))
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

### Performance Improvements

- Fast-fail HACS not-found lookups + batch-verify deep-search E2E fixtures (#1515)
  ([#1552](https://github.com/homeassistant-ai/ha-mcp/pull/1552))

### Refactoring

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

---
<details>
<summary>Internal Changes</summary>


### Changed

- Trim AGENTS.md below 40k + improve subdirectory CLAUDE.md files
  ([#1499](https://github.com/homeassistant-ai/ha-mcp/pull/1499))

### Chores

- **addon**: Publish dev addon version 7.6.0.dev415 [skip ci]
  ([`2c0148b`](https://github.com/homeassistant-ai/ha-mcp/commit/2c0148ba512a02b5299edbb4086c38410777a295))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.6.1
  ([#1571](https://github.com/homeassistant-ai/ha-mcp/pull/1571))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.19
  ([#1570](https://github.com/homeassistant-ai/ha-mcp/pull/1570))
- **addon**: Publish dev addon version 7.6.0.dev414 [skip ci]
  ([`0bf9e39`](https://github.com/homeassistant-ai/ha-mcp/commit/0bf9e39947e8bf87d18bae23877d6b0d955dd35f))
- Sync tool docs after merge [skip ci]
  ([`8f5e037`](https://github.com/homeassistant-ai/ha-mcp/commit/8f5e037b89296fd483888d158d8f72f4b1b7dfa8))
- **addon**: Publish dev addon version 7.6.0.dev413 [skip ci]
  ([`6729327`](https://github.com/homeassistant-ai/ha-mcp/commit/6729327de8c0747f5cc7bb639859f95bbe1f0cfc))
- Sync tool docs after merge [skip ci]
  ([`4341e95`](https://github.com/homeassistant-ai/ha-mcp/commit/4341e9574b3270c67bc016676f854f585fec816f))
- **addon**: Publish dev addon version 7.6.0.dev412 [skip ci]
  ([`148f506`](https://github.com/homeassistant-ai/ha-mcp/commit/148f506a98c8f89292616a3a05dd0c9156516439))
- **addon**: Publish dev addon version 7.6.0.dev411 [skip ci]
  ([`287fe55`](https://github.com/homeassistant-ai/ha-mcp/commit/287fe5503917e551f2dcb363cd28250237ed880c))
- Sync tool docs after merge [skip ci]
  ([`b642b8e`](https://github.com/homeassistant-ai/ha-mcp/commit/b642b8eba67d8015d8fe5a1c1f88c9dd72ffc45f))
- **addon**: Publish dev addon version 7.6.0.dev410 [skip ci]
  ([`29e34dd`](https://github.com/homeassistant-ai/ha-mcp/commit/29e34dd6eb517399de3c0a6a886e45ecf4749923))
- **addon**: Publish dev addon version 7.6.0.dev409 [skip ci]
  ([`dec16a1`](https://github.com/homeassistant-ai/ha-mcp/commit/dec16a12d2ac3341908b130e9ea79f950d02466b))
- Sync tool docs after merge [skip ci]
  ([`dd72535`](https://github.com/homeassistant-ai/ha-mcp/commit/dd725357f5d48a00959aaa7eceb4bad67a7ba202))
- **addon**: Publish dev addon version 7.6.0.dev408 [skip ci]
  ([`c3c89f1`](https://github.com/homeassistant-ai/ha-mcp/commit/c3c89f1a758b6086b59ff21f9044597da103fa25))
- **addon**: Publish dev addon version 7.6.0.dev407 [skip ci]
  ([`94195ee`](https://github.com/homeassistant-ai/ha-mcp/commit/94195ee4c8dbd0d8eec13d7c3d7ee392e5b7240f))
- **addon**: Publish dev addon version 7.6.0.dev406 [skip ci]
  ([`f439424`](https://github.com/homeassistant-ai/ha-mcp/commit/f4394240dd1170f9d978e3c4a5011bab12f17f54))
- Sync tool docs after merge [skip ci]
  ([`7464277`](https://github.com/homeassistant-ai/ha-mcp/commit/746427745d3debccea57c844278d1ba834d7ebfb))
- **addon**: Publish dev addon version 7.6.0.dev405 [skip ci]
  ([`146eb07`](https://github.com/homeassistant-ai/ha-mcp/commit/146eb073a6f260ed0b0653cdb71346e9d29deae7))
- **addon**: Publish dev addon version 7.6.0.dev404 [skip ci]
  ([`6a707bb`](https://github.com/homeassistant-ai/ha-mcp/commit/6a707bba576b987f8c3395adca3d59f7dd8a1050))
- Sync tool docs after merge [skip ci]
  ([`85f3935`](https://github.com/homeassistant-ai/ha-mcp/commit/85f3935f80d9677f791b3098b201ec9c27e643be))
- **addon**: Publish dev addon version 7.6.0.dev403 [skip ci]
  ([`be1fa1d`](https://github.com/homeassistant-ai/ha-mcp/commit/be1fa1dd2693170cbe3c037e7a88cf02f1847dcf))
- Sync tool docs after merge [skip ci]
  ([`d9adbd2`](https://github.com/homeassistant-ai/ha-mcp/commit/d9adbd2c27750c9bc8faa55470d24564c48b3dbf))
- **addon**: Publish dev addon version 7.6.0.dev402 [skip ci]
  ([`41ad7ca`](https://github.com/homeassistant-ai/ha-mcp/commit/41ad7caf1e0d982dd9e7bda3420ba3ca577cf765))
- **addon**: Publish dev addon version 7.6.0.dev401 [skip ci]
  ([`8205034`](https://github.com/homeassistant-ai/ha-mcp/commit/820503485386f13dc66a0da3d304739e1f03dafd))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.18
  ([#1523](https://github.com/homeassistant-ai/ha-mcp/pull/1523))
- **addon**: Publish dev addon version 7.6.0.dev400 [skip ci]
  ([`532cdc4`](https://github.com/homeassistant-ai/ha-mcp/commit/532cdc440a8db053c524579f3e6b48b099c13c66))
- Sync tool docs after merge [skip ci]
  ([`e2c08ff`](https://github.com/homeassistant-ai/ha-mcp/commit/e2c08ff797ffabf7b39bfc4b22b498b1c02ce1fa))
- **addon**: Publish dev addon version 7.6.0.dev399 [skip ci]
  ([`9501348`](https://github.com/homeassistant-ai/ha-mcp/commit/95013488ea99761535ea9532285839bcd0ec8e3b))
- Sync tool docs after merge [skip ci]
  ([`e70a050`](https://github.com/homeassistant-ai/ha-mcp/commit/e70a050efcb6edeb65d51dabd5307a192a22e66c))
- **addon**: Publish dev addon version 7.6.0.dev398 [skip ci]
  ([`0b61424`](https://github.com/homeassistant-ai/ha-mcp/commit/0b61424ed823453dec27bb173d049f901eddc2d3))
- Sync tool docs after merge [skip ci]
  ([`6df7987`](https://github.com/homeassistant-ai/ha-mcp/commit/6df79877669686a9fee3ec04979dff3960bc5e3f))
- **addon**: Publish dev addon version 7.6.0.dev397 [skip ci]
  ([`c34915f`](https://github.com/homeassistant-ai/ha-mcp/commit/c34915f40cd82f83f0eb4af5c3a5655843608770))
- Sync tool docs after merge [skip ci]
  ([`6f88e7e`](https://github.com/homeassistant-ai/ha-mcp/commit/6f88e7e0c8dcb7fee27792e0f87f6acd8aa892c4))
- **addon**: Publish dev addon version 7.6.0.dev396 [skip ci]
  ([`1490e19`](https://github.com/homeassistant-ai/ha-mcp/commit/1490e190382454b17fc14cd362b17612a6af253f))
- Sync tool docs after merge [skip ci]
  ([`ac2c7d0`](https://github.com/homeassistant-ai/ha-mcp/commit/ac2c7d038a90fb44466afa8f047378d7e8daed94))
- **addon**: Publish dev addon version 7.6.0.dev395 [skip ci]
  ([`11e5ee8`](https://github.com/homeassistant-ai/ha-mcp/commit/11e5ee8f07f63f63b8cdf017397263c7602e80f6))
- **addon**: Publish dev addon version 7.6.0.dev394 [skip ci]
  ([`5fc3e0f`](https://github.com/homeassistant-ai/ha-mcp/commit/5fc3e0f572bfd1bb367e65b3f4d78b96cb78fa91))
- Flag untrusted third-party content in HACS and add-on tool responses
  ([#1509](https://github.com/homeassistant-ai/ha-mcp/pull/1509))
- **addon**: Publish dev addon version 7.6.0.dev393 [skip ci]
  ([`9a37c28`](https://github.com/homeassistant-ai/ha-mcp/commit/9a37c28cfa0d1f3f652fe441ce2fe1d72a7360ab))
- **addon**: Publish dev addon version 7.6.0.dev392 [skip ci]
  ([`4f737ac`](https://github.com/homeassistant-ai/ha-mcp/commit/4f737ac7c116e9afa046ab45b1230e2cdb5ae959))
- **addon**: Publish dev addon version 7.6.0.dev391 [skip ci]
  ([`fd5d3f2`](https://github.com/homeassistant-ai/ha-mcp/commit/fd5d3f2d4ce87baa4cfcc85a173bb0cc9d132a35))
- **addon**: Publish dev addon version 7.6.0.dev390 [skip ci]
  ([`28dedf4`](https://github.com/homeassistant-ai/ha-mcp/commit/28dedf43f3cd094b1c40fb50e535166cc8c33388))
- **addon**: Publish dev addon version 7.6.0.dev389 [skip ci]
  ([`9443446`](https://github.com/homeassistant-ai/ha-mcp/commit/9443446fdd607a75d8db5fdad009130b188267d6))
- Sync tool docs after merge [skip ci]
  ([`94fcbeb`](https://github.com/homeassistant-ai/ha-mcp/commit/94fcbeb14bd9cfa3bef274182f95e4562a379971))
- **addon**: Publish dev addon version 7.6.0.dev388 [skip ci]
  ([`ccc1816`](https://github.com/homeassistant-ai/ha-mcp/commit/ccc18162eec520950c58cc8eabb2aac04ac57ab3))
- Drop dead entity_cache attr + ruff-format fuzzy_search.py
  ([#1503](https://github.com/homeassistant-ai/ha-mcp/pull/1503))
- **addon**: Publish dev addon version 7.6.0.dev387 [skip ci]
  ([`057c108`](https://github.com/homeassistant-ai/ha-mcp/commit/057c108b2958959bfee34225899b47b5c989a119))
- **addon**: Publish dev addon version 7.6.0.dev386 [skip ci]
  ([`66f4ac6`](https://github.com/homeassistant-ai/ha-mcp/commit/66f4ac6a37d85412b895962afef1b03707400e56))
- **addon**: Publish dev addon version 7.6.0.dev385 [skip ci]
  ([`f331f11`](https://github.com/homeassistant-ai/ha-mcp/commit/f331f1150f525a18bf5bd2e27007b46a07a3492c))
- **addon**: Publish dev addon version 7.6.0.dev384 [skip ci]
  ([`17c319e`](https://github.com/homeassistant-ai/ha-mcp/commit/17c319e68ba4b4d4e6acd84deb57c588aafcb48b))
- Sync tool docs after merge [skip ci]
  ([`f36cbb7`](https://github.com/homeassistant-ai/ha-mcp/commit/f36cbb7e620839b16808b37b61dbc7602557b409))
- **addon**: Publish dev addon version 7.6.0.dev383 [skip ci]
  ([`782ba3b`](https://github.com/homeassistant-ai/ha-mcp/commit/782ba3b2bbcfe50564c5524d6a2c018a4c9c9ba6))
- **addon**: Publish dev addon version 7.6.0.dev382 [skip ci]
  ([`b647ab6`](https://github.com/homeassistant-ai/ha-mcp/commit/b647ab6eea76a030c7e3c46f72d732ff339398fe))
- **addon**: Publish dev addon version 7.6.0.dev381 [skip ci]
  ([`e999c24`](https://github.com/homeassistant-ai/ha-mcp/commit/e999c242553a60561ca06dbefe3177c627bb9e2d))
- Sync tool docs after merge [skip ci]
  ([`50d76b0`](https://github.com/homeassistant-ai/ha-mcp/commit/50d76b04a0691c565fafaabc76fd4bd3cf321953))
- **addon**: Publish dev addon version 7.6.0.dev380 [skip ci]
  ([`ebdc69b`](https://github.com/homeassistant-ai/ha-mcp/commit/ebdc69bde929fd3342234149956af0710b9f260a))
- Sync tool docs after merge [skip ci]
  ([`1f03967`](https://github.com/homeassistant-ai/ha-mcp/commit/1f03967160156cbffdfc9fc7deb5ed9a2b25f2ba))
- **addon**: Publish dev addon version 7.6.0.dev379 [skip ci]
  ([`7cabb2e`](https://github.com/homeassistant-ai/ha-mcp/commit/7cabb2ec342561dbc74251f7697e3c4df0a8fcb3))
- **addon**: Publish dev addon version 7.6.0.dev378 [skip ci]
  ([`7b631c7`](https://github.com/homeassistant-ai/ha-mcp/commit/7b631c75e0e4163ee13906b90ddad7a1b744e402))
- **addon**: Publish dev addon version 7.6.0.dev377 [skip ci]
  ([`ea7b614`](https://github.com/homeassistant-ai/ha-mcp/commit/ea7b614615606a8fc05a0a18cb9cafa2f0a42c0d))
- **addon**: Publish dev addon version 7.6.0.dev376 [skip ci]
  ([`fd33ecd`](https://github.com/homeassistant-ai/ha-mcp/commit/fd33ecd4d260f5fc2ab4f229c90b1a4514c5ccde))
- Sync tool docs after merge [skip ci]
  ([`ba8fae3`](https://github.com/homeassistant-ai/ha-mcp/commit/ba8fae3cc4f451dc65022756a86cce5275f6e35f))
- **addon**: Publish dev addon version 7.6.0.dev375 [skip ci]
  ([`afcd0d8`](https://github.com/homeassistant-ai/ha-mcp/commit/afcd0d8e061e1cba65fd119727fc64ed77f1818d))
- Sync tool docs after merge [skip ci]
  ([`f9b55ad`](https://github.com/homeassistant-ai/ha-mcp/commit/f9b55ad59c6f03d6a98c3d3f6dd52ba9ff7c8a7a))
- **addon**: Publish dev addon version 7.6.0.dev374 [skip ci]
  ([`eecdf8b`](https://github.com/homeassistant-ai/ha-mcp/commit/eecdf8b058ba932ba94cfb292ebf2331a54d2def))
- Sync tool docs after merge [skip ci]
  ([`ef16a8a`](https://github.com/homeassistant-ai/ha-mcp/commit/ef16a8a197c76e51a49ae6ac69eecb15d202a194))
- **addon**: Publish dev addon version 7.6.0.dev373 [skip ci]
  ([`9a93703`](https://github.com/homeassistant-ai/ha-mcp/commit/9a9370362fe5573ac71cd6e90353877faccce8a6))
- **addon**: Publish dev addon version 7.6.0.dev372 [skip ci]
  ([`7352ce8`](https://github.com/homeassistant-ai/ha-mcp/commit/7352ce88ef59feef74c29ab47f6991cc2ac132a8))
- **addon**: Publish dev addon version 7.6.0.dev371 [skip ci]
  ([`d7601f5`](https://github.com/homeassistant-ai/ha-mcp/commit/d7601f5796308e1c42a01dbce9861460027a93c4))
- Sync tool docs after merge [skip ci]
  ([`d0a4482`](https://github.com/homeassistant-ai/ha-mcp/commit/d0a448276306a08f8a52c4645f1b9c5aeaafd68e))
- **addon**: Publish dev addon version 7.6.0.dev370 [skip ci]
  ([`f83c32c`](https://github.com/homeassistant-ai/ha-mcp/commit/f83c32c747ddaeccffb040bc28d3e08d7507ded4))
- **addon**: Publish dev addon version 7.6.0.dev369 [skip ci]
  ([`ea16661`](https://github.com/homeassistant-ai/ha-mcp/commit/ea16661f1d144feada93aee119b625f968f990cb))
- **addon**: Publish dev addon version 7.6.0.dev368 [skip ci]
  ([`af49ea5`](https://github.com/homeassistant-ai/ha-mcp/commit/af49ea5aff5c9f54914943cc2f8a67ebeea2fdee))
- **addon**: Publish dev addon version 7.6.0.dev367 [skip ci]
  ([`7dd5f4c`](https://github.com/homeassistant-ai/ha-mcp/commit/7dd5f4cd0237ef4d80a454efe4dfc0801f18fdbd))
- **addon**: Publish dev addon version 7.6.0.dev366 [skip ci]
  ([`6273b7e`](https://github.com/homeassistant-ai/ha-mcp/commit/6273b7ec8695706948eefe8e617cac39363940ce))
- Sync tool docs after merge [skip ci]
  ([`4e3fd5f`](https://github.com/homeassistant-ai/ha-mcp/commit/4e3fd5f331ce5fefa8e0e5560da411de17f2a8db))
- **addon**: Publish dev addon version 7.6.0.dev365 [skip ci]
  ([`f3bf17b`](https://github.com/homeassistant-ai/ha-mcp/commit/f3bf17be132ebc67005c33a3c8fde85025d56809))
- Sync tool docs after merge [skip ci]
  ([`5307b60`](https://github.com/homeassistant-ai/ha-mcp/commit/5307b608879154fccf67bc1894f61e97f26719c6))
- **addon**: Publish dev addon version 7.6.0.dev364 [skip ci]
  ([`76ec1ae`](https://github.com/homeassistant-ai/ha-mcp/commit/76ec1aefab8e904abf620bf1c1130e5f5d0c4bd9))
- Sync tool docs after merge [skip ci]
  ([`be864b5`](https://github.com/homeassistant-ai/ha-mcp/commit/be864b5d685f167334487d4fe83324afbb4999ca))
- **addon**: Publish dev addon version 7.6.0.dev363 [skip ci]
  ([`da10c75`](https://github.com/homeassistant-ai/ha-mcp/commit/da10c75a248dcc19442e4db6f34f325683cb0a71))
- **addon**: Publish dev addon version 7.6.0.dev362 [skip ci]
  ([`69f4527`](https://github.com/homeassistant-ai/ha-mcp/commit/69f45279707d18c724bd702ced46111b26cd000c))
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

### Refactoring

- Extract settings-UI JavaScript and CSS to separate files
  ([#1505](https://github.com/homeassistant-ai/ha-mcp/pull/1505))
- Collapse settings-UI route registration into one table
  ([#1504](https://github.com/homeassistant-ai/ha-mcp/pull/1504))

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
- **haos-e2e**: Add parallel inaddon test tier (ha-mcp runs inside HAOS addon)
  ([#1361](https://github.com/homeassistant-ai/ha-mcp/pull/1361))
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

- **agents**: Drop ha_backup_create + ha_backup_restore from accepted exceptions
  ([#1445](https://github.com/homeassistant-ai/ha-mcp/pull/1445))
- Update contributors list [contributors-updated]
  ([`c7665a6`](https://github.com/homeassistant-ai/ha-mcp/commit/c7665a6f5a08737d8fcbebbbe6afdc88ec5c4901))
- **overview**: Enumerate dismissed_repair_count in fields= description + static drift test
  ([#1411](https://github.com/homeassistant-ai/ha-mcp/pull/1411))
- Credit @tomwilkie and six other contributors in README
  ([#1400](https://github.com/homeassistant-ai/ha-mcp/pull/1400))
- **#1157**: Bump skills-vendor + auto-update via Renovate + native for: field + scrub eval_template anti-patterns
  ([#1383](https://github.com/homeassistant-ai/ha-mcp/pull/1383))
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
- Surface tool-discovery / categorized search
  ([#1123](https://github.com/homeassistant-ai/ha-mcp/pull/1123))
- Fix two stale ha_get_skill_guide references missed in #1289
  ([#1305](https://github.com/homeassistant-ai/ha-mcp/pull/1305))
- Clarify setup wizard placeholders need braces removed (#1284)
  ([#1286](https://github.com/homeassistant-ai/ha-mcp/pull/1286))

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
- Exit on HA container death + daily reset before CI check
  ([#1295](https://github.com/homeassistant-ai/ha-mcp/pull/1295))
- Align ha_config_set_dashboard with sibling re-fetch-after-save pattern (#1291)
  ([#1301](https://github.com/homeassistant-ai/ha-mcp/pull/1301))
- Allow str.replace in python_transform; hint at search mode on IndexError
  ([#1287](https://github.com/homeassistant-ai/ha-mcp/pull/1287))
- **array_patch**: Tighten validation and surface silent failures
  ([#1285](https://github.com/homeassistant-ai/ha-mcp/pull/1285))
- HA Core proxy fallback for ha_get_logs(source=system_service) on non-addon installs
  ([#1283](https://github.com/homeassistant-ai/ha-mcp/pull/1283))

### Performance Improvements

- Tighten _poll_for_automation_entity first-poll cadence
  ([#1384](https://github.com/homeassistant-ai/ha-mcp/pull/1384))
- Parallelize ha_get_system_health optional sections via asyncio.gather
  ([#1336](https://github.com/homeassistant-ai/ha-mcp/pull/1336))

### Refactoring

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

---
<details>
<summary>Internal Changes</summary>


### Fixed

- **ci**: Install libguestfs in HAOS publish workflow
  ([#1358](https://github.com/homeassistant-ai/ha-mcp/pull/1358))

### Build System

- **deps**: Bump esbuild from 0.24.2 to 0.25.0 in /tests/js
  ([#1427](https://github.com/homeassistant-ai/ha-mcp/pull/1427))
- **deps**: Bump devalue from 5.6.4 to 5.8.1 in /site
  ([#1282](https://github.com/homeassistant-ai/ha-mcp/pull/1282))
- **deps**: Bump astro from 6.1.6 to 6.1.10 in /site
  ([#1274](https://github.com/homeassistant-ai/ha-mcp/pull/1274))

### Chores

- **addon**: Publish dev addon version 7.5.0.dev360 [skip ci]
  ([`ad7aed1`](https://github.com/homeassistant-ai/ha-mcp/commit/ad7aed13d6843967e2a259ae46fec2bbc6abc896))
- Sync tool docs after merge [skip ci]
  ([`9c4984f`](https://github.com/homeassistant-ai/ha-mcp/commit/9c4984fc44d30c15a96372fbc1b401fbd679dc8f))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.5.4
  ([#1450](https://github.com/homeassistant-ai/ha-mcp/pull/1450))
- **addon**: Publish dev addon version 7.5.0.dev359 [skip ci]
  ([`b82d4ee`](https://github.com/homeassistant-ai/ha-mcp/commit/b82d4eea85792b620368d4859e99c143f9fbfadf))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.16
  ([#1449](https://github.com/homeassistant-ai/ha-mcp/pull/1449))
- **addon**: Publish dev addon version 7.5.0.dev358 [skip ci]
  ([`53fba6d`](https://github.com/homeassistant-ai/ha-mcp/commit/53fba6da76ce8c78a0de035883233a36762601bc))
- Sync tool docs after merge [skip ci]
  ([`dc7750d`](https://github.com/homeassistant-ai/ha-mcp/commit/dc7750de16b60f70e40e6e5c8908d9e84e544683))
- **addon**: Publish dev addon version 7.5.0.dev357 [skip ci]
  ([`fd150c9`](https://github.com/homeassistant-ai/ha-mcp/commit/fd150c9b9c4bdb9026e67eae340226c36b270d94))
- **addon**: Publish dev addon version 7.5.0.dev356 [skip ci]
  ([`5fa1463`](https://github.com/homeassistant-ai/ha-mcp/commit/5fa14630229ba0d6b97d9328768e247bf9c20af4))
- **addon**: Publish dev addon version 7.5.0.dev355 [skip ci]
  ([`174ac5d`](https://github.com/homeassistant-ai/ha-mcp/commit/174ac5dca7667702cd664420c50e0101366e6c3a))
- **addon**: Publish dev addon version 7.5.0.dev354 [skip ci]
  ([`4f93989`](https://github.com/homeassistant-ai/ha-mcp/commit/4f939892dea2d1951541b1a557c51e957f6751c0))
- **addon**: Publish dev addon version 7.5.0.dev353 [skip ci]
  ([`53f282f`](https://github.com/homeassistant-ai/ha-mcp/commit/53f282f19ee736637106f5baececa3939393778e))
- **addon**: Publish dev addon version 7.5.0.dev352 [skip ci]
  ([`4911d46`](https://github.com/homeassistant-ai/ha-mcp/commit/4911d4682c4a865a93a8e821bc86bd26cef0ed5a))
- Sync tool docs after merge [skip ci]
  ([`8a79837`](https://github.com/homeassistant-ai/ha-mcp/commit/8a798373a7b2f1adb94b93e65d626ab582c7bd5d))
- **addon**: Publish dev addon version 7.5.0.dev351 [skip ci]
  ([`13afa9d`](https://github.com/homeassistant-ai/ha-mcp/commit/13afa9df026dd27332277140c34f7d41d0229efd))
- **addon**: Publish dev addon version 7.5.0.dev350 [skip ci]
  ([`e93d680`](https://github.com/homeassistant-ai/ha-mcp/commit/e93d6806025c6d7fd2ef94c509a607c4ac85c8e0))
- **addon**: Publish dev addon version 7.5.0.dev349 [skip ci]
  ([`1631ad1`](https://github.com/homeassistant-ai/ha-mcp/commit/1631ad10bdada4487d7b54049e92df6ddb29439a))
- **addon**: Publish dev addon version 7.5.0.dev348 [skip ci]
  ([`18a8aef`](https://github.com/homeassistant-ai/ha-mcp/commit/18a8aef50675c1a881b279666e0af1b093679850))
- Sync tool docs after merge [skip ci]
  ([`9e0493d`](https://github.com/homeassistant-ai/ha-mcp/commit/9e0493d248e23a8042841e90847662926395a448))
- **addon**: Publish dev addon version 7.5.0.dev347 [skip ci]
  ([`e2067e4`](https://github.com/homeassistant-ai/ha-mcp/commit/e2067e446b127097e61d07516ae0e742ae2de6c8))
- Sync tool docs after merge [skip ci]
  ([`7bdb3d4`](https://github.com/homeassistant-ai/ha-mcp/commit/7bdb3d402b60900a1012b8269e52b7cbeb400f84))
- **addon**: Publish dev addon version 7.5.0.dev346 [skip ci]
  ([`c2d4dd7`](https://github.com/homeassistant-ai/ha-mcp/commit/c2d4dd7091653a618326f665fd34b5458d26c8f6))
- Sync tool docs after merge [skip ci]
  ([`393b354`](https://github.com/homeassistant-ai/ha-mcp/commit/393b354e8a8e1280029eee9521971c693526970a))
- **addon**: Publish dev addon version 7.5.0.dev345 [skip ci]
  ([`42ede8b`](https://github.com/homeassistant-ai/ha-mcp/commit/42ede8b0e6066faf3ed6f6920629a8d94d77f69a))
- **addon**: Publish dev addon version 7.5.0.dev344 [skip ci]
  ([`e6cc7a1`](https://github.com/homeassistant-ai/ha-mcp/commit/e6cc7a1b8ed252f1aaa2b8c964e56c97a27b94c2))
- Sync tool docs after merge [skip ci]
  ([`fb35f30`](https://github.com/homeassistant-ai/ha-mcp/commit/fb35f305cbf7de6807c29bd3ada9da2f028ff730))
- **addon**: Publish dev addon version 7.5.0.dev343 [skip ci]
  ([`401b7b4`](https://github.com/homeassistant-ai/ha-mcp/commit/401b7b4d707dbb3c56a3afecad9e9d22ecbc7d1e))
- **addon**: Publish dev addon version 7.5.0.dev342 [skip ci]
  ([`0679371`](https://github.com/homeassistant-ai/ha-mcp/commit/06793710b04a985ed0d66861266b9b79494e4111))
- Sync tool docs after merge [skip ci]
  ([`f6796ec`](https://github.com/homeassistant-ai/ha-mcp/commit/f6796ec19b4596455ff54b58746bd3e075d5d280))
- **addon**: Publish dev addon version 7.5.0.dev341 [skip ci]
  ([`3654478`](https://github.com/homeassistant-ai/ha-mcp/commit/3654478f80428d15671e49162acecea19d67f672))
- **addon**: Publish dev addon version 7.5.0.dev340 [skip ci]
  ([`64f00b6`](https://github.com/homeassistant-ai/ha-mcp/commit/64f00b658846cad0e0714f37d221fb65327015cf))
- **addon**: Publish dev addon version 7.5.0.dev339 [skip ci]
  ([`d6e8873`](https://github.com/homeassistant-ai/ha-mcp/commit/d6e88731556464deb768524dc24c90fd0a622a96))
- Sync tool docs after merge [skip ci]
  ([`7525e93`](https://github.com/homeassistant-ai/ha-mcp/commit/7525e930a872871fed78f9b1461aea8e78eb5ad7))
- **addon**: Publish dev addon version 7.5.0.dev338 [skip ci]
  ([`288ca4a`](https://github.com/homeassistant-ai/ha-mcp/commit/288ca4abd00b2b366cbaf671c1ace3398cc8f2fd))
- **addon**: Publish dev addon version 7.5.0.dev337 [skip ci]
  ([`f539ae5`](https://github.com/homeassistant-ai/ha-mcp/commit/f539ae5aa881853d831e6952ff3af8bc85eb8b2a))
- **addon**: Publish dev addon version 7.5.0.dev336 [skip ci]
  ([`5568a86`](https://github.com/homeassistant-ai/ha-mcp/commit/5568a861002cfb909e29368bd4e93ef0bd2e4c03))
- **addon**: Publish dev addon version 7.5.0.dev335 [skip ci]
  ([`e069405`](https://github.com/homeassistant-ai/ha-mcp/commit/e0694054bd8627f6464379c34c1e3eec5b080a44))
- **addon**: Publish dev addon version 7.5.0.dev334 [skip ci]
  ([`f7be6ea`](https://github.com/homeassistant-ai/ha-mcp/commit/f7be6ead486a32840a935a1e5ec78a34f8003c0d))
- **addon**: Publish dev addon version 7.5.0.dev333 [skip ci]
  ([`cb480ea`](https://github.com/homeassistant-ai/ha-mcp/commit/cb480eae4d855345c19a6465351e0bdef2bdf5c3))
- Sync tool docs after merge [skip ci]
  ([`9a5bc3c`](https://github.com/homeassistant-ai/ha-mcp/commit/9a5bc3c61372159bb936d7b929166924343dc76f))
- **addon**: Publish dev addon version 7.5.0.dev332 [skip ci]
  ([`e0e59ee`](https://github.com/homeassistant-ai/ha-mcp/commit/e0e59ee08072bb4aac51e90928fab2d68f6156ce))
- Sync tool docs after merge [skip ci]
  ([`499ebf0`](https://github.com/homeassistant-ai/ha-mcp/commit/499ebf0c9d4c4ec3784dd72782fe301cd9c19d60))
- **addon**: Publish dev addon version 7.5.0.dev331 [skip ci]
  ([`3e0ce92`](https://github.com/homeassistant-ai/ha-mcp/commit/3e0ce92e11777a8fd9c86153e4c248007b8edae9))
- **addon**: Publish dev addon version 7.5.0.dev330 [skip ci]
  ([`e48d056`](https://github.com/homeassistant-ai/ha-mcp/commit/e48d056ab356e11f8b2952b6c675307d1e76a895))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.15
  ([#1376](https://github.com/homeassistant-ai/ha-mcp/pull/1376))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.5.3
  ([#1377](https://github.com/homeassistant-ai/ha-mcp/pull/1377))
- **addon**: Publish dev addon version 7.5.0.dev329 [skip ci]
  ([`c523c50`](https://github.com/homeassistant-ai/ha-mcp/commit/c523c502c5c6f282905620d3db489e9c69ec2472))
- Sync tool docs after merge [skip ci]
  ([`5b7a8aa`](https://github.com/homeassistant-ai/ha-mcp/commit/5b7a8aa0794359a90d7b89a9cc61d73d8cf038d2))
- **addon**: Publish dev addon version 7.5.0.dev328 [skip ci]
  ([`6c42fba`](https://github.com/homeassistant-ai/ha-mcp/commit/6c42fba92d4786d7498f738c53dfe50c899d0890))
- **addon**: Publish dev addon version 7.5.0.dev327 [skip ci]
  ([`aecd025`](https://github.com/homeassistant-ai/ha-mcp/commit/aecd025eeea3d1edcd9466db3720cb17de71b90a))
- **addon**: Publish dev addon version 7.5.0.dev326 [skip ci]
  ([`399c17c`](https://github.com/homeassistant-ai/ha-mcp/commit/399c17c36602a7767491df6e317186cb6291f3e4))
- **addon**: Publish dev addon version 7.5.0.dev325 [skip ci]
  ([`b580b45`](https://github.com/homeassistant-ai/ha-mcp/commit/b580b452e9007deb33acc49098238988aa768603))
- Sync tool docs after merge [skip ci]
  ([`8567c3a`](https://github.com/homeassistant-ai/ha-mcp/commit/8567c3a6e66e7bebbbad8e96b94058f8456deb14))
- **addon**: Publish dev addon version 7.5.0.dev324 [skip ci]
  ([`a65579d`](https://github.com/homeassistant-ai/ha-mcp/commit/a65579d0b6e4a8bb7f8c1ed97fd432cd770c47a4))
- **addon**: Publish dev addon version 7.5.0.dev323 [skip ci]
  ([`c7667ba`](https://github.com/homeassistant-ai/ha-mcp/commit/c7667ba66538f1ba8a06b7616fb15896bf021744))
- **addon**: Publish dev addon version 7.5.0.dev322 [skip ci]
  ([`44d15a8`](https://github.com/homeassistant-ai/ha-mcp/commit/44d15a89cbacc2c38ab39868b380414507a6708b))
- **addon**: Publish dev addon version 7.5.0.dev321 [skip ci]
  ([`8739f6c`](https://github.com/homeassistant-ai/ha-mcp/commit/8739f6c09121c88a596470df293ac8e7e0f1050a))
- **addon**: Publish dev addon version 7.5.0.dev320 [skip ci]
  ([`02b6e47`](https://github.com/homeassistant-ai/ha-mcp/commit/02b6e47f8eb6c778f709d7adca115638c2fe98a2))
- Sync tool docs after merge [skip ci]
  ([`ab68c9a`](https://github.com/homeassistant-ai/ha-mcp/commit/ab68c9a9aa94ac6861af7b59997ec679f1849503))
- **addon**: Publish dev addon version 7.5.0.dev319 [skip ci]
  ([`4472904`](https://github.com/homeassistant-ai/ha-mcp/commit/44729040c11af0bd97189080c1ec59995fc563b4))
- **addon**: Publish dev addon version 7.5.0.dev318 [skip ci]
  ([`e030dbc`](https://github.com/homeassistant-ai/ha-mcp/commit/e030dbcc99e002751834274b1d53da161ab27759))
- **addon**: Publish dev addon version 7.5.0.dev317 [skip ci]
  ([`d87855c`](https://github.com/homeassistant-ai/ha-mcp/commit/d87855cf07cef9d9d7babf0bbec74d228149327d))
- Sync tool docs after merge [skip ci]
  ([`a72a4e8`](https://github.com/homeassistant-ai/ha-mcp/commit/a72a4e8c0de99a8516f3820545642c37a31c17e5))
- **addon**: Publish dev addon version 7.5.0.dev316 [skip ci]
  ([`bd9397f`](https://github.com/homeassistant-ai/ha-mcp/commit/bd9397f3a088a24a14af66a97afcf1c97904ef61))
- **addon**: Publish dev addon version 7.5.0.dev315 [skip ci]
  ([`264bfc2`](https://github.com/homeassistant-ai/ha-mcp/commit/264bfc2fcb5ef6fe3ee6f9e073b69e0469eeafdd))
- **addon**: Publish dev addon version 7.5.0.dev314 [skip ci]
  ([`df62881`](https://github.com/homeassistant-ai/ha-mcp/commit/df6288194cdde8b625132f19b0dd3edb2142a2b3))
- **addon**: Publish dev addon version 7.5.0.dev313 [skip ci]
  ([`f6c47ca`](https://github.com/homeassistant-ai/ha-mcp/commit/f6c47caa21b0d82799b2be9330f97aa5494aa3c8))
- **addon**: Publish dev addon version 7.5.0.dev312 [skip ci]
  ([`2bb7a74`](https://github.com/homeassistant-ai/ha-mcp/commit/2bb7a74f06e0652a729a735b08fc7cdc20a034f6))
- Sync tool docs after merge [skip ci]
  ([`137e279`](https://github.com/homeassistant-ai/ha-mcp/commit/137e27943b1b704fe1d3e3a5b525b4d82bce33eb))
- **addon**: Publish dev addon version 7.5.0.dev311 [skip ci]
  ([`28324ea`](https://github.com/homeassistant-ai/ha-mcp/commit/28324ea4a92fbf61f5ee40c561286c466af2309a))
- Sync tool docs after merge [skip ci]
  ([`9a753d4`](https://github.com/homeassistant-ai/ha-mcp/commit/9a753d49abb4ccd84e874451dbcc563260f9e19d))
- **addon**: Publish dev addon version 7.5.0.dev310 [skip ci]
  ([`f893b2e`](https://github.com/homeassistant-ai/ha-mcp/commit/f893b2ebf91967d0e2548f9205a15bfe0e7d86c2))
- **addon**: Publish dev addon version 7.5.0.dev309 [skip ci]
  ([`8cbdb7b`](https://github.com/homeassistant-ai/ha-mcp/commit/8cbdb7bf318707fed1a0b3e8cb9922e6c170521e))
- **addon**: Publish dev addon version 7.5.0.dev308 [skip ci]
  ([`2d18016`](https://github.com/homeassistant-ai/ha-mcp/commit/2d18016de8eb26a18cfb471e7b5f755f1309bd36))
- **addon**: Publish dev addon version 7.5.0.dev307 [skip ci]
  ([`3fc3b28`](https://github.com/homeassistant-ai/ha-mcp/commit/3fc3b28881f237df02da8b65467fba9a7d693009))
- Sync tool docs after merge [skip ci]
  ([`9e6cff8`](https://github.com/homeassistant-ai/ha-mcp/commit/9e6cff8f641bcd8122e94fc8be9c30aa18456e80))
- **addon**: Publish dev addon version 7.5.0.dev306 [skip ci]
  ([`8bdd0fc`](https://github.com/homeassistant-ai/ha-mcp/commit/8bdd0fca9c107f45e4c3719e7616984b68876a9e))
- **addon**: Publish dev addon version 7.5.0.dev305 [skip ci]
  ([`83535b9`](https://github.com/homeassistant-ai/ha-mcp/commit/83535b99e190fb56b7ce227334331ebf2affb3e1))
- **addon**: Publish dev addon version 7.5.0.dev304 [skip ci]
  ([`1435b3a`](https://github.com/homeassistant-ai/ha-mcp/commit/1435b3a24d73503e189b83f8ea5aaeea20d9ebf3))
- **addon**: Publish dev addon version 7.5.0.dev303 [skip ci]
  ([`e2da659`](https://github.com/homeassistant-ai/ha-mcp/commit/e2da6591d6ad2def0b48a9be750cb8a41f40524d))
- Sync tool docs after merge [skip ci]
  ([`23789fa`](https://github.com/homeassistant-ai/ha-mcp/commit/23789fa978591b7d3966894082a2683ca6a6ae3b))
- **addon**: Publish dev addon version 7.5.0.dev302 [skip ci]
  ([`6c8e574`](https://github.com/homeassistant-ai/ha-mcp/commit/6c8e574e7e3d8d78b3a2e48f6a70531d7a95eed8))
- Sync tool docs after merge [skip ci]
  ([`d2329cb`](https://github.com/homeassistant-ai/ha-mcp/commit/d2329cbff39891d209317528fd7b42759c3414a8))
- **addon**: Publish dev addon version 7.5.0.dev301 [skip ci]
  ([`bb538f7`](https://github.com/homeassistant-ai/ha-mcp/commit/bb538f70d2fc57072f09cfc6202e9d3dc1a2d257))
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

### Refactoring

- Consolidate lovelace/dashboards/list through shared helper
  ([#1344](https://github.com/homeassistant-ai/ha-mcp/pull/1344))

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

- Clarify standard-mode HTTP deployment guidance
  ([#1185](https://github.com/homeassistant-ai/ha-mcp/pull/1185))
- Add Cloudflared add-on hostname alternative for tunnel service
  ([#1183](https://github.com/homeassistant-ai/ha-mcp/pull/1183))
- Align tool naming convention between AGENTS.md and styleguide (#943)
  ([#1174](https://github.com/homeassistant-ai/ha-mcp/pull/1174))
- **addon**: Note tool-list (#985)/runtime divergence; fix #1139/#1162 test conflict
  ([#1172](https://github.com/homeassistant-ai/ha-mcp/pull/1172))
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
- Replace cron with systemd for demo server (prevents process leak)
  ([#1110](https://github.com/homeassistant-ai/ha-mcp/pull/1110))
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

### Performance Improvements

- Dedupe lovelace/dashboards/list in ha_config_set_dashboard (#1085)
  ([#1191](https://github.com/homeassistant-ai/ha-mcp/pull/1191))

### Refactoring

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

---
<details>
<summary>Internal Changes</summary>


### Fixed

- **ci**: Align pr.yml E2E with --dist loadscope (#1206)
  ([#1247](https://github.com/homeassistant-ai/ha-mcp/pull/1247))
- **ci**: Switch Renovate to a GitHub App token to allow workflow-file pushes
  ([#1229](https://github.com/homeassistant-ai/ha-mcp/pull/1229))
- **ci**: Break gemini-triage retrigger loop and bump turn budget
  ([#1131](https://github.com/homeassistant-ai/ha-mcp/pull/1131))
- **ci**: Harden gemini-triage so failures stop spamming user issues
  ([#1122](https://github.com/homeassistant-ai/ha-mcp/pull/1122))
- **ci**: Unbreak hotfix-release semantic-release run
  ([#1091](https://github.com/homeassistant-ai/ha-mcp/pull/1091))

### Chores

- **addon**: Publish dev addon version 7.4.1.dev299 [skip ci]
  ([`397aa6d`](https://github.com/homeassistant-ai/ha-mcp/commit/397aa6d1c9651c32e2914802c79a065e9cff1c21))
- **addon**: Publish dev addon version 7.4.1.dev298 [skip ci]
  ([`942b7e0`](https://github.com/homeassistant-ai/ha-mcp/commit/942b7e0aada19d1391f130c098fc1c196041da37))
- Sync tool docs after merge [skip ci]
  ([`6823c47`](https://github.com/homeassistant-ai/ha-mcp/commit/6823c473494802011ac0c4ccd60bc701b2b4a978))
- **addon**: Publish dev addon version 7.4.1.dev297 [skip ci]
  ([`6eac062`](https://github.com/homeassistant-ai/ha-mcp/commit/6eac062307bc2495bef3d9844c6641ce69a42f43))
- **addon**: Publish dev addon version 7.4.1.dev296 [skip ci]
  ([`b2afe93`](https://github.com/homeassistant-ai/ha-mcp/commit/b2afe937cb74178076395dd05705850d158264c4))
- **addon**: Publish dev addon version 7.4.1.dev295 [skip ci]
  ([`4f4c4f3`](https://github.com/homeassistant-ai/ha-mcp/commit/4f4c4f306cdc5f6bf3245e30dd96e741f6bf55e6))
- **deps**: Update ghcr.io/home-assistant/home-assistant docker tag to v2026.5.1
  ([#1236](https://github.com/homeassistant-ai/ha-mcp/pull/1236))
- **addon**: Publish dev addon version 7.4.1.dev294 [skip ci]
  ([`fd24991`](https://github.com/homeassistant-ai/ha-mcp/commit/fd249912ea8460a66b55d518e2c0c79757faec01))
- **deps**: Update ghcr.io/astral-sh/uv docker tag to v0.11.13
  ([#1233](https://github.com/homeassistant-ai/ha-mcp/pull/1233))
- **addon**: Publish dev addon version 7.4.1.dev293 [skip ci]
  ([`fcc6496`](https://github.com/homeassistant-ai/ha-mcp/commit/fcc6496e8baa823ca40ff18632bfa775feb4fb2a))
- **addon**: Publish dev addon version 7.4.1.dev292 [skip ci]
  ([`2961650`](https://github.com/homeassistant-ai/ha-mcp/commit/2961650c6f2d92636007202a2211ec1df0ca6f15))
- **addon**: Publish dev addon version 7.4.1.dev291 [skip ci]
  ([`5703112`](https://github.com/homeassistant-ai/ha-mcp/commit/57031124b3294e573074202e6c091e997ff6282a))
- **addon**: Publish dev addon version 7.4.1.dev290 [skip ci]
  ([`19b2f65`](https://github.com/homeassistant-ai/ha-mcp/commit/19b2f65cf6aaafc4f395934c55e196040ae6530a))
- **addon**: Publish dev addon version 7.4.1.dev289 [skip ci]
  ([`e5a1365`](https://github.com/homeassistant-ai/ha-mcp/commit/e5a136594fc754fa95a5b63cfa4a51fd3a0ccedf))
- Sync tool docs after merge [skip ci]
  ([`d2ff93b`](https://github.com/homeassistant-ai/ha-mcp/commit/d2ff93b7ae756d285108012ec1d594806e2c6205))
- **addon**: Publish dev addon version 7.4.1.dev288 [skip ci]
  ([`0f62400`](https://github.com/homeassistant-ai/ha-mcp/commit/0f624006bdb16fb13d2536c33f3242d38838acf1))
- Sync tool docs after merge [skip ci]
  ([`c7e2066`](https://github.com/homeassistant-ai/ha-mcp/commit/c7e2066d3c9df53402e500e07fa3013db3bf621f))
- **addon**: Publish dev addon version 7.4.1.dev287 [skip ci]
  ([`c1133d4`](https://github.com/homeassistant-ai/ha-mcp/commit/c1133d434f2d3995ae9066b791b3ce1438936aa3))
- **addon**: Publish dev addon version 7.4.1.dev286 [skip ci]
  ([`1ae790e`](https://github.com/homeassistant-ai/ha-mcp/commit/1ae790e15bb11e8388be2ddc886be258a1630b62))
- **addon**: Publish dev addon version 7.4.1.dev285 [skip ci]
  ([`2387d0c`](https://github.com/homeassistant-ai/ha-mcp/commit/2387d0c3150aa58976b006b4e1c55741d4672485))
- **addon**: Publish dev addon version 7.4.1.dev284 [skip ci]
  ([`dd3a4a5`](https://github.com/homeassistant-ai/ha-mcp/commit/dd3a4a5cf34ef214055f5bf51522a0304fdb4dc5))
- **addon**: Publish dev addon version 7.4.1.dev283 [skip ci]
  ([`78af8eb`](https://github.com/homeassistant-ai/ha-mcp/commit/78af8eb944def76542d0c75a0e89e283431873aa))
- Sync tool docs after merge [skip ci]
  ([`093fd74`](https://github.com/homeassistant-ai/ha-mcp/commit/093fd743a7ffdc26c1768239eca6a0cdba712fa6))
- **addon**: Publish dev addon version 7.4.1.dev282 [skip ci]
  ([`2141e15`](https://github.com/homeassistant-ai/ha-mcp/commit/2141e154cdc7d9bc786c8ed2ce5f9e19710f5ea0))
- Sync tool docs after merge [skip ci]
  ([`7810c95`](https://github.com/homeassistant-ai/ha-mcp/commit/7810c95fc81b499481d552a24b86e6c32318bc4b))
- **addon**: Publish dev addon version 7.4.1.dev281 [skip ci]
  ([`7d79ec2`](https://github.com/homeassistant-ai/ha-mcp/commit/7d79ec2b8fc07e8c954f41d15509c6fb6081ec54))
- Sync tool docs after merge [skip ci]
  ([`a73dc81`](https://github.com/homeassistant-ai/ha-mcp/commit/a73dc8163aa6fca66f8a2cfdbc9723ea8fdb60fe))
- **addon**: Publish dev addon version 7.4.1.dev280 [skip ci]
  ([`c858ce3`](https://github.com/homeassistant-ai/ha-mcp/commit/c858ce3afcc8fcb32cb64350224b504dd262b636))
- Sync tool docs after merge [skip ci]
  ([`a587be0`](https://github.com/homeassistant-ai/ha-mcp/commit/a587be0beac0ebcbe98c44f326fd1c54fc1bb374))
- **addon**: Publish dev addon version 7.4.1.dev279 [skip ci]
  ([`b78ddb2`](https://github.com/homeassistant-ai/ha-mcp/commit/b78ddb2ae2ee35b27b90bb4404c3ea7df4dcb0c8))
- Sync tool docs after merge [skip ci]
  ([`1210725`](https://github.com/homeassistant-ai/ha-mcp/commit/1210725557ac9322e6ea0ca684507d41b2ebde8b))
- **addon**: Publish dev addon version 7.4.1.dev278 [skip ci]
  ([`a282c17`](https://github.com/homeassistant-ai/ha-mcp/commit/a282c17bd499e7020c0778dee9c5af8b685e7b0a))
- **addon**: Publish dev addon version 7.4.1.dev277 [skip ci]
  ([`1081768`](https://github.com/homeassistant-ai/ha-mcp/commit/1081768e744b004c3d3a54b9a11c315fbf0995e3))
- Sync tool docs after merge [skip ci]
  ([`e03f5d2`](https://github.com/homeassistant-ai/ha-mcp/commit/e03f5d2d005dae14379530d9d561fb4772a0841c))
- **addon**: Publish dev addon version 7.4.1.dev276 [skip ci]
  ([`c4ef680`](https://github.com/homeassistant-ai/ha-mcp/commit/c4ef680e192d0b27a710eb4d7f4427301c7ea9f0))
- **addon**: Publish dev addon version 7.4.1.dev275 [skip ci]
  ([`780422d`](https://github.com/homeassistant-ai/ha-mcp/commit/780422deb2be7af5292cc19b08ccb798f1b78537))
- Sync tool docs after merge [skip ci]
  ([`8a2bd1a`](https://github.com/homeassistant-ai/ha-mcp/commit/8a2bd1acde6560afe03a19587b10aaf753f72ed1))
- **addon**: Publish dev addon version 7.4.1.dev274 [skip ci]
  ([`f0f09de`](https://github.com/homeassistant-ai/ha-mcp/commit/f0f09de10c6e00890cdaeee679e9638d9162d309))
- **addon**: Publish dev addon version 7.4.1.dev273 [skip ci]
  ([`cb49f68`](https://github.com/homeassistant-ai/ha-mcp/commit/cb49f680d546bbd2275d3867cc6ced511e1971aa))
- **addon**: Publish dev addon version 7.4.1.dev272 [skip ci]
  ([`5097186`](https://github.com/homeassistant-ai/ha-mcp/commit/5097186c13067672381307a3a623b7e71b3cd1bd))
- **addon**: Publish dev addon version 7.4.1.dev271 [skip ci]
  ([`4714342`](https://github.com/homeassistant-ai/ha-mcp/commit/47143427f2dd4c1d1e01c286bf0a340ed85069d4))
- **addon**: Publish dev addon version 7.4.1.dev270 [skip ci]
  ([`217982a`](https://github.com/homeassistant-ai/ha-mcp/commit/217982a36663a6a24f83b6eae12573b4be3f0eee))
- **addon**: Publish dev addon version 7.4.1.dev269 [skip ci]
  ([`a65dd5f`](https://github.com/homeassistant-ai/ha-mcp/commit/a65dd5fc0027ed82036b4eeaf591b50fb15abbb6))
- Sync tool docs after merge [skip ci]
  ([`0e6b54f`](https://github.com/homeassistant-ai/ha-mcp/commit/0e6b54f5ccf338788434f7066bfb2846f6b33136))
- **addon**: Publish dev addon version 7.4.1.dev268 [skip ci]
  ([`60ba1f2`](https://github.com/homeassistant-ai/ha-mcp/commit/60ba1f2fa2a48d8ea9713c9e509b6656cbec01d4))
- **addon**: Publish dev addon version 7.4.1.dev267 [skip ci]
  ([`13412aa`](https://github.com/homeassistant-ai/ha-mcp/commit/13412aab1068fd5dd179d19b881b8c134f429ef7))
- Sync tool docs after merge [skip ci]
  ([`2702a0f`](https://github.com/homeassistant-ai/ha-mcp/commit/2702a0fe905a8f9b1fd0a12e486c73a310cbfd21))
- **addon**: Publish dev addon version 7.4.1.dev266 [skip ci]
  ([`77abe0b`](https://github.com/homeassistant-ai/ha-mcp/commit/77abe0b24e3ca2f5f6d049db1a7d989d82e08b0d))
- **addon**: Publish dev addon version 7.4.1.dev265 [skip ci]
  ([`08b69db`](https://github.com/homeassistant-ai/ha-mcp/commit/08b69db2e32e162b77e256bfed4a338a0c01722a))
- Sync tool docs after merge [skip ci]
  ([`c1f24b5`](https://github.com/homeassistant-ai/ha-mcp/commit/c1f24b5bd5b8a268e5f1ecf6619393897fc9997a))
- **addon**: Publish dev addon version 7.4.1.dev264 [skip ci]
  ([`f2583f6`](https://github.com/homeassistant-ai/ha-mcp/commit/f2583f6d87d1b6915405e70d5d19d64f476089cf))
- Sync tool docs after merge [skip ci]
  ([`c2ed2d3`](https://github.com/homeassistant-ai/ha-mcp/commit/c2ed2d30c722d2658c5fc257fa4cf57621261208))
- **addon**: Publish dev addon version 7.4.1.dev263 [skip ci]
  ([`9d43e54`](https://github.com/homeassistant-ai/ha-mcp/commit/9d43e549abead438df36d13e6cbacb5099f15f0b))
- **addon**: Publish dev addon version 7.4.1.dev262 [skip ci]
  ([`a7355c8`](https://github.com/homeassistant-ai/ha-mcp/commit/a7355c8aa7d31a6e839fceff98a7ff331b2c329f))
- Sync tool docs after merge [skip ci]
  ([`085bd8a`](https://github.com/homeassistant-ai/ha-mcp/commit/085bd8a5f984dc0fac6fac25935fa009c9c2ab0a))
- Convert agents to skills
  ([#1084](https://github.com/homeassistant-ai/ha-mcp/pull/1084))
- **addon**: Publish dev addon version 7.4.1.dev261 [skip ci]
  ([`0d1af36`](https://github.com/homeassistant-ai/ha-mcp/commit/0d1af36f2138f0ebe2403f33251dc1d133258a23))
- **addon**: Publish dev addon version 7.4.1.dev260 [skip ci]
  ([`29397dc`](https://github.com/homeassistant-ai/ha-mcp/commit/29397dc0dbf581ba0ee4a12239e28ff14211442c))
- **addon**: Publish dev addon version 7.4.1.dev259 [skip ci]
  ([`4bbc74b`](https://github.com/homeassistant-ai/ha-mcp/commit/4bbc74b048631f8854f23445cbc8b3d1abdbd4b4))
- Sync tool docs after merge [skip ci]
  ([`0f6d41e`](https://github.com/homeassistant-ai/ha-mcp/commit/0f6d41ea8b863b7dec68b14036746976ee2ac6f6))
- **addon**: Publish dev addon version 7.4.1.dev258 [skip ci]
  ([`6751d08`](https://github.com/homeassistant-ai/ha-mcp/commit/6751d08f79412e88a725a2765689e93fa68de260))
- **addon**: Publish dev addon version 7.4.1.dev257 [skip ci]
  ([`2213c89`](https://github.com/homeassistant-ai/ha-mcp/commit/2213c8957704a57bba52595472e23dab66e1fbc8))
- **addon**: Publish dev addon version 7.4.1.dev256 [skip ci]
  ([`18a366e`](https://github.com/homeassistant-ai/ha-mcp/commit/18a366e55c35b37cae1b4a0743995ab24673dd1e))
- **addon**: Publish dev addon version 7.4.1.dev255 [skip ci]
  ([`0e9b18d`](https://github.com/homeassistant-ai/ha-mcp/commit/0e9b18d8710f5b0a6dfd171a966cc3940a95eac9))
- **addon**: Publish dev addon version 7.4.1.dev254 [skip ci]
  ([`39fc65b`](https://github.com/homeassistant-ai/ha-mcp/commit/39fc65bc5171cc530042d12f30f58e05397a14ff))
- Sync tool docs after merge [skip ci]
  ([`9fa0aea`](https://github.com/homeassistant-ai/ha-mcp/commit/9fa0aea8ca5b38b48a5f958945dff6bca31d80c7))
- **addon**: Publish dev addon version 7.4.1.dev253 [skip ci]
  ([`0dcc59e`](https://github.com/homeassistant-ai/ha-mcp/commit/0dcc59e63b8e8fbec0474c23ada7258002b33c66))
- Sync tool docs after merge [skip ci]
  ([`ec7413f`](https://github.com/homeassistant-ai/ha-mcp/commit/ec7413feb7507df511da412327de57bf208218f6))
- **addon**: Publish dev addon version 7.4.1.dev252 [skip ci]
  ([`345640c`](https://github.com/homeassistant-ai/ha-mcp/commit/345640c81789f22fc8e306731a07d40b46c04821))
- **addon**: Publish dev addon version 7.4.1.dev251 [skip ci]
  ([`bab9d49`](https://github.com/homeassistant-ai/ha-mcp/commit/bab9d49683e8a01848b688d976486395394c2a7f))
- Sync tool docs after merge [skip ci]
  ([`726f0a5`](https://github.com/homeassistant-ai/ha-mcp/commit/726f0a505bdbbb11853d463790d3d70932ddb4d8))
- **addon**: Publish dev addon version 7.4.1.dev250 [skip ci]
  ([`ded04ea`](https://github.com/homeassistant-ai/ha-mcp/commit/ded04ea0af2b908fa71fd648d8b7db06027921f0))
- **addon**: Publish dev addon version 7.4.1.dev249 [skip ci]
  ([`37d5628`](https://github.com/homeassistant-ai/ha-mcp/commit/37d5628b95116c93e84a99a8fbc0d8a735109bd4))
- **addon**: Publish dev addon version 7.4.1.dev248 [skip ci]
  ([`530786a`](https://github.com/homeassistant-ai/ha-mcp/commit/530786a623cabfcd61a230122bf7c18d32798238))
- Sync tool docs after merge [skip ci]
  ([`36719c3`](https://github.com/homeassistant-ai/ha-mcp/commit/36719c32704b829651f9b502ac2925179d7414df))
- **addon**: Publish dev addon version 7.4.1.dev247 [skip ci]
  ([`4dc47b5`](https://github.com/homeassistant-ai/ha-mcp/commit/4dc47b544a2c51fba14bade724d379bd63143119))
- **addon**: Publish dev addon version 7.4.1.dev246 [skip ci]
  ([`6ffbd6a`](https://github.com/homeassistant-ai/ha-mcp/commit/6ffbd6a8f45993f76e4b64f497b5b8dfda79626e))
- Sync tool docs after merge [skip ci]
  ([`add66e3`](https://github.com/homeassistant-ai/ha-mcp/commit/add66e3310b48ad4898d37bd13f4fbcc14abc0f0))
- **addon**: Publish dev addon version 7.4.1.dev245 [skip ci]
  ([`d0114af`](https://github.com/homeassistant-ai/ha-mcp/commit/d0114af27dfb8e65aa13d6fdf0ab9a6e3bde73ff))
- Sync tool docs after merge [skip ci]
  ([`0ca41af`](https://github.com/homeassistant-ai/ha-mcp/commit/0ca41afc8ae3cf0e694c2c93a0b2d705cdff4193))
- **addon**: Publish dev addon version 7.4.1.dev244 [skip ci]
  ([`d052dd0`](https://github.com/homeassistant-ai/ha-mcp/commit/d052dd0ccfb52c96f65749007be797acdc396d09))
- **addon**: Publish dev addon version 7.4.0.dev243 [skip ci]
  ([`827bc65`](https://github.com/homeassistant-ai/ha-mcp/commit/827bc6565db89c5ea0a9ba4a798a5f1f35c404b6))
- Bump package version to 7.4.1 to match released addon
  ([`4f65497`](https://github.com/homeassistant-ai/ha-mcp/commit/4f654975ecafe9cc00b8e5fd92e9b55d15875c7b))
- **addon**: Publish dev addon version 7.4.0.dev242 [skip ci]
  ([`8ba80ae`](https://github.com/homeassistant-ai/ha-mcp/commit/8ba80aee1e942651e34d64a5c501a98d6c49adb6))
- **addon**: Publish hotfix version 7.4.1
  ([`bda75e6`](https://github.com/homeassistant-ai/ha-mcp/commit/bda75e6efb20ec97109c077361bb3f9da1c25897))
- **addon**: Publish dev addon version 7.4.0.dev241 [skip ci]
  ([`2126428`](https://github.com/homeassistant-ai/ha-mcp/commit/212642881de9d759a79922a2f4799e1dbb4c1553))

### Continuous Integration

- **deps**: Bump renovatebot/github-action in the github-actions group
  ([#1218](https://github.com/homeassistant-ai/ha-mcp/pull/1218))
- **deps**: Bump renovatebot/github-action in the github-actions group
  ([#1111](https://github.com/homeassistant-ai/ha-mcp/pull/1111))

### Refactoring

- Extract _fetch_dashboards_list helper (#1193)
  ([#1207](https://github.com/homeassistant-ai/ha-mcp/pull/1207))

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
