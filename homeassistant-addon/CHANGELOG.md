# CHANGELOG

<!-- version list -->

## v4.16.2 (2025-12-06)

### Bug Fixes

- Return helpful error message for YAML script delete attempts (#268)
  ([#268](https://github.com/homeassistant-ai/ha-mcp/pull/268),
  [`01195ae`](https://github.com/homeassistant-ai/ha-mcp/commit/01195aec5b42848b0133bdb4cc5c9e52f3786227))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`ecff2c6`](https://github.com/homeassistant-ai/ha-mcp/commit/ecff2c657c9caa0822608e170de85b656a80339e))


## v4.16.1 (2025-12-06)

### Bug Fixes

- Filter artifact download to avoid Docker buildx cache
  ([`1757e53`](https://github.com/homeassistant-ai/ha-mcp/commit/1757e537a308d43c02df2cbd12f37a6919d40c1a))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`de6e5dd`](https://github.com/homeassistant-ai/ha-mcp/commit/de6e5ddbd00a128a7040230eeda403fdd5fe668b))


## v4.16.0 (2025-12-06)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`37b8e7e`](https://github.com/homeassistant-ai/ha-mcp/commit/37b8e7e2ac72eccc61e22e379560cf4584393e52))

### Features

- Implement dual-channel release strategy (dev + stable) (#291)
  ([#291](https://github.com/homeassistant-ai/ha-mcp/pull/291),
  [`c18fd92`](https://github.com/homeassistant-ai/ha-mcp/commit/c18fd92db173d63a8123c679408387258b41d05e))


## v4.15.1 (2025-12-05)

### Bug Fixes

- **macos**: Use full path to uvx in Claude Desktop config (#284)
  ([#284](https://github.com/homeassistant-ai/ha-mcp/pull/284),
  [`0066857`](https://github.com/homeassistant-ai/ha-mcp/commit/00668575559dbdeca7d40acddafd5fdcc847d9b0))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`8a09fe5`](https://github.com/homeassistant-ai/ha-mcp/commit/8a09fe57a047ebfcafafa119a09fa7ceae225aff))


## v4.15.0 (2025-12-05)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`5c7c76b`](https://github.com/homeassistant-ai/ha-mcp/commit/5c7c76b18d805974994f5739d5dfeb486e45e2bd))

### Documentation

- Simplify signin and move manual install to step 2 (#282)
  ([#282](https://github.com/homeassistant-ai/ha-mcp/pull/282),
  [`066e67a`](https://github.com/homeassistant-ai/ha-mcp/commit/066e67a0ab0645e6c3a31d4c27786dc8c851d9d9))

### Features

- Include system info in ha_get_overview response (#283)
  ([#283](https://github.com/homeassistant-ai/ha-mcp/pull/283),
  [`f44d8d1`](https://github.com/homeassistant-ai/ha-mcp/commit/f44d8d1c6284f5dab9817d1733137ca2d8a3e0a0))


## v4.14.2 (2025-12-05)

### Bug Fixes

- Write JSON config without UTF-8 BOM on Windows (#281)
  ([#281](https://github.com/homeassistant-ai/ha-mcp/pull/281),
  [`c9ac201`](https://github.com/homeassistant-ai/ha-mcp/commit/c9ac20121bbe58795edb25c4c726c140197492a1))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`a405f61`](https://github.com/homeassistant-ai/ha-mcp/commit/a405f61b21ade8c5174d56f5be5215b296e3d480))


## v4.14.1 (2025-12-05)

### Bug Fixes

- Installer UX improvements (#280) ([#280](https://github.com/homeassistant-ai/ha-mcp/pull/280),
  [`7f2c78e`](https://github.com/homeassistant-ai/ha-mcp/commit/7f2c78e1e74b0e01f7e547b2c082df338f9616fb))

### Chores

- Update issue-to-pr-resolver agent workflow
  ([`1562ed9`](https://github.com/homeassistant-ai/ha-mcp/commit/1562ed931d1addff051f5b2f7d3314a39b6d1ad7))

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`9db7b40`](https://github.com/homeassistant-ai/ha-mcp/commit/9db7b402c78be7c241507713017485cc6add1e09))

### Documentation

- Improve onboarding UX with demo environment (#265)
  ([#265](https://github.com/homeassistant-ai/ha-mcp/pull/265),
  [`0e9115d`](https://github.com/homeassistant-ai/ha-mcp/commit/0e9115d87f8b755eb466c5254e8ba51a9bb83cbb))


## v4.14.0 (2025-12-05)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`357e1bc`](https://github.com/homeassistant-ai/ha-mcp/commit/357e1bc48211c299ed9d56da7679d36ed4139323))

### Features

- Enhance ha_get_device with Zigbee integration support (Z2M & ZHA) (#262)
  ([#262](https://github.com/homeassistant-ai/ha-mcp/pull/262),
  [`fab0100`](https://github.com/homeassistant-ai/ha-mcp/commit/fab0100d2bfa25bc32a5acf965a44331869bfcdd))


## v4.13.0 (2025-12-05)

### Chores

- Add github-issue-analyzer agent with standard comment title
  ([`5211d45`](https://github.com/homeassistant-ai/ha-mcp/commit/5211d4559bdb2a3b242ac69c87bac8a53d4d9421))

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`1ef09a4`](https://github.com/homeassistant-ai/ha-mcp/commit/1ef09a4051294c8825110c0d2ba054aace0e8466))

### Features

- Add lab setup script with auto-updates and weekly reset (#263)
  ([#263](https://github.com/homeassistant-ai/ha-mcp/pull/263),
  [`6ae3659`](https://github.com/homeassistant-ai/ha-mcp/commit/6ae3659b423b65d221d39379a7c810a65a8e3746))

### Testing

- Add HACS integration to E2E test environment (#259)
  ([#259](https://github.com/homeassistant-ai/ha-mcp/pull/259),
  [`e07ba9c`](https://github.com/homeassistant-ai/ha-mcp/commit/e07ba9c30bbd29ddc25b2c9298e1bb4df1f103f5))


## v4.12.0 (2025-12-03)

### Bug Fixes

- Add missing py.typed marker file for type hint distribution (#251)
  ([#251](https://github.com/homeassistant-ai/ha-mcp/pull/251),
  [`187672b`](https://github.com/homeassistant-ai/ha-mcp/commit/187672b16329f41397512f89bb52c93e44282f10))

### Chores

- Add fastmcp to gitignore
  ([`7db3a66`](https://github.com/homeassistant-ai/ha-mcp/commit/7db3a668235d67213338b6bccb49fdf9116daa2a))

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`727ae2a`](https://github.com/homeassistant-ai/ha-mcp/commit/727ae2a818efc6bf7e61504aa89ccb79be64e2b8))

### Documentation

- Clarify bug description prompt in template
  ([`96b9bc7`](https://github.com/homeassistant-ai/ha-mcp/commit/96b9bc7cc379f568f9b90ea2b46e708aabd276ab))

- Update bug report template to emphasize ha_bug_report tool
  ([`2e72f16`](https://github.com/homeassistant-ai/ha-mcp/commit/2e72f166d5d9b1dcd6693c2258093743585e2b6b))

### Features

- Add HACS integration tools for custom component discovery (#250)
  ([#250](https://github.com/homeassistant-ai/ha-mcp/pull/250),
  [`c3e9895`](https://github.com/homeassistant-ai/ha-mcp/commit/c3e9895a320b58ad8df21d96959ae6df8a8a623c))


## v4.11.9 (2025-12-03)

### Bug Fixes

- Improve bug report tool with better diagnostics (#256)
  ([#256](https://github.com/homeassistant-ai/ha-mcp/pull/256),
  [`5f60f74`](https://github.com/homeassistant-ai/ha-mcp/commit/5f60f74128370b7ca70730b21b2a901b6f1ce13a))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`5674a13`](https://github.com/homeassistant-ai/ha-mcp/commit/5674a13f45385bb5892fd16075a3187eebf0f45d))


## v4.11.8 (2025-12-03)

### Bug Fixes

- Disable VCS release via GitHub Action input (#257)
  ([#257](https://github.com/homeassistant-ai/ha-mcp/pull/257),
  [`53e984b`](https://github.com/homeassistant-ai/ha-mcp/commit/53e984b4d253f12021c13d86cfe3e5e1f2dfa3b3))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`c09a3f9`](https://github.com/homeassistant-ai/ha-mcp/commit/c09a3f92ba7cff0a3962866e80b9bbd8110e8374))


## v4.11.7 (2025-12-03)

### Bug Fixes

- Correct semantic-release v10 config and add release fallback (#255)
  ([#255](https://github.com/homeassistant-ai/ha-mcp/pull/255),
  [`f8cca22`](https://github.com/homeassistant-ai/ha-mcp/commit/f8cca22221d041142be9f34065a354d2cfca84b8))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`7b21732`](https://github.com/homeassistant-ai/ha-mcp/commit/7b217326cac323b9351301a22f7df77c3c8ac8b8))


## v4.11.6 (2025-12-03)

### Bug Fixes

- Create GitHub release from build-binary workflow (#254)
  ([#254](https://github.com/homeassistant-ai/ha-mcp/pull/254),
  [`8939c80`](https://github.com/homeassistant-ai/ha-mcp/commit/8939c807af84978d10d9665bdb6c5382b4bbcf67))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`a502d87`](https://github.com/homeassistant-ai/ha-mcp/commit/a502d87fc232938ed67a3a5f003be17c23272ffa))


## v4.11.5 (2025-12-03)

### Bug Fixes

- Use gh release upload to avoid target_commitish conflict (#252)
  ([#252](https://github.com/homeassistant-ai/ha-mcp/pull/252),
  [`e62e05b`](https://github.com/homeassistant-ai/ha-mcp/commit/e62e05ba3911f99a1dfbff7ef6783d86fc30e18b))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`9e4ad12`](https://github.com/homeassistant-ai/ha-mcp/commit/9e4ad128a0feffdd8cbbaf6cf7562abdc5cb6237))


## v4.11.4 (2025-12-03)

### Bug Fixes

- Trigger binary builds after SemVer Release via workflow_run (#249)
  ([#249](https://github.com/homeassistant-ai/ha-mcp/pull/249),
  [`a06ec57`](https://github.com/homeassistant-ai/ha-mcp/commit/a06ec57f3c11e4e740ba427945fd20244df74a61))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`217afba`](https://github.com/homeassistant-ai/ha-mcp/commit/217afbaaeea041c5ff5ef8cb1563836e21e8d734))


## v4.11.3 (2025-12-03)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`a848d2d`](https://github.com/homeassistant-ai/ha-mcp/commit/a848d2ddc627c01378a8804de493e94a7bba4637))

### Refactoring

- Remove MCP prompts feature (#248) ([#248](https://github.com/homeassistant-ai/ha-mcp/pull/248),
  [`32896a6`](https://github.com/homeassistant-ai/ha-mcp/commit/32896a6cf10fd61f8712ca4e16202b05251394d7))


## v4.11.2 (2025-12-02)

### Bug Fixes

- Use correct WebSocket command type for Supervisor API (#246)
  ([#246](https://github.com/homeassistant-ai/ha-mcp/pull/246),
  [`51e457c`](https://github.com/homeassistant-ai/ha-mcp/commit/51e457ca8b09af5cdb7255aa37d049c619f4867f))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`74856a2`](https://github.com/homeassistant-ai/ha-mcp/commit/74856a2440eebe52a4d755e143016bc8c60196c6))

### Documentation

- Update uvx instructions to use @latest (#241)
  ([#241](https://github.com/homeassistant-ai/ha-mcp/pull/241),
  [`d3f16e6`](https://github.com/homeassistant-ai/ha-mcp/commit/d3f16e6b578088e9e794e76362b232f817e03720))


## v4.11.1 (2025-12-02)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`af1e238`](https://github.com/homeassistant-ai/ha-mcp/commit/af1e238eae459c3dccd64ca05e24dbe960c927bf))

### Performance Improvements

- Improve startup time with lazy initialization (#237)
  ([#237](https://github.com/homeassistant-ai/ha-mcp/pull/237),
  [`2d24c48`](https://github.com/homeassistant-ai/ha-mcp/commit/2d24c4800cadccdc994c041ea2193210cb74df45))


## v4.11.0 (2025-12-02)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`c68b5bf`](https://github.com/homeassistant-ai/ha-mcp/commit/c68b5bff214251b12193c5cfb46eeaaf3d389b2a))

### Features

- Add diagnostic mode for empty automation traces (#235)
  ([#235](https://github.com/homeassistant-ai/ha-mcp/pull/235),
  [`7ceba5b`](https://github.com/homeassistant-ai/ha-mcp/commit/7ceba5b539d3600335c95c4eef0d87abe53a86c0))


## v4.10.0 (2025-12-02)

### Bug Fixes

- Improve error handling for missing env variables (#234)
  ([#234](https://github.com/homeassistant-ai/ha-mcp/pull/234),
  [`af3a169`](https://github.com/homeassistant-ai/ha-mcp/commit/af3a1698e1caf3924dfaccb7e7f1183503fac904))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`ae9f3bc`](https://github.com/homeassistant-ai/ha-mcp/commit/ae9f3bc2ba921323d392b36d2a578593a2252ee8))

### Features

- Add graceful shutdown on SIGTERM/SIGINT signals (#232)
  ([#232](https://github.com/homeassistant-ai/ha-mcp/pull/232),
  [`92bece9`](https://github.com/homeassistant-ai/ha-mcp/commit/92bece9df9d69bdbc663bee8369827c392e4ead6))

- Add ha_bug_report tool for collecting diagnostic info (#233)
  ([#233](https://github.com/homeassistant-ai/ha-mcp/pull/233),
  [`87fc533`](https://github.com/homeassistant-ai/ha-mcp/commit/87fc5333fdd1ef3115d08d75691484cd593a68b0))

- Add server icon to FastMCP configuration (#236)
  ([#236](https://github.com/homeassistant-ai/ha-mcp/pull/236),
  [`cfce352`](https://github.com/homeassistant-ai/ha-mcp/commit/cfce35238c971e5333695d83b491588a08e4fc0f))

- Add structured error handling with error codes and suggestions (#238)
  ([#238](https://github.com/homeassistant-ai/ha-mcp/pull/238),
  [`0dfc10c`](https://github.com/homeassistant-ai/ha-mcp/commit/0dfc10c5a3195b435c7dea535492684a4c09325f))

- **search**: Add graceful degradation with fallback for ha_search_entities (#231)
  ([#231](https://github.com/homeassistant-ai/ha-mcp/pull/231),
  [`36bc147`](https://github.com/homeassistant-ai/ha-mcp/commit/36bc14778b39b558f15c371eced99c5f8f1caff5))


## v4.9.0 (2025-12-02)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`aed5fa1`](https://github.com/homeassistant-ai/ha-mcp/commit/aed5fa13208f3cecde8f091a03eeafd4fc757c31))

### Features

- Add HA_TEST_PORT env var for custom test container port
  ([`4743ee8`](https://github.com/homeassistant-ai/ha-mcp/commit/4743ee82491f8df82308f80d03565bd6de6909b5))


## v4.8.5 (2025-12-01)

### Bug Fixes

- Include resource files in PyPI package distribution (#230)
  ([#230](https://github.com/homeassistant-ai/ha-mcp/pull/230),
  [`6052b29`](https://github.com/homeassistant-ai/ha-mcp/commit/6052b29690b27fd2dd8f61fbdcfb40d67ce6490e))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`9f93cf2`](https://github.com/homeassistant-ai/ha-mcp/commit/9f93cf2b046aec846a40423faa1e84ba6a347737))


## v4.8.4 (2025-12-01)

### Bug Fixes

- Resolve entity_id to unique_id for trace lookup (#229)
  ([#229](https://github.com/homeassistant-ai/ha-mcp/pull/229),
  [`3a9faf1`](https://github.com/homeassistant-ai/ha-mcp/commit/3a9faf19d8f98308a0315115cc1876c9ec057213))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`c1be33b`](https://github.com/homeassistant-ai/ha-mcp/commit/c1be33b7bd3672f8682c16ebfae5ddbc21118da4))


## v4.8.3 (2025-12-01)

### Bug Fixes

- Add error handling to search tools for better diagnostics (#227)
  ([#227](https://github.com/homeassistant-ai/ha-mcp/pull/227),
  [`aaeac64`](https://github.com/homeassistant-ai/ha-mcp/commit/aaeac6498eec8cb1c5dfa230772e9d2dc5f56229))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`3dcfefa`](https://github.com/homeassistant-ai/ha-mcp/commit/3dcfefaffdb3fb360a6ee51548cab7714123e134))


## v4.8.2 (2025-12-01)

### Bug Fixes

- Fetch Core release notes from GitHub releases API (#228)
  ([#228](https://github.com/homeassistant-ai/ha-mcp/pull/228),
  [`b118883`](https://github.com/homeassistant-ai/ha-mcp/commit/b118883b809957552b4e6207654e43d139c7ba33))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`9eabf46`](https://github.com/homeassistant-ai/ha-mcp/commit/9eabf4668883101c454ef742e91d7156bf8bd933))


## v4.8.1 (2025-12-01)

### Bug Fixes

- Add error handling to ha_deep_search (#226)
  ([#226](https://github.com/homeassistant-ai/ha-mcp/pull/226),
  [`f952fe4`](https://github.com/homeassistant-ai/ha-mcp/commit/f952fe4dacf3048346102f36d8e3b96fe81e668d))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`634d142`](https://github.com/homeassistant-ai/ha-mcp/commit/634d142adc8db6a0b0fa8d920a663d0bb6e61238))


## v4.8.0 (2025-12-01)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`7931b3f`](https://github.com/homeassistant-ai/ha-mcp/commit/7931b3fe9ebb447cd14e12dd5dd165278d910ec8))


## v4.7.7 (2025-12-01)

### Bug Fixes

- Normalize automation GET config for round-trip compatibility (#221)
  ([#221](https://github.com/homeassistant-ai/ha-mcp/pull/221),
  [`278b7a5`](https://github.com/homeassistant-ai/ha-mcp/commit/278b7a5310275389993333b9f63451f86c7385e5))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`f9c512b`](https://github.com/homeassistant-ai/ha-mcp/commit/f9c512bcb63fc9fb9ed98c6e22795c90ff7bf323))


## v4.7.6 (2025-12-01)

### Bug Fixes

- Add boolean coercion for string parameters from XML-style calls (#219)
  ([#219](https://github.com/homeassistant-ai/ha-mcp/pull/219),
  [`aa34589`](https://github.com/homeassistant-ai/ha-mcp/commit/aa3458978a8a8cb66f3af6d76d0de4450f5f282a))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`f983929`](https://github.com/homeassistant-ai/ha-mcp/commit/f983929b722cbb655364aa9204a4f0eb727a1378))


## v4.7.5 (2025-12-01)

### Bug Fixes

- Add string coercion for numeric parameters (fixes #205, #206) (#217)
  ([#217](https://github.com/homeassistant-ai/ha-mcp/pull/217),
  [`59fc978`](https://github.com/homeassistant-ai/ha-mcp/commit/59fc978afa817c8c5759427608d121cc01c66822))

- Normalize automation config field names (trigger/triggers) (#215)
  ([#215](https://github.com/homeassistant-ai/ha-mcp/pull/215),
  [`c570bdf`](https://github.com/homeassistant-ai/ha-mcp/commit/c570bdf28f80f0240c5d1910560c01922cf49bfd))

- Query area/entity registries for accurate area count in overview (#216)
  ([#216](https://github.com/homeassistant-ai/ha-mcp/pull/216),
  [`3808907`](https://github.com/homeassistant-ai/ha-mcp/commit/3808907c4e6d615f5a770da6f09c36077e377bc9))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`645ca39`](https://github.com/homeassistant-ai/ha-mcp/commit/645ca39f300a9799ad9f5ecc35780bde483f20e2))


## v4.7.4 (2025-11-29)

### Bug Fixes

- Handle read-only filesystem in usage logger (#196)
  ([#196](https://github.com/homeassistant-ai/ha-mcp/pull/196),
  [`1cf4be9`](https://github.com/homeassistant-ai/ha-mcp/commit/1cf4be9f72ea4b099e1b2e8bc93b09c5bf975840))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`3291b15`](https://github.com/homeassistant-ai/ha-mcp/commit/3291b15facd1bbbb26669c8c967e7f291480046a))

### Documentation

- Add VS Code one-click install button (#195)
  ([#195](https://github.com/homeassistant-ai/ha-mcp/pull/195),
  [`9511208`](https://github.com/homeassistant-ai/ha-mcp/commit/9511208c8ef5d692cc3c62fc9bad83810f8316cd))


## v4.7.3 (2025-11-29)

### Bug Fixes

- Correct WebSocket URL construction for Supervisor proxy (#193)
  ([#193](https://github.com/homeassistant-ai/ha-mcp/pull/193),
  [`2084e22`](https://github.com/homeassistant-ai/ha-mcp/commit/2084e22ebf0f43e9b95d90d928984d53d83be04b))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`4b096b2`](https://github.com/homeassistant-ai/ha-mcp/commit/4b096b264daa6f7e4d09de5f39a44d8fb2cb602e))


## v4.7.2 (2025-11-29)

### Bug Fixes

- Handle None values in update entity attributes (#192)
  ([#192](https://github.com/homeassistant-ai/ha-mcp/pull/192),
  [`a04c7f2`](https://github.com/homeassistant-ai/ha-mcp/commit/a04c7f2d02dfa0cdd65833ced68f010ff3ce30e6))

### Chores

- Add idempotentHint, title, and tags to all tools (#190)
  ([#190](https://github.com/homeassistant-ai/ha-mcp/pull/190),
  [`2f55072`](https://github.com/homeassistant-ai/ha-mcp/commit/2f55072f1ca61cff19fc3f96fe2f31121dcff6b2))

- Add MCP tool annotations for readOnlyHint and destructiveHint (#184)
  ([#184](https://github.com/homeassistant-ai/ha-mcp/pull/184),
  [`4777d7e`](https://github.com/homeassistant-ai/ha-mcp/commit/4777d7e917209000d7c158c77d81d20fa73f0767))

- Remove obsolete run scripts
  ([`598e397`](https://github.com/homeassistant-ai/ha-mcp/commit/598e3970cc455bcbdc75ffa7ec0c80f9a503ce5f))

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`34b6d0b`](https://github.com/homeassistant-ai/ha-mcp/commit/34b6d0b2f460fb7dd64d982abf4c4d6577b07b79))

### Documentation

- Add macOS UV setup guide (#191) ([#191](https://github.com/homeassistant-ai/ha-mcp/pull/191),
  [`3947ad7`](https://github.com/homeassistant-ai/ha-mcp/commit/3947ad772203a0209e18628c685390e3f8b207e7))

- Remove duplicate CONTRIBUTING.md reference
  ([`a57e315`](https://github.com/homeassistant-ai/ha-mcp/commit/a57e315c74fdaf8b8e87c38689f41390baaf8022))

- Reorder installation methods in README (#188)
  ([#188](https://github.com/homeassistant-ai/ha-mcp/pull/188),
  [`bbb37d3`](https://github.com/homeassistant-ai/ha-mcp/commit/bbb37d32ace25f459124a4b9c22537167a159952))


## v4.7.1 (2025-11-28)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`698ed11`](https://github.com/homeassistant-ai/ha-mcp/commit/698ed1111dd7d9c36b2448e31bb3a48b7a03ae75))

### Documentation

- Update README and addon docs for new v4.x tools (#178)
  ([#178](https://github.com/homeassistant-ai/ha-mcp/pull/178),
  [`ad385b7`](https://github.com/homeassistant-ai/ha-mcp/commit/ad385b7e0d12a970bad51c7405fa6726f548d245))

### Refactoring

- Auto-discover tool modules to prevent merge conflicts (#183)
  ([#183](https://github.com/homeassistant-ai/ha-mcp/pull/183),
  [`4e78db1`](https://github.com/homeassistant-ai/ha-mcp/commit/4e78db1430930a273ce6f040c15b72d64c1105ef))


## v4.7.0 (2025-11-28)

### Bug Fixes

- **build**: Include tests package for hamcp-test-env script (#177)
  ([#177](https://github.com/homeassistant-ai/ha-mcp/pull/177),
  [`397d4e8`](https://github.com/homeassistant-ai/ha-mcp/commit/397d4e8f8bc628a0fcfd05698b277cd27d4e5924))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`a08b010`](https://github.com/homeassistant-ai/ha-mcp/commit/a08b01091e1444dae936d4e8cf754b888bfdb7fb))

### Features

- Add historical data access tools (history + statistics) (#176)
  ([#176](https://github.com/homeassistant-ai/ha-mcp/pull/176),
  [`b182db1`](https://github.com/homeassistant-ai/ha-mcp/commit/b182db1c90fdecb291bdaaa8742d49bc0ab53822))


## v4.6.0 (2025-11-28)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`ca6f6bf`](https://github.com/homeassistant-ai/ha-mcp/commit/ca6f6bf37871ae9c3b06fcb9da0b03f94612bb4b))

### Features

- Add ha_get_camera_image tool to retrieve camera snapshots (#175)
  ([#175](https://github.com/homeassistant-ai/ha-mcp/pull/175),
  [`5ea3fb3`](https://github.com/homeassistant-ai/ha-mcp/commit/5ea3fb3beab312bb3831e7039e36317f0ab2a076))


## v4.5.0 (2025-11-28)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`44f6adf`](https://github.com/homeassistant-ai/ha-mcp/commit/44f6adf4abc8db6509c96d2a5b43d027ed526f65))

### Features

- Add addon management tools (ha_list_addons, ha_list_available_addons) (#174)
  ([#174](https://github.com/homeassistant-ai/ha-mcp/pull/174),
  [`80feed9`](https://github.com/homeassistant-ai/ha-mcp/commit/80feed9adbd8d131e93b24d7d3c63f345b5b7726))


## v4.4.0 (2025-11-28)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`91d98d6`](https://github.com/homeassistant-ai/ha-mcp/commit/91d98d6e2df6929ffaaeaef360617d36364cc2ef))

### Features

- **tools**: Add ZHA device detection and integration source tools (#172)
  ([#172](https://github.com/homeassistant-ai/ha-mcp/pull/172),
  [`6df952a`](https://github.com/homeassistant-ai/ha-mcp/commit/6df952a693cab3b4376680ed245d72a76f340304))


## v4.3.0 (2025-11-28)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`b5721f5`](https://github.com/homeassistant-ai/ha-mcp/commit/b5721f5bcef7435da677adead5d61113954e43ce))

### Features

- Add Group management tools (#171) ([#171](https://github.com/homeassistant-ai/ha-mcp/pull/171),
  [`aa70234`](https://github.com/homeassistant-ai/ha-mcp/commit/aa702343473794981bd0db6a33c6d8c8f1ee5392))


## v4.2.0 (2025-11-28)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`bef1437`](https://github.com/homeassistant-ai/ha-mcp/commit/bef14377088853dd153a1d73d8e6e1488a6ce11b))

### Features

- Add ha_get_automation_traces tool for debugging automations (#170)
  ([#170](https://github.com/homeassistant-ai/ha-mcp/pull/170),
  [`3a3bd8a`](https://github.com/homeassistant-ai/ha-mcp/commit/3a3bd8a3db441233de2f0c2cd361479667b20892))


## v4.1.0 (2025-11-27)

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`9a38ec3`](https://github.com/homeassistant-ai/ha-mcp/commit/9a38ec3f38547dfaf1456f97efc98dafdca2059e))

### Documentation

- Update README with all 63 tools (#168)
  ([#168](https://github.com/homeassistant-ai/ha-mcp/pull/168),
  [`9392502`](https://github.com/homeassistant-ai/ha-mcp/commit/93925028a81835b96f05cf84a4bec6fdd90a6a0e))

### Features

- **tests**: Pin Home Assistant container version with Renovate tracking (#167)
  ([#167](https://github.com/homeassistant-ai/ha-mcp/pull/167),
  [`e22a061`](https://github.com/homeassistant-ai/ha-mcp/commit/e22a061f0ae7ffe2c730b61485060af167e83441))


## v4.0.1 (2025-11-27)

### Bug Fixes

- **search**: Resolve search_types validation and domain_filter issues (#165)
  ([#165](https://github.com/homeassistant-ai/ha-mcp/pull/165),
  [`64c97b3`](https://github.com/homeassistant-ai/ha-mcp/commit/64c97b318164d79863915b2baabb7ba8d88c316a))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`83db30b`](https://github.com/homeassistant-ai/ha-mcp/commit/83db30b23a537ef72aa53d9f731837d3909cf33a))


## v4.0.0 (2025-11-27)

### Build System

- **deps**: Bump astral-sh/uv (#148) ([#148](https://github.com/homeassistant-ai/ha-mcp/pull/148),
  [`bbae36e`](https://github.com/homeassistant-ai/ha-mcp/commit/bbae36e57a258eed38b3e81926ec66a6430fab06))

### Chores

- **addon**: Sync changelog for Home Assistant add-on [skip ci]
  ([`39359ad`](https://github.com/homeassistant-ai/ha-mcp/commit/39359ad482bfb943c276acfe63611635baa05f27))

### Features

- Major release with 11 new tool modules (#146)
  ([#146](https://github.com/homeassistant-ai/ha-mcp/pull/146),
  [`ca3a25e`](https://github.com/homeassistant-ai/ha-mcp/commit/ca3a25e065fde77fc62182cc098837174c90539f))


## v3.7.0 (2025-11-27)

### Bug Fixes

- **deps**: Switch dependabot from pip to uv ecosystem (#147)
  ([#147](https://github.com/homeassistant-ai/ha-mcp/pull/147),
  [`4fcec1c`](https://github.com/homeassistant-ai/ha-mcp/commit/4fcec1c533950e3c3435a4c61dae61793d768f1a))

### Features

- **addon**: Add changelog for Home Assistant add-on updates (#119)
  ([#119](https://github.com/homeassistant-ai/ha-mcp/pull/119),
  [`eaa2559`](https://github.com/homeassistant-ai/ha-mcp/commit/eaa2559c88398ca8d5edc0aa574321222dcbd2cc))


## v3.6.2 (2025-11-26)

### Bug Fixes

- **ci**: Add explicit permissions to prepare job (#117)
  ([#117](https://github.com/homeassistant-ai/ha-mcp/pull/117),
  [`90bd686`](https://github.com/homeassistant-ai/ha-mcp/commit/90bd6867fabec28fd0b27810ee921825ba7105b7))

### Chores

- Remove CHANGELOG.md (#89) ([#89](https://github.com/homeassistant-ai/ha-mcp/pull/89),
  [`383fd71`](https://github.com/homeassistant-ai/ha-mcp/commit/383fd71171499d815c415085816b62054d548f48))

### Continuous Integration

- **deps**: Bump actions/checkout from 5 to 6 (#90)
  ([#90](https://github.com/homeassistant-ai/ha-mcp/pull/90),
  [`1d7aecb`](https://github.com/homeassistant-ai/ha-mcp/commit/1d7aecbe4b0b61b97e025cb1d3917e09333f1a43))

### Documentation

- **tools**: Recommend description field for automations (#111)
  ([#111](https://github.com/homeassistant-ai/ha-mcp/pull/111),
  [`bcaf590`](https://github.com/homeassistant-ai/ha-mcp/commit/bcaf590a900d89444cd8a0d50fc2a7adfaf974b2))


## v3.6.1 (2025-11-25)

### Bug Fixes

- **docs**: Add missing --transport flag for mcp-proxy in add-on docs (#94)
  ([#94](https://github.com/homeassistant-ai/ha-mcp/pull/94),
  [`fe3d6d8`](https://github.com/homeassistant-ai/ha-mcp/commit/fe3d6d89aed9afb9d5b819cb65cad8ed0e4bbcd7))

### Build System

- **deps**: Bump astral-sh/uv (#92) ([#92](https://github.com/homeassistant-ai/ha-mcp/pull/92),
  [`f865529`](https://github.com/homeassistant-ai/ha-mcp/commit/f865529edd026d053b96b0faa92efe9844df92bf))

### Continuous Integration

- **deps**: Bump renovatebot/github-action from 44.0.3 to 44.0.4 (#91)
  ([#91](https://github.com/homeassistant-ai/ha-mcp/pull/91),
  [`3da6553`](https://github.com/homeassistant-ai/ha-mcp/commit/3da6553d4a4962752866cfa923a1d961d7ade195))


## v3.6.0 (2025-11-23)

### Features

- Python 3.13 only with automated Renovate upgrades (#88)
  ([#88](https://github.com/homeassistant-ai/ha-mcp/pull/88),
  [`492d71c`](https://github.com/homeassistant-ai/ha-mcp/commit/492d71c18359a5244be552d8268b76a78b46cf1b))


## v3.5.1 (2025-11-18)

### Bug Fixes

- Improve test isolation in test_deep_search_no_results (#80)
  ([#80](https://github.com/homeassistant-ai/ha-mcp/pull/80),
  [`15e0d18`](https://github.com/homeassistant-ai/ha-mcp/commit/15e0d181d2ac4bd2a718714972770ab97ae6f1cc))

### Documentation

- Update dashboard guide with modern best practices (#81)
  ([#81](https://github.com/homeassistant-ai/ha-mcp/pull/81),
  [`1c0d131`](https://github.com/homeassistant-ai/ha-mcp/commit/1c0d131cccf20870a7f251660bdadf50206e9206))


## v3.5.0 (2025-11-17)

### Build System

- **deps**: Bump astral-sh/uv (#66) ([#66](https://github.com/homeassistant-ai/ha-mcp/pull/66),
  [`e765769`](https://github.com/homeassistant-ai/ha-mcp/commit/e765769abd381f1a7d8261cd4eb3f916717d73be))

- **deps**: Bump astral-sh/uv (#77) ([#77](https://github.com/homeassistant-ai/ha-mcp/pull/77),
  [`f52fdaa`](https://github.com/homeassistant-ai/ha-mcp/commit/f52fdaad7501409834ddfd17b36d25946a6f8edf))

### Continuous Integration

- **deps**: Bump python-semantic-release/python-semantic-release (#65)
  ([#65](https://github.com/homeassistant-ai/ha-mcp/pull/65),
  [`cf98607`](https://github.com/homeassistant-ai/ha-mcp/commit/cf98607b62572816dd2df10d06b57e9ad0c442f0))

- **deps**: Bump python-semantic-release/python-semantic-release (#78)
  ([#78](https://github.com/homeassistant-ai/ha-mcp/pull/78),
  [`660888e`](https://github.com/homeassistant-ai/ha-mcp/commit/660888ed3f46972be43eaf5a980d563ed13723e0))

### Documentation

- Remove Code Refactoring Patterns section from AGENTS.md
  ([`f4612c9`](https://github.com/homeassistant-ai/ha-mcp/commit/f4612c9477f67b50d76b091e740383d816a1981f))

- Update AGENTS.md to reflect registry refactoring architecture
  ([`97111a5`](https://github.com/homeassistant-ai/ha-mcp/commit/97111a59c00537abf38c13fe86e2d38905d04d7a))

### Features

- Add dashboard management tools for Lovelace UI (#75)
  ([#75](https://github.com/homeassistant-ai/ha-mcp/pull/75),
  [`7945b26`](https://github.com/homeassistant-ai/ha-mcp/commit/7945b26d0503f751171587f193f881344b38b3de))


## v3.4.3 (2025-11-09)

### Bug Fixes

- Align release workflow and server manifest (#64)
  ([#64](https://github.com/homeassistant-ai/ha-mcp/pull/64),
  [`ad851b0`](https://github.com/homeassistant-ai/ha-mcp/commit/ad851b01829387ee61b61354c68ab08cc51476ad))


## v3.4.2 (2025-11-09)

### Bug Fixes

- Validate server manifest via script (#63)
  ([#63](https://github.com/homeassistant-ai/ha-mcp/pull/63),
  [`fcb58dd`](https://github.com/homeassistant-ai/ha-mcp/commit/fcb58ddbf6a25685ee7d8aee33fa0a673c8aef98))


## v3.4.1 (2025-11-09)

### Bug Fixes

- Correct release workflow indentation (#62)
  ([#62](https://github.com/homeassistant-ai/ha-mcp/pull/62),
  [`9811a70`](https://github.com/homeassistant-ai/ha-mcp/commit/9811a706d091be9c5f08717d9b3a1db784638f60))

### Continuous Integration

- Automate MCP registry publishing (#61) ([#61](https://github.com/homeassistant-ai/ha-mcp/pull/61),
  [`59d0b29`](https://github.com/homeassistant-ai/ha-mcp/commit/59d0b298084e207056cd6564b7dd7e62928e7ac2))


## v3.4.0 (2025-11-07)

### Chores

- Disable autofix workflow (#59) ([#59](https://github.com/homeassistant-ai/ha-mcp/pull/59),
  [`f15a9f7`](https://github.com/homeassistant-ai/ha-mcp/commit/f15a9f79b77f8fbaf7d4e15d08b1694e9217dc01))

### Features

- Add SSE FastMCP deployment config (#60)
  ([#60](https://github.com/homeassistant-ai/ha-mcp/pull/60),
  [`94a3501`](https://github.com/homeassistant-ai/ha-mcp/commit/94a3501e92fd6aa1cc07e7ea354527f2b4cb9479))


## v3.3.2 (2025-11-07)

### Bug Fixes

- Repair codex autofix workflow conditions (#58)
  ([#58](https://github.com/homeassistant-ai/ha-mcp/pull/58),
  [`9563971`](https://github.com/homeassistant-ai/ha-mcp/commit/9563971c0b8b491ad4430ae8f7c64ca4b8833294))


## v3.3.1 (2025-11-07)

### Bug Fixes

- **ci**: Gate autofix workflow via mode (#57)
  ([#57](https://github.com/homeassistant-ai/ha-mcp/pull/57),
  [`1813507`](https://github.com/homeassistant-ai/ha-mcp/commit/18135077db8fb671651fe11f5c6a43745ae27560))

### Chores

- Disable codex autofix workflow (#55) ([#55](https://github.com/homeassistant-ai/ha-mcp/pull/55),
  [`125c934`](https://github.com/homeassistant-ai/ha-mcp/commit/125c9340fd889a5a18552acd94f78d45b85b49ca))

### Documentation

- Simplifies the installation instructions
  ([`fd8f68d`](https://github.com/homeassistant-ai/ha-mcp/commit/fd8f68db0f5cafcb7ad6d6c6b8b00440822c44a7))


## v3.3.0 (2025-11-06)

### Chores

- Deduplicate dev dependencies (#43) ([#43](https://github.com/homeassistant-ai/ha-mcp/pull/43),
  [`e26592e`](https://github.com/homeassistant-ai/ha-mcp/commit/e26592e71024aaac7423cd852d743103a162a405))

- **ci**: Add workflow to close inactive issues (#45)
  ([#45](https://github.com/homeassistant-ai/ha-mcp/pull/45),
  [`6f54487`](https://github.com/homeassistant-ai/ha-mcp/commit/6f54487f7d4fc8cbe69d29bed1637adae51ed4f0))

### Continuous Integration

- Streamline codex autofix actions (#47) ([#47](https://github.com/homeassistant-ai/ha-mcp/pull/47),
  [`a1f01a9`](https://github.com/homeassistant-ai/ha-mcp/commit/a1f01a914b967075b0bc5283194721530ed0b07b))

- **deps**: Bump peter-evans/create-pull-request from 6 to 7 (#49)
  ([#49](https://github.com/homeassistant-ai/ha-mcp/pull/49),
  [`4ae6a0a`](https://github.com/homeassistant-ai/ha-mcp/commit/4ae6a0a92f07ded91cf036fe222357307a1110a4))

### Documentation

- Clarify agent guidance on e2e requirements (#53)
  ([#53](https://github.com/homeassistant-ai/ha-mcp/pull/53),
  [`799d441`](https://github.com/homeassistant-ai/ha-mcp/commit/799d44168ba823c805123f6268986a63263df880))

### Features

- Add pypi publish
  ([`bd6d358`](https://github.com/homeassistant-ai/ha-mcp/commit/bd6d358b46212f0102292b56751d9f3f037e673c))


## v3.2.3 (2025-10-25)

### Bug Fixes

- Try multiple codex models per step (#42)
  ([#42](https://github.com/homeassistant-ai/ha-mcp/pull/42),
  [`0bc5ee6`](https://github.com/homeassistant-ai/ha-mcp/commit/0bc5ee682c042b53794221898e2f6b0b9d9a12ed))


## v3.2.2 (2025-10-24)

### Bug Fixes

- **ci**: Streamline codex autofix credentials (#40)
  ([#40](https://github.com/homeassistant-ai/ha-mcp/pull/40),
  [`1af7905`](https://github.com/homeassistant-ai/ha-mcp/commit/1af79054b9da48e84d084b1dcd5c787b85662eaf))


## v3.2.1 (2025-10-23)

### Bug Fixes

- Retain textdistance version constraints (#39)
  ([#39](https://github.com/homeassistant-ai/ha-mcp/pull/39),
  [`62d9755`](https://github.com/homeassistant-ai/ha-mcp/commit/62d9755949036cdfe36234e6c1c4ee515e509065))

### Chores

- Use base textdistance dependency (#38) ([#38](https://github.com/homeassistant-ai/ha-mcp/pull/38),
  [`eedbe80`](https://github.com/homeassistant-ai/ha-mcp/commit/eedbe80d5cce823d1f3a445d81aea60c74c9bbd8))


## v3.2.0 (2025-10-23)

### Documentation

- Add Windows UV guide and reorganize assets (#34)
  ([#34](https://github.com/homeassistant-ai/ha-mcp/pull/34),
  [`3ba92d5`](https://github.com/homeassistant-ai/ha-mcp/commit/3ba92d5629a5cb95d917fa3aaa79d965008af1df))

### Features

- Migrate fuzzy search to textdistance (#36)
  ([#36](https://github.com/homeassistant-ai/ha-mcp/pull/36),
  [`53d217f`](https://github.com/homeassistant-ai/ha-mcp/commit/53d217f2c9c8870ef3b8671e3852b0e11fd45e71))


## v3.1.6 (2025-10-21)

### Bug Fixes

- Align add-on schema with HA Supervisor (#33)
  ([#33](https://github.com/homeassistant-ai/ha-mcp/pull/33),
  [`9da440a`](https://github.com/homeassistant-ai/ha-mcp/commit/9da440acfd21f6213a8669cd356a45aab572746d))

### Build System

- **deps**: Bump astral-sh/uv (#27) ([#27](https://github.com/homeassistant-ai/ha-mcp/pull/27),
  [`74f171a`](https://github.com/homeassistant-ai/ha-mcp/commit/74f171a2d31f88a6dda800f8afe3d218f6ffd689))


## v3.1.5 (2025-10-20)

### Refactoring

- Remove redundant static docs (#26) ([#26](https://github.com/homeassistant-ai/ha-mcp/pull/26),
  [`c3c22f8`](https://github.com/homeassistant-ai/ha-mcp/commit/c3c22f8a1d29faf28c133196148f4c3902da0456))


## v3.1.4 (2025-10-20)

### Refactoring

- Drop duplicate convenience tools (#25) ([#25](https://github.com/homeassistant-ai/ha-mcp/pull/25),
  [`c815cbe`](https://github.com/homeassistant-ai/ha-mcp/commit/c815cbefaa4bd8e2662070b433b491d2f2b5ee1d))


## v3.1.3 (2025-10-18)

### Bug Fixes

- Ha_deep_search docs (#23) ([#23](https://github.com/homeassistant-ai/ha-mcp/pull/23),
  [`0bdc53f`](https://github.com/homeassistant-ai/ha-mcp/commit/0bdc53fb51451cf919a4aa64b1282bdcbf10c9fe))


## v3.1.2 (2025-10-18)

### Bug Fixes

- Return subscription id from WebSocket result (#22)
  ([#22](https://github.com/homeassistant-ai/ha-mcp/pull/22),
  [`11acc02`](https://github.com/homeassistant-ai/ha-mcp/commit/11acc023b24c6e64bbbb178fb3a883fb755787f2))


## v3.1.1 (2025-10-18)

### Documentation

- Add ha_deep_search tool to documentation (#20)
  ([#20](https://github.com/homeassistant-ai/ha-mcp/pull/20),
  [`012559c`](https://github.com/homeassistant-ai/ha-mcp/commit/012559c39e78460dabb9a0549fd3d2d05debeebd))

### Refactoring

- Split registry.py into focused modules (2106 → 76 lines) (#21)
  ([#21](https://github.com/homeassistant-ai/ha-mcp/pull/21),
  [`6063a4b`](https://github.com/homeassistant-ai/ha-mcp/commit/6063a4b7c3714a78e12fb9ce180e64e5eadd70ef))


## v3.1.0 (2025-10-17)

### Features

- Add ha_deep_search tool for searching automation/script/helper configs (#19)
  ([#19](https://github.com/homeassistant-ai/ha-mcp/pull/19),
  [`99f8d07`](https://github.com/homeassistant-ai/ha-mcp/commit/99f8d072fe8d1498978e6c45a6a72ab4abc43bb7))


## v3.0.1 (2025-10-17)

### Bug Fixes

- Correct logbook API endpoint format (Issue #16) (#18)
  ([#18](https://github.com/homeassistant-ai/ha-mcp/pull/18),
  [`ab81f16`](https://github.com/homeassistant-ai/ha-mcp/commit/ab81f16649f18efe68c505bcd20fb5a41ba0bdb6))

### Build System

- **deps**: Bump astral-sh/uv (#17) ([#17](https://github.com/homeassistant-ai/ha-mcp/pull/17),
  [`7f84735`](https://github.com/homeassistant-ai/ha-mcp/commit/7f847354721385d4677393115bc004fccaf31836))


## v3.0.0 (2025-10-17)

### Documentation

- Finalize Docker and addon documentation with tested syntax (#15)
  ([#15](https://github.com/homeassistant-ai/ha-mcp/pull/15),
  [`2763c96`](https://github.com/homeassistant-ai/ha-mcp/commit/2763c96eb2a160c77d6dedc1e52601ecd3023649))

### Breaking Changes

- Addon configuration options changed - removed port, path, and require_auth options in favor of
  auto-generated secure paths


## v2.5.7 (2025-10-10)

### Bug Fixes

- Make addon build wait for semantic-release to complete
  ([`2ae666a`](https://github.com/homeassistant-ai/ha-mcp/commit/2ae666a4468370c39c1b7bf25b6dfb34db7ee897))


## v2.5.6 (2025-10-10)

### Bug Fixes

- Add git add to build_command to include config.yaml in version commits
  ([`0d50f24`](https://github.com/homeassistant-ai/ha-mcp/commit/0d50f24b95ae132efa53342a65236c43ebac92f8))


## v2.5.5 (2025-10-10)

### Bug Fixes

- Use direct mcp.run() instead of os.execvp with debug output
  ([`91b698b`](https://github.com/homeassistant-ai/ha-mcp/commit/91b698bba474e5ff344e038e535775c19fcdf4b8))

- Use semantic-release build_command to sync addon version in same commit
  ([`c725aaa`](https://github.com/homeassistant-ai/ha-mcp/commit/c725aaa9d143d6a0e26b65a485be36d2eda83886))

### Chores

- Configure semantic-release to update addon config.yaml version
  ([`ff65337`](https://github.com/homeassistant-ai/ha-mcp/commit/ff6533768b931f1853c8c2af37957cb01643a60b))

- Sync addon version to 2.5.4
  ([`0055dda`](https://github.com/homeassistant-ai/ha-mcp/commit/0055ddab181292cf525f83e9dc845943cf1539a2))

- Sync addon version with package semver and fix slug
  ([`8b90a76`](https://github.com/homeassistant-ai/ha-mcp/commit/8b90a766908b5c709135cd991ee49b314a35f4f8))

### Testing

- Update addon startup tests for direct mcp.run() approach
  ([`1d7ee6b`](https://github.com/homeassistant-ai/ha-mcp/commit/1d7ee6b47ea27141778ab2a254b772a68855415c))


## v2.5.4 (2025-10-10)

### Bug Fixes

- Enable host network mode for local network access
  ([`b991ddf`](https://github.com/homeassistant-ai/ha-mcp/commit/b991ddf100f458a7c5a1d6a3997ced7e8ba2c9fb))

### Chores

- Update uv.lock
  ([`80842ab`](https://github.com/homeassistant-ai/ha-mcp/commit/80842abc6e9b3ab3c6892456302c96a08b52936c))

### Testing

- Add integration tests for addon container startup
  ([`1881075`](https://github.com/homeassistant-ai/ha-mcp/commit/1881075874c38869df687b2e8e26f68262537240))


## v2.5.3 (2025-10-10)

### Bug Fixes

- Specify ha_mcp module in fastmcp run command
  ([`22ddb0b`](https://github.com/homeassistant-ai/ha-mcp/commit/22ddb0b75bd07048fb10bd394903ea18a130e20a))


## v2.5.2 (2025-10-10)

### Bug Fixes

- Correct COPY paths in Dockerfile for project root context
  ([`bcb6568`](https://github.com/homeassistant-ai/ha-mcp/commit/bcb6568d57c82815b4ec23227cd1abce15577ef2))


## v2.5.1 (2025-10-10)

### Bug Fixes

- Use Debian-based uv image instead of non-existent Alpine variant
  ([`3e94860`](https://github.com/homeassistant-ai/ha-mcp/commit/3e94860c51916e5d8b84a7a62d328122b88380b7))


## v2.5.0 (2025-10-10)

### Features

- Add HA token authentication for add-on (#14)
  ([#14](https://github.com/homeassistant-ai/ha-mcp/pull/14),
  [`e47f6a3`](https://github.com/homeassistant-ai/ha-mcp/commit/e47f6a304d93f4702b30d92d1fe39d36d21f8f47))


## v2.4.0 (2025-10-10)

### Continuous Integration

- **deps**: Bump astral-sh/setup-uv from 6 to 7 (#11)
  ([#11](https://github.com/homeassistant-ai/ha-mcp/pull/11),
  [`285b02f`](https://github.com/homeassistant-ai/ha-mcp/commit/285b02f9e9ab1769da2720db719a686d9d724698))

### Features

- Add-on pre-built images with HTTP transport (#13)
  ([#13](https://github.com/homeassistant-ai/ha-mcp/pull/13),
  [`f738754`](https://github.com/homeassistant-ai/ha-mcp/commit/f738754a4a58635cbc033c0f3d32f99f1802ef10))


## v2.3.2 (2025-10-09)

### Bug Fixes

- Add repository.yaml for HA add-on repository identification
  ([`c57e433`](https://github.com/homeassistant-ai/ha-mcp/commit/c57e43384992880393b50416774ebc9f3b60d3ef))

### Documentation

- Document repository.yaml requirement in AGENTS.md
  ([`7dfd746`](https://github.com/homeassistant-ai/ha-mcp/commit/7dfd746df27c033a8dae3c0593da287ba1c1327a))

- Revert README to simple installation instructions
  ([`2f501cf`](https://github.com/homeassistant-ai/ha-mcp/commit/2f501cf31c34e0ccbbb5870e5be79ddd1732c4d5))

### Testing

- Add repository.yaml validation tests
  ([`dc0e0df`](https://github.com/homeassistant-ai/ha-mcp/commit/dc0e0df9621ad0006c1c2241a7fa51bc82ad06f4))


## v2.3.1 (2025-10-09)

### Bug Fixes

- Limit platforms to 64-bit (amd64/arm64) supported by uv image (#12)
  ([#12](https://github.com/homeassistant-ai/ha-mcp/pull/12),
  [`65fb116`](https://github.com/homeassistant-ai/ha-mcp/commit/65fb1167a48df70b35d95092bcbcacb7c5fc13ee))


## v2.3.0 (2025-10-09)

### Documentation

- Add demo animation to README
  ([`8670474`](https://github.com/homeassistant-ai/ha-mcp/commit/86704745669af8e8ef78117f0c2edccb1dd477a9))

- Add demo animation to README
  ([`8d0c574`](https://github.com/homeassistant-ai/ha-mcp/commit/8d0c574f21a940d49b6d1aa9eb7950ca7fe5b5b8))

- Add YouTube demo link
  ([`f189df9`](https://github.com/homeassistant-ai/ha-mcp/commit/f189df9f06c8cecb068969c08c8175b0e8dd7170))

- Clarify YouTube link is same demo
  ([`cc3527c`](https://github.com/homeassistant-ai/ha-mcp/commit/cc3527c367ae36cdf01fec599a9c3a1c09eedcd3))

- Move logo to img directory
  ([`19e8394`](https://github.com/homeassistant-ai/ha-mcp/commit/19e83947d440952653629af981b1390b7cd18e74))

### Features

- Docker deployment and Home Assistant add-on support (#10)
  ([#10](https://github.com/homeassistant-ai/ha-mcp/pull/10),
  [`06ea9ac`](https://github.com/homeassistant-ai/ha-mcp/commit/06ea9ac66f51f72f27cb431dd2b121df46bab27c))


## v2.2.0 (2025-10-05)

### Features

- Add backup creation and restore tools (#9)
  ([#9](https://github.com/homeassistant-ai/ha-mcp/pull/9),
  [`14568cd`](https://github.com/homeassistant-ai/ha-mcp/commit/14568cd183f219a0d91b7424c587aeb708a14dbe))


## v2.1.0 (2025-10-02)

### Documentation

- Add Claude Code acknowledgment and remove footer tagline
  ([`291ce86`](https://github.com/homeassistant-ai/ha-mcp/commit/291ce86c8302dd8c532b0c39125adb7eb7cfa721))

### Features

- Add detail_level parameter to ha_get_overview with 4 levels (#8)
  ([#8](https://github.com/homeassistant-ai/ha-mcp/pull/8),
  [`180f478`](https://github.com/homeassistant-ai/ha-mcp/commit/180f4782f169215ca0c386b06c2e4a2b67bf43df))


## v2.0.0 (2025-10-02)

### Documentation

- Add lessons learned from ha_config_* refactoring to AGENTS.md
  ([`25a8f66`](https://github.com/homeassistant-ai/ha-mcp/commit/25a8f66dd3a5c1861fc7f756ba603ac4cb8b67c1))

- Remove non-reusable package rename documentation from AGENTS.md
  ([`d0602ba`](https://github.com/homeassistant-ai/ha-mcp/commit/d0602ba800195063ee1f8f9ab85a9983bc154920))

### Features

- Rename package and repository to ha-mcp (#7)
  ([#7](https://github.com/homeassistant-ai/ha-mcp/pull/7),
  [`949c6a2`](https://github.com/homeassistant-ai/ha-mcp/commit/949c6a2f3fd7b000645987e4e20418f8bd5ddc47))

### Breaking Changes

- Package name changed from homeassistant-mcp to ha-mcp


## v1.0.3 (2025-10-01)

### Documentation

- Fix typos and formatting in README
  ([`ebfa004`](https://github.com/homeassistant-ai/ha-mcp/commit/ebfa004f76143c3c53735bc1834ee17539980e4d))

### Refactoring

- Split ha_manage_* into ha_config_{get,set,remove}_* tools (#6)
  ([#6](https://github.com/homeassistant-ai/ha-mcp/pull/6),
  [`5548f7e`](https://github.com/homeassistant-ai/ha-mcp/commit/5548f7e09a1ea48b73d20f47fd8ecfd6d287e6c0))


## v1.0.2 (2025-09-19)

### Bug Fixes

- Documentation formatting and accuracy improvements (#2)
  ([#2](https://github.com/homeassistant-ai/ha-mcp/pull/2),
  [`2eb83e0`](https://github.com/homeassistant-ai/ha-mcp/commit/2eb83e0dd2b0767901159880cb619cbcd280911a))

- Resolve GitHub Action semantic-release configuration issues (#3)
  ([#3](https://github.com/homeassistant-ai/ha-mcp/pull/3),
  [`e4b8ad4`](https://github.com/homeassistant-ai/ha-mcp/commit/e4b8ad4af52f076cafb39ad72d36a42df54189d3))

### Continuous Integration

- **deps**: Bump python-semantic-release/python-semantic-release
  ([`a09cd92`](https://github.com/homeassistant-ai/ha-mcp/commit/a09cd929fc1dd8f2991eace3af8892af0b1b6367))


## v1.0.1 (2025-09-18)

### Bug Fixes

- Remove Docker ecosystem from dependabot config
  ([`b393282`](https://github.com/homeassistant-ai/ha-mcp/commit/b393282f7e5774ea706f364b27ff522e4af800a8))


## v1.0.0 (2025-09-18)

- Initial Release
