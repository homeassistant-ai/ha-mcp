# Hourly Renovate and HA Release Coverage Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task in this session. Track progress with checkboxes. Do not delegate unless separately authorized.

**Goal:** Fix Renovate first: refresh the dependency dashboard hourly and promptly propose stable Home Assistant Core, Supervisor, and HAOS updates while preserving other dependencies' existing protections and explicit manual overrides; then complete beta HAOS coverage.

**Architecture:** Keep one full-repository Renovate scan, with exact package rules separating HA release updates from ordinary dependencies. Add a guarded dashboard-edit trigger for manual requests and Renovate-readable stable image inputs; beta lanes independently resolve all three components from the upstream beta channel and key their images on those versions.

**Tech Stack:** GitHub Actions YAML, Renovate 44.50.1 JSON configuration and custom regex/datasource managers, Python HAOS image builder, pytest workflow-contract and live HAOS tests.

**Spec:** The approved in-chat design and the complete session requirements transcribed below are the specification. The user approved implementation and explicitly requested this persistent plan on 2026-09-06. No additional design approval is needed for the scope below.

## Global constraints

- Renovate correctness is primary. Beta HAOS is secondary, but remains required in the same requested PR.
- Stable HA Core, Supervisor, and HAOS updates must have no seven-day age delay and no Tuesday scheduling delay. Immediate means the next successful hourly scan, or an explicit earlier scan; GitHub scheduled execution and upstream publication can introduce latency.
- Ordinary dependencies retain the existing seven-day release-age policy and Tuesday-after-15:00-UTC automatic update window. Preserve existing timestamp-optional behavior and security-update exceptions; do not silently tighten or weaken those policies.
- Scanning ordinary dependencies hourly is permitted. It must not create or update ordinary dependency PRs outside their schedule except through existing explicit overrides/security policy.
- Human dashboard approval/rebase requests must remain effective and be processed promptly. A manual workflow dispatch runs a scan; it does not globally force ordinary dependencies past their policy.
- Stable lanes consume stable releases; beta lanes consume the latest version listed in beta metadata, including when beta currently equals stable. Do not assume every patch release had an advance beta soak.
- User correction: remove manual Core bumps from this PR. Preserve the four existing 2026.9.0 pins and initialize the new HAOS Core pin at 2026.9.0. After an explicitly approved merge, manually dispatch Renovate and verify that the bot itself proposes the latest stable Core update across all five pins; do not merge or dispatch during this PR-preparation phase.
- Preserve environment overrides and Supervisor self-update behavior; a tracked Supervisor minimum is not a freeze or downgrade requirement.
- Preserve existing beta triggers, lane topology, sole cache writer, manual forced runs, and PR coverage boundaries unless a scoped correctness fix requires a change.
- Use the existing isolated worktree and preserve unrelated work. Open a draft PR only; no merge, ready transition, releases, or branch deletion is authorized.
- Before every further push, wait for the current head’s fast CI lanes to finish (Fast Checks, Unit Tests, CodeQL, Renovate validation, site, version/changes gates, and Docker/app validation). E2E/performance runs are not the push gate. Wait for the first bot review and bundle verified fixes into the next push. Do not cancel CI.
- Local execution is limited to CodeQL, mypy, Ruff format, and lightweight unit tests in proot, as explicitly authorized on 2026-09-06. All heavier tests/builds/E2E run in GitHub CI. Do not describe unrun tests as passing.

## Prompt-by-prompt scope audit

All nine earlier task prompts were reread verbatim from this session's raw transcript on 2026-09-06, not inferred solely from a compaction summary. Times below are UTC. This section preserves the requirements in normalized spelling, not as direct quotations.

| Prompt | Required outcome | Covered by |
| --- | --- | --- |
| 12:25:07 — Investigate hamcp Renovate, stale 2026.9 dashboard entry, current stable 2026.9.1, instant E2E image updates, latest beta | Establish actual triggers, dashboard and running-image state; remove HA update delays; align stable pins and preserve beta resolution | Tasks 1–3, 5 |
| 12:30:07 — Patch releases may not have a separate beta; at least stable/main E2E should have 2026.9.1 pending | Do not depend on beta precoverage for prompt stable updates; catch up current stable Core consumers | Tasks 2–3 |
| 12:59:15 — Make a PR for hourly scans, stale-dashboard fix, immediate Core/Supervisor updates, seven days for everything else | One draft PR; hourly scan plus narrowly scoped immediate-HA policy | Tasks 1–2, 5 |
| 13:00:19 — Other dependencies may be scanned if dashboard-only outside their normal policy | Preserve ordinary creation and branch-update scheduling while refreshing discovery | Tasks 1–2 |
| 13:01:20 — Supervisor releases exist; verify whether Dependabot or Renovate handles them | Verify actual ownership and runtime self-update, then add missing explicit Renovate tracking | Tasks 1, 3 |
| 13:02:33 — Manual overrides always respected; checked box had waited 17 hours without PRs | Trigger a guarded scan on human dashboard body edits and verify checked requests bypass intended gates | Tasks 1–2, 5 |
| 13:04:47 — Track Supervisor, immediate stable PRs, beta Supervisor remains in beta lanes | Stable-channel datasource and image minimum pin; beta override preserved | Tasks 2–4 |
| 13:05:22 — HAOS itself gets the same treatment | Stable HAOS dependency also receives immediate Renovate PRs and rebuilds stable images | Tasks 2–3 |
| 13:07:00 — Beta HAOS should load in beta lanes too | Resolve beta OS, download that image, include OS in cache and skip decisions, attest running OS | Task 4 |

Latest instruction: continue until the draft PR exists; do not stop after planning or request repeat approval. Keep Renovate primary, include every requirement above, and persist progress here. Local execution is limited as specified above.

## Implementation checkpoint

- Implemented hourly scans, the guarded human dashboard-edit trigger, writer serialization, and single repository-config loading.
- Implemented immediate rules for precisely Core/Supervisor/HAOS; ordinary Tuesday/seven-day policy remains in place with off-schedule branch updates disabled.
- Added the stable Supervisor datasource and real stable image inputs. Removed the initial manual Core 2026.9.1 bump at the user’s request; all five Core pins stay at 2026.9.0 for end-to-end verification by the later live Renovate dispatch.
- Implemented beta OS resolution, download override, cache key, three-component skip decision, and live OS attestation.
- Added a credential-free CI workflow to validate configuration with the scanner's pinned Renovate engine. Full Renovate behavior has not been executed locally; production dashboard cleanup still requires the deployed workflow's next scan.
- Regression evidence: before the image/beta implementation, focused tests reported 11 failures and 3 passes; the missing OS-only-beta behavior was reproduced. After implementation, release-input, workflow-contract, and Renovate-extraction/policy tests passed (23 tests) in proot.
- Expanded focused run including Supervisor readiness tests passed: 79 tests in 35.82 seconds. Ruff format completed on the eight changed Python files.
- Draft PR: https://github.com/homeassistant-ai/ha-mcp/pull/2379, targeting master. Initial implementation commit: `309eeb85eada12c9112f293fafcb6ff99a100fc1`. All six stable HAOS lanes and container CI started; automated reviews started without extra requests.
- First CI validator run succeeded, but its log showed explicit filenames default to global validation. Corrected to `--no-global`; repository-mode validation then passed at `8d81fe0c`. All fast CI lanes at that head subsequently passed.
- First Codex review identified missing behavioral coverage for the policy and dashboard guard. Commit `c6281089` added fixtures using the pinned Renovate package’s actual rule/schedule/age/limit functions, GitHub’s expression evaluator for nine event cases, and a credential-free Supervisor datasource lookup. Its CI failed before fixtures executed because the CLI wrapper resolved to `/usr/local`, not the installed package root. Verified the pinned image Dockerfile and corrected the root to `/usr/local/renovate` with explicit package/module existence checks (also the first actionable CodeRabbit finding). All other fast lanes at `c6281089` finished successfully before the next push.
- Removed the manual Core bumps and reran the four focused lightweight test files in proot: 23 tests passed in 7.05 seconds. The four existing pin files now have no net diff against master; the new HAOS Core pin remains 2026.9.0. Commit `5a92a443` bundled this correction and the CI path fix; the PR description now reserves the upgrade for a post-merge Renovate dispatch.
- At `5a92a443`, repository-mode validation and the actual Renovate HA/ordinary schedule, age, and limit fixtures passed. CI then exposed a separate image assumption: the slim image bundles Node but no npm. Commit `e52cdb65` moved the expression-library installation to a setup-node runner step and mounted its output read-only into the pinned engine container, after all previous fast lanes and CodeRabbit completed. Replied to both Codex threads and resolved the release-policy thread with passing CI evidence. CodeRabbit’s package-root thread is also replied/resolved.
- At `e52cdb65`, the expression cases executed and revealed an incorrect test assertion for a missing changed-body field: GitHub returns falsy null, not a Boolean string. Use the official evaluator’s `truthy` helper for job admission assertions. Also run the independent Supervisor lookup after successful schema validation even if an earlier fixture fails, retaining overall job failure. The dashboard thread remains pending its complete CI result. The live scanner is not deployed yet.

## Latest verified checkpoint

- At `a08135f9`, the full [Renovate validation lane](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34038899661) passed: repository schema, actual HA/ordinary policy functions, all nine dashboard cases, frozen fixture installation, and the real promoted Supervisor lookup. All fast lanes (including Unit Tests and CodeQL) passed, container E2E passed, and CodeRabbit's current-head round had no actionable findings. All five Codex/CodeRabbit threads were replied to and resolved.
- The six [HAOS lanes](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34038899719) failed during image building before tests: first boot installed current stable Core 2026.9.1, and applying the preserved 2026.9.0 pin immediately hit `Another job is running for job group home_assistant_core`. Every lane showed the same error, with Core still `NOT_RUNNING` at onboarding.
- Verified the exact upstream contract before fixing: [Supervisor 2026.08.0 Core install/start/update](https://github.com/home-assistant/supervisor/blob/2026.08.0/supervisor/homeassistant/core.py) uses `GROUP_REJECT`; [job concurrency admission](https://github.com/home-assistant/supervisor/blob/2026.08.0/supervisor/jobs/decorator.py) and [job-group acquire](https://github.com/home-assistant/supervisor/blob/2026.08.0/supervisor/jobs/job_group.py) reject a competing job before calling the update method.
- Added a bounded, exact-error retry for that rejected Core request. The ten-minute admission deadline is separate from the existing accepted-update timeout. Other command/auth/health errors still fail; ambiguous transport outcomes retain exact-version polling without redispatch.
- Regression evidence before the fix: two new busy-job cases failed, and three semantic/auth rejection cases passed. After the fix, all four focused files passed: 84 tests in 28.97 seconds; Ruff format completed on the two touched Python files. No local VM or heavy test execution was used.
- Keep all five Core pins at 2026.9.0. No merge or production Renovate dispatch occurred. Per the user's latest instruction, do not keep editing the PR description for individual passing checks; use CI and review replies for verification evidence.

## Source-backed validation audit (after user requested no speculative CI churn)

The next validation fix is held until this audit and the prior head's fast lanes/review finish. Failed CI exposed harness assumptions, not evidence that the deployed Renovate writer had been tested.

| Boundary | Primary evidence read | Decision |
| --- | --- | --- |
| Pinned image package and tools | [Renovate 44.50.1 Dockerfile](https://github.com/renovatebot/renovate/blob/44.50.1/tools/docker/Dockerfile), [base-image 13.91.4 Dockerfile](https://github.com/renovatebot/base-image/blob/13.91.4/Dockerfile) | Package is copied to `/usr/local/renovate`; Node is explicitly bundled and symlinked. Do not derive the package root from the CLI wrapper or assume npm/Python are installed in the slim image. Prepare npm dependencies on the setup-node runner; parse lookup logs with bundled Node. |
| Job guard result | [GitHub expression literals/conversions](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#literals), [pinned expression truthiness implementation](https://github.com/actions/languageservices/blob/b71ac284fc6a7382c22ecbf3e0009a6ae216823b/expressions/src/result.ts), [context reviver](https://github.com/actions/languageservices/blob/b71ac284fc6a7382c22ecbf3e0009a6ae216823b/expressions/src/data/reviver.ts) | Conditional evaluation coerces null/empty string to false. Use the library's own `truthy` helper, not `coerceString() === "false"`. Parse the actual workflow expression and convert fixture contexts with the documented reviver. |
| Expression library setup | [Pinned package README](https://github.com/actions/languageservices/blob/b71ac284fc6a7382c22ecbf3e0009a6ae216823b/expressions/README.md), npm 0.3.61 manifest and compiled exports | The package ships ESM and requires Node >=20. Its exact published files include `dist/index.js` and `dist/result.js`; the verified runtime is Node 24. Install with lifecycle scripts disabled and mount read-only. |
| Non-mutating Supervisor lookup | [Local platform documentation](https://docs.renovatebot.com/modules/platform/local/), [44.50.1 platform implementation](https://github.com/renovatebot/renovate/blob/44.50.1/lib/modules/platform/local/index.ts), [local filesystem implementation](https://github.com/renovatebot/renovate/blob/44.50.1/lib/modules/platform/local/scm.ts) | Run from the disposable copy, with no repository arguments or GitHub credentials. Local platform forces extract/lookup and has no PR/branch writer. It reads local file contents, so changing only the temporary Supervisor fixture pin is supported. |
| Environment and configuration loading | [44.50.1 configuration overview](https://github.com/renovatebot/renovate/blob/44.50.1/docs/usage/config-overview.md), [environment parser](https://github.com/renovatebot/renovate/blob/44.50.1/lib/workers/global/config/parse/env.ts), [array coercion](https://github.com/renovatebot/renovate/blob/44.50.1/lib/workers/global/config/parse/coersions.ts) | Repository config is discovered normally, once. JSON arrays for `RENOVATE_INCLUDE_PATHS` and `RENOVATE_IGNORE_DEPS` are accepted by the actual parser. The fixture targets the builder and ignores Core/OS lookup without changing release policy. |
| Supervisor transform and result assertion | [Pinned custom datasource documentation](https://github.com/renovatebot/renovate/blob/44.50.1/lib/modules/datasource/custom/readme.md), [lookup log producer](https://github.com/renovatebot/renovate/blob/44.50.1/lib/workers/repository/process/extract-update.ts) | JSONata must produce `releases[].version`; the configured transform selects only the stable metadata's Supervisor field. Lookup emits `packageFiles with updates` at debug level. Assert a nonempty update list on the exact temporary Supervisor pin, not merely a zero CLI exit. |
| Independent CI evidence | [GitHub status-check documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#status-check-functions) | Use `!cancelled() && steps.validate.outcome == 'success'` to run the independent lookup after successful schema validation even if another fixture fails; do not use continue-on-error or hide the job failure. Print failed lookup diagnostics. |

The completed CodeRabbit round at `e52cdb65` also requested a committed expression-library lockfile and a checkpoint wording correction. Added the manifest/lockfile with registry-verified tarball integrity, using [npm ci's frozen-install contract](https://docs.npmjs.com/cli/v11/commands/npm-ci/). Version 0.3.61 has no runtime transitive dependencies, but the lockfile still records the package artifact integrity and makes later dependency-tree changes reviewable. Fixed the wording in the same bundle.

Remaining verification: run the corrected nine admission cases, frozen npm install, and actual Supervisor lookup in CI. Source verification is not a substitute for claiming those executions passed. Core stays at 2026.9.0; no production dispatch or merge is authorized.

## Resume state and investigation evidence

- Worktree: `/data/data/com.termux/files/home/ha-mcp/worktree/renovate-hourly-ha`.
- Branch: `fix/renovate-hourly-ha`; starting fetched `origin/master`: `70ec6c109d29dc140090e46076cf4c78cf17be6c`.
- Upstream: `homeassistant-ai/ha-mcp`; push remote: `kingpanther13/ha-mcp-fork`.
- Initial pre-plan state had only the workflow and Renovate configuration edits. Implementation is now published as draft PR #2379; use the checkpoint above for current verification state.
- Root `AGENTS.md`, applicable tests guidance, development/workflow references, and `SECURITY.md` were read during investigation. Re-read applicable guidance when resuming changes to those areas.
- The original workflow ran Tuesday at 15:00 UTC or by manual dispatch; it did not react to dashboard edits. The repository already exempted Core from release age and schedule, but exemption did not cause scans to run.
- [Renovate run 33871075811](https://github.com/homeassistant-ai/ha-mcp/actions/runs/33871075811) was the last observed run. It predated the current Core pin/schedule correction and logged a 2026.8.3 → 2026.9.0 update as not scheduled.
- [Dashboard #1237](https://github.com/homeassistant-ai/ha-mcp/issues/1237) contained stale Core data and checked manual requests. Its verified author is `ha-mcp-renovate[bot]`. Passing `renovate.json` as both action-global and normally discovered repository config duplicated custom-manager discovery.
- At investigation, the four container/test defaults were 2026.9.0. [Stable container run 34030642588](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34030642588) used 2026.9.0; [beta container run 34030642504](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34030642504) used 2026.9.1.
- [Stable metadata](https://version.home-assistant.io/stable.json) then listed Core 2026.9.1, Supervisor 2026.08.0, HAOS OVA 18.2. [Beta metadata](https://version.home-assistant.io/beta.json) listed Core 2026.9.1, Supervisor 2026.09.0, HAOS OVA 18.2. Refresh these before choosing final pins; they are dated observations.
- Supervisor already self-updates to its configured channel at boot; runtime explicitly waits for that update to settle. [Stable HAOS run 34028070701](https://github.com/homeassistant-ai/ha-mcp/actions/runs/34028070701) reported Supervisor 2026.08.0 and Core 2026.9.1. Do not repeat the earlier incorrect inference that Supervisor necessarily remains frozen in a cached image.
- Dependabot handles GitHub Actions and uv, not Supervisor. Renovate already tracks stable HAOS releases; Supervisor lacks a tracked stable pin.
- Beta HAOS workflow currently resolves Core/Supervisor only, so its OS stays on the stable image-builder pin. Actual OS prerelease tags include `18.2.rc1`, with a dot before `rc`.

## Task 1: Finish diagnosis and make Renovate scans timely

**Files:** `.github/workflows/renovate.yml`; `docs/agents/github-workflow.md`; new focused `tests/src/unit/test_renovate_workflow_shape.py`.

**Interface:** Schedule/manual/dashboard events start a serialized repository scan. Repository configuration is loaded once. Issue body contents never become executable commands or configuration.

- [x] Inspect actual workflow, dashboard, previous run logs, stable/beta metadata, historical Supervisor behavior, and Dependabot ownership.
- [x] Refresh the exact master head, dashboard body and open dependency PRs before final implementation so any new runs or human changes are preserved.
- [x] Finish the existing hourly trigger draft and retain workflow dispatch:

  ```yaml
  schedule:
    - cron: '17 * * * *'
  issues:
    types: [edited]
  ```

- [x] Gate issue-triggered jobs to edits of the real Renovate dashboard authored by `ha-mcp-renovate[bot]`, a non-bot sender, a changed body, and a checked request. Exclude unrelated issue edits and Renovate's own edits. Keep event data out of shell interpolation.
- [x] Serialize Renovate writers with job-level `concurrency` and `cancel-in-progress: false`; preserve the existing app-token permission boundary.
- [ ] Remove `configurationFile: renovate.json` from the action input and let normal repository discovery load it once. Verify custom manager extraction and dashboard entries are no longer duplicated in a non-mutating Renovate run.
- [ ] Add regression coverage for hourly cadence, preserved dispatch, issue guard, and single configuration loading. A representative contract assertion is:

  ```python
  assert workflow["on"]["schedule"] == [{"cron": "17 * * * *"}]
  assert "workflow_dispatch" in workflow["on"]
  assert workflow["on"]["issues"]["types"] == ["edited"]
  assert "configurationFile" not in renovate_step["with"]
  ```

- [ ] Record expected event cases: human checked dashboard edit runs; bot edit does not; unrelated issue edit does not; unchecked edit does not; manual dispatch runs. Verify guards beyond simple string presence through event-expression evaluation where available.
- [x] Document scan cadence versus PR policy and explain why checked requests previously waited for the next scheduled scan.

## Task 2: Prove the HA exception and ordinary-dependency protections

**Files:** `renovate.json`; `tests/src/unit/test_renovate_workflow_shape.py`; `docs/agents/github-workflow.md`.

**Interface:** Exact dependency names select the immediate-release policy; other packages retain inherited ordinary policy. Manual dashboard instructions remain Renovate-native overrides.

- [x] Keep global `minimumReleaseAge: "7 days"`, `minimumReleaseAgeBehaviour: "timestamp-optional"`, UTC Tuesday schedule, and existing vulnerability exception. Set `updateNotScheduled: false` so hourly scans do not churn ordinary PR branches outside their window.
- [x] Finish the immediate rule, restricted to precisely these dependencies:

  ```json
  {
    "matchDepNames": [
      "ghcr.io/home-assistant/home-assistant",
      "home-assistant/supervisor",
      "home-assistant/operating-system"
    ],
    "minimumReleaseAge": null,
    "schedule": ["at any time"],
    "prCreation": "immediate",
    "prHourlyLimit": 0,
    "prConcurrentLimit": 0
  }
  ```

- [x] Confirm inherited branch/PR caps cannot delay those HA updates and later package rules do not override this exception. Keep stable selection free of prereleases.
- [ ] Validate against the pinned Renovate engine/schema and use a non-mutating dry run or equivalent controlled fixture to exercise the following decision table. Configuration shape alone is not proof of scheduling behavior.

  | Case | Expected result |
  | --- | --- |
  | Stable Core/Supervisor/OS release younger than seven days, outside Tuesday window | Update eligible immediately |
  | Ordinary release younger than seven days, inside Tuesday window, no override | Age gate preserved |
  | Ordinary mature release outside Tuesday window, no override | Dashboard discovery only; no new or updated PR branch |
  | Ordinary mature release inside Tuesday window | Normal update eligible |
  | Explicit checked dashboard request outside normal gates | Renovate processes the requested override |
  | Hourly scan or manual workflow dispatch without checked overrides | No blanket schedule/age bypass |

- [x] Verify the checked create-all-awaiting-schedule and individual approval/rebase request paths against Renovate 44.50.1. Do not delete checked requests, disable dashboard approvals, or add a global forced schedule override.

## Task 3: Track stable image inputs without manually advancing Core

**Files:** `tests/haos_image_build/build_image.py`; `tests/haos_image_build/README.md`; `.github/workflows/pr.yml`; `.github/workflows/e2e-tests.yml`; `.github/workflows/performance-tests.yml`; `tests/test_constants.py`; `renovate.json`; focused builder/manager tests.

**Interface:** Renovate updates concrete stable literals used by actual builds. Existing `HAOS_BUILD_*` environment variables override those defaults. Changes to the builder invalidate the existing stable image cache key.

- [x] Add a stable-channel Supervisor datasource using authoritative promotion metadata, not an inference from the latest GitHub release:

  ```json
  "ha-supervisor-stable": {
    "defaultRegistryUrlTemplate": "https://version.home-assistant.io/stable.json",
    "format": "json",
    "transformTemplates": [
      "{\"releases\": [{\"version\": supervisor}], \"sourceUrl\": \"https://github.com/home-assistant/supervisor\"}"
    ]
  }
  ```

- [x] Use Supervisor calendar versioning `regex:^(?<major>\\d{4})\\.(?<minor>\\d{1,2})\\.(?<patch>\\d+)$` so zero-padded monthly versions compare correctly.
- [x] Add real builder defaults, with refreshed stable versions at implementation time:

  ```python
  # renovate: datasource=github-releases depName=home-assistant/operating-system
  STABLE_HAOS_VERSION = "18.2"
  # renovate: datasource=custom.ha-supervisor-stable depName=home-assistant/supervisor
  STABLE_SUPERVISOR_VERSION = "2026.08.0"
  # renovate: datasource=docker depName=ghcr.io/home-assistant/home-assistant
  STABLE_CORE_VERSION = "2026.9.0"

  HAOS_VERSION = os.environ.get("HAOS_BUILD_OS_VERSION", STABLE_HAOS_VERSION)
  SUPERVISOR_CHANNEL = os.environ.get("HAOS_BUILD_SUPERVISOR_CHANNEL", "stable")
  SUPERVISOR_MIN_VERSION = os.environ.get(
      "HAOS_BUILD_SUPERVISOR_MIN_VERSION", STABLE_SUPERVISOR_VERSION
  )
  CORE_VERSION = os.environ.get("HAOS_BUILD_CORE_VERSION", STABLE_CORE_VERSION)
  ```

- [x] Define `HAOS_VERSION` before constructing its download URL. Ensure existing Supervisor/Core configuration functions consume the defaults, and make their beta-specific log messages generic where now used by stable builds. Preserve newer Supervisor versions rather than forcing a downgrade.
- [x] Preserve the four existing Core image pins at 2026.9.0 and initialize the new HAOS Core pin at the same version. The user explicitly requested that Renovate, not this implementation PR, perform the later stable Core upgrade. Keep unrelated metadata/version test fixtures unchanged.
- [ ] Verify regex managers extract every intended pin exactly once; verify datasource transformation emits only the promoted stable Supervisor; verify beta/dev Supervisor versions are excluded from stable PR selection.
- [x] Confirm stable cache invalidation and all stable HAOS consumers rebuild/restore images keyed on the changed builder inputs. Do not introduce a marker pin that is unused by the running build.
- [x] Cover no-override stable defaults and explicit beta overrides in builder tests; assert the OS override is reflected in the actual download URL and the existing build workflow can still read `HAOS_VERSION`.
- [x] Update the image-builder README to describe all stable pins, environment overrides, cache invalidation, and the distinction between a Supervisor minimum and automatic channel self-updates.

## Task 4: Complete beta OS coverage without disturbing the Renovate fix

**Files:** `.github/workflows/haos-e2e-beta-tests.yml`; `tests/haos_image_build/build_image.py`; `tests/src/unit/test_haos_image_workflow_shape.py`; `tests/src/e2e/conftest.py`; `tests/src/e2e/haos_only/test_canary_addons.py`; `tests/haos_image_build/README.md`.

**Interface:** Each beta resolver emits `os_version`, `supervisor_version`, and `core_version`. Build uses `HAOS_BUILD_OS_VERSION`; live attestation uses `HAOS_EXPECTED_OS_VERSION`.

- [x] Extend both lane resolvers to read `metadata["hassos"]["ova"]`, validate stable and prerelease OS formats with `^[0-9]+\.[0-9]+([.]rc[0-9]+)?$`, and output `os_version` alongside the existing two outputs. Missing, malformed, or failed metadata must fail visibly rather than falsely skip coverage.
- [x] Extend the initial stable-versus-beta comparison to compare all three versions. Only when OS, Supervisor, and Core all match may automatic beta runs be skipped; manual dispatch still runs.
- [x] Include OS in the identical cache key used by both lanes:

  ```text
  haos-beta-image-${{ steps.versions.outputs.os_version }}-${{ steps.versions.outputs.supervisor_version }}-${{ steps.versions.outputs.core_version }}-$hash
  ```

- [x] Add `HAOS_BUILD_OS_VERSION: ${{ steps.versions.outputs.os_version }}` to both builds and preserve the existing beta Supervisor channel/minimum and exact Core overrides. Validate OS values before they become URL/path components.
- [x] Add `HAOS_EXPECTED_OS_VERSION` to both test environments and to beta-attestation marker setup. Extend `test_beta_image_versions_match_manifest` to query the running VM through the existing Supervisor WebSocket proxy:

  ```python
  response = await ha_client.send_websocket_message(
      {"type": "supervisor/api", "endpoint": "/os/info", "method": "GET"}
  )
  assert response.get("success"), response
  assert response["result"]["version"] == os.environ["HAOS_EXPECTED_OS_VERSION"]
  ```

- [x] Update existing workflow-contract tests for the third output, six individually checked channel reads, OS-aware cache key, new build environment, and expected OS version. Preserve the in-app lane as sole cache writer.
- [x] Add behavioral comparison fixtures: all equal → skip; OS-only `18.2.rc1` difference → run; Supervisor-only difference → run; Core-only difference → run; missing/invalid OS → fail. Use mocked metadata, not live release values, so the regression tests remain stable.
- [x] Confirm the OS currently being identical across channels does not hide the new path: CI must cover an OS-only beta fixture even if real metadata has no OS beta today.

## Task 5: Verification, draft PR, and honest handoff

**Files:** Relevant changed files above, this plan, and `.github/pull_request_template.md` as the read-only PR-body template.

- [x] Self-audit the completed diff against every prompt row and decision-table case. Run `git diff --check` and inspect final branch/status before committing.
- [ ] Validate Renovate configuration and non-mutating behavior separately from Python workflow tests. Do not invoke the production Renovate writer from an unmerged implementation merely to test it.
- [x] Commit scoped changes on `fix/renovate-hourly-ha`, push the fork branch, and open one draft against `homeassistant-ai/ha-mcp:master`. Use the repository PR template and preserve all headings/generated sections.
- [ ] Inspect CI on the exact pushed head, including container and stable HAOS lanes. Inspect full automated/human review bodies after every push and fix verified findings within the approved scope. Do not manually request extra reviews or cancel CI.
- [ ] Validate beta workflow behavior at the PR head through an available scoped GitHub Actions path; beta lanes do not automatically run on pull requests. If that runtime verification cannot be performed before merge, explicitly distinguish unit/contract coverage from unexecuted beta runtime coverage; do not claim all beta E2E passed.
- [ ] Refresh dashboard/run/PR evidence at handoff. Separate what the draft changes guarantee from what is active on default branch: the new cron and issue trigger are not deployed until this PR is merged.
- [ ] After an explicitly approved merge, manually dispatch the deployed Renovate workflow; verify refreshed dashboard #1237, a bot-authored stable Core upgrade across all five pins, expected Supervisor/OS discovery, and preserved ordinary-dependency policy/manual overrides. This is a post-merge validation step, not authorization to merge or dispatch now.
- [ ] Update this plan's checkboxes and resume state with commit, draft PR link, exact tested head, CI/review outcomes, and any remaining verification gaps.

## Completion criteria

The implementation is ready for handoff only when the Renovate schedule, dashboard refresh, manual-request path, stable HA tracking, ordinary dependency protections, preserved 2026.9.0 Core baseline, and beta OS selection/cache/gate/attestation changes are all accounted for. A passing fast check alone is insufficient. Keep the PR draft and report unexecuted deployment/runtime validation plainly; the actual Core upgrade is reserved for a verified Renovate PR after merge.
