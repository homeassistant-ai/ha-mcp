# GitHub workflow reference

Read this document before triaging issues, changing GitHub automation, managing
a pull request, or preparing a release. Repository-wide behavioral rules remain
in [`AGENTS.md`](../../AGENTS.md); this file owns the detailed commands,
labels, bot behavior, and workflow inventory.

## Automated review

Codex reviews pull requests through `pr-codex-review-request.yml` and
`pr-codex-review-delivery.yml`, posting as
`chatgpt-codex-connector[bot]`. The request explicitly applies
[`.gemini/styleguide.md`](../../.gemini/styleguide.md). Gemini Code Assist is
retired; `.gemini/config.yaml` disables the app, but the style guide remains
the repository's code-review criteria.

CodeRabbit reviews drafts. `.coderabbit.yaml` deliberately sets:

- `reviews.auto_review.drafts: true`
- `auto_pause_after_reviewed_commits: 0`
  (the schema default pauses after five reviewed commits)
- `reviews.pre_merge_checks.docstrings.mode: "off"`
  (the default 80% repository-wide coverage quota is disabled; ordinary
  guideline-based docstring review remains enabled)

The second setting spends the per-developer hourly review allowance faster. A
rate-limited push reports that state in a comment and does not block merging;
`@coderabbitai rate limit` reports availability without requesting a review.
CodeRabbit auto-detects `AGENTS.md`; the style guide is added through
`knowledge_base.code_guidelines.filePatterns`.

Repository YAML outranks CodeRabbit UI settings and does not merge with them.
Omitted keys use schema defaults. On public repositories, a change to
`.coderabbit.yaml` does not govern its own pull request because CodeRabbit
uses the base branch configuration.
The tell on the pull request changing the file is
`Configuration used: defaults`; the new rules take effect only after merge.

Dependabot, Renovate, and `github-actions[bot]` webhook-proxy promotion pull
requests are excluded from automatic Codex and CodeRabbit review. Their exact
lists are kept in lockstep by `test_coderabbit_config.py`. A maintainer can
still request review on a promotion pull request with
`@coderabbitai review`, or request Codex with a comment that is exactly
`/review` or `@ghhamcp review`.
`.coderabbit.yaml`'s `ignore_usernames` and the
`pull_request_target` admission list enforce the exclusion. The
`issue_comment` admission list deliberately omits `github-actions[bot]`, so
a maintainer's exact manual-review command remains the only way to admit a
promotion pull request.

Division of responsibility:

- Codex: code quality, test coverage, generic security, and MCP conventions.
- CodeRabbit: line-level review against `AGENTS.md` and the style guide, plus
  the walkthrough and summary.
- `/contrib-pr-review`: repository-specific security, detailed test analysis,
  pull-request size, and issue linkage.
- `/my-pr-checker`: lifecycle management, review threads, CI, and fixes.

## Issue labels

Triage-state labels:

| Label | Meaning |
|---|---|
| `ready-to-implement` | Clear path with no unresolved decisions. |
| `needs-choices` | Multiple approaches need stakeholder input. |
| `needs-info` | Awaiting the reporter. `close-needs-info.yml` clocks from the label event, reminds on days 3, 5, and 6, and closes on day 7; an author reply removes the label. |
| `priority: high/medium/low` | Relative priority. |
| `triaged` | Historical marker from the retired triage bot. |
| `triage-failed` | Historical failure marker from the retired triage bot. |
| `issue-analyzed` | Deep analysis is complete. |

Bug and scope labels:
Bug-class labels originate in issue-template form selection, CodeRabbit
labeling, or manual triage. Scope labels are orthogonal: one issue may carry
both a bug-class label and a scope label.

| Label | Meaning |
|---|---|
| `runtime-bug` | Failure during normal operation. |
| `startup-bug` | Failure during startup, installation, or connection. |
| `agent-behavior` | Agent workflow, tool selection, or prompt behavior. |
| `addon` | Home Assistant app (add-on) deployment or Supervisor ingress. |
| `docker` | Docker or container deployment. |
| `javascript` | Website or Astro code under `site/`. |

Lifecycle and automation labels:
Lifecycle labels record state and do not double as close reasons.

| Label | Meaning |
|---|---|
| `wontfix` | Valid issue intentionally not addressed; usually records the closure rationale. |
| `blocked` | Progress depends on an upstream change, sibling pull request, or design decision; recording it lets sweepers find what is waiting. |
| `python-upgrade` | Added by Renovate's global `labels` array to every managed pull request, including non-Python updates. |

CodeRabbit issue enrichment replaces the retired GitHub Models triage bot. It
runs on new and edited issues, suggests duplicates and related work, and applies
labels from `.coderabbit.yaml`. Plans are manual: comment
`@coderabbitai plan` or select **Create Plan** in the enrichment comment.
The owning configuration keys are `issue_enrichment` and
`labeling_instructions`.

To find open issues without deep analysis:

```bash
gh issue list --state open --json number,title,labels \
  --jq '.[] | select(.labels | map(.name) | contains(["issue-analyzed"]) | not) | "#\(.number): \(.title)"'
```

Draft any analysis for approval before posting it or applying labels.
When the user says “analyze issues,” run the issue-analysis workflow
sequentially for each issue missing `issue-analyzed`.

## Review comments

After every push, inspect both human and automated feedback. Human comments have
priority. Bot findings are claims to verify against the current source,
contracts, and tests—not commands to accept blindly.

Read every CodeRabbit review body in full. Findings may exist only inside
collapsed `Outside diff range comments` or `Nitpick comments` sections, so
zero unresolved threads and a green check do not prove the round is clean. A
findings-free pass may update the walkthrough comment in place without adding a
review row.

```bash
gh api repos/{owner}/{repo}/pulls/{n}/reviews --paginate --jq '.[].body' \
  | grep -oiE "(outside diff range|nitpick) comments \([1-9][0-9]*\)|actionable comments posted: [1-9][0-9]*"

gh api repos/{owner}/{repo}/issues/{n}/comments --paginate \
  --jq '.[] | select(.user.login=="coderabbitai[bot]") | .updated_at'
```

For an accepted inline finding, implement the fix, reply on that thread with
the evidence, and resolve it. When a review contains inline comments, also post
one pull-request-level summary. Leave a thread open only when the reply asks
for clarification. The `/my-pr-checker` workflow owns the exact reply endpoint
and GraphQL `resolveReviewThread` mutation; its input field is `threadId`,
not `pullRequestReviewThreadId`.

To locate failed runs from a pull request:

```bash
gh pr checks <PR> --json bucket,link \
  --jq '.[] | select(.bucket == "fail") | .link'
```

## Pull-request lifecycle

The permission, worktree, draft, testing, scope, and completion rules are in
[`AGENTS.md`](../../AGENTS.md). Once a pull request exists, use this loop:

1. Update tests and documentation when the root testing rules require them.
2. Commit and push the scoped change.
3. Monitor CI; do not use a fixed sleep as a substitute for checking state.
4. Inspect failures with `gh run view <run-id> --log-failed`.
5. Read all review comments and full review bodies.
6. Fix verified failures and findings, then repeat until the required checks
   pass and every addressed thread is resolved.
7. If the pull request is already ready for review, refresh the description
   whenever the implemented scope has changed.

Before declaring the pull request ready, verify the current head, the complete
required-check state, and the review-thread state. Post an implementation
summary only when the pull request actually reaches that state.

## CI/CD workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `pr.yml` | Pull request | Fast checks and validation orchestration. |
| `e2e-tests.yml` | Push to `master` touching code, or manual | Full container-backend E2E validation on the pinned stable Core image. |
| `haos-e2e-tests.yml` | Pull request or manual | Six HAOS lanes against a baked qcow2; required status checks. |
| `haos-e2e-beta-tests.yml` | Push to `master`, nightly, or manual | The inaddon and embedded HAOS lanes against the current beta Supervisor and Core; skipped on push and nightly when beta equals stable. |
| `e2e-beta-tests.yml` | Push to `master`, nightly, or manual | The container-backend E2E jobs against the current beta Core image; skipped on push and nightly when beta equals the stable lane's pin. |
| `publish-dev.yml` | Push to `master` | Development `.devN` release. |
| `notify-dev-channel.yml` | Push to `master` touching `src/` | Development-testing notices. |
| `semver-release.yml` | Biweekly or manual | Stable version tag and GitHub release. |
| `release-publish.yml` | `workflow_run` after SemVer Release, or manual | Stable container images and MCP registry. |
| `build-binary.yml` | Release | Linux, macOS, and Windows binaries. |
| `addon-publish.yml` | Release | Home Assistant app publishing. |
| `sync-tool-docs.yml` | Push to `master` touching tool sources or `scripts/extract_tools.py` | Regenerate `tools.json`, README, and app `DOCS.md`. |
| `locale-sync.yml` | Daily or manual | Post-merge translations pushed directly to `master`. |

Stable container releases publish `:latest`, `:stable`, and semantic-version
tags. Development builds publish only `:dev` and `:dev-<sha>`; `:latest`
is never a development tag. Home Assistant app images use separate per-arch
repositories and an explicit `version:` pin.
The per-architecture app repositories are `-addon-{arch}` for stable and
`-addon-dev-{arch}` for development.

The fast-check order in `pr.yml` is security-sensitive. HACS and Hassfest run
before anything that executes pull-request-controlled code. The AGENTS size
check may precede them because it only reads a text file. Do not insert another
step ahead of those validators without preserving that invariant.

## Releases

Conventional commit effects:

| Prefix | Version effect | Changelog |
|---|---|---|
| `fix:`, `perf:`, `refactor:` | Patch | User-facing |
| `feat:` | Minor | User-facing |
| `feat!:` or `BREAKING CHANGE:` | Major | User-facing |
| `chore:`, `ci:`, `test:` | None | Internal |
| `docs:` | None | User-facing |
| `*:(internal)` | Normal type effect | Internal |

Releases use
[python-semantic-release](https://python-semantic-release.readthedocs.io/).
Use the `(internal)` scope when the change should not appear in user release
notes, for example:
`feat(internal): Log package version on startup`.
Every `master` commit updates the development channel; stable releases are
normally cut biweekly on Wednesday at 10:00 UTC.

For an urgent release, merge the fix through the normal branch and pull-request
flow, then manually dispatch `semver-release.yml` from `master`. Use
`force` only when a release is required but no releasable commit has landed
since the previous stable tag.

The Home Assistant app flavors and custom component have additional release
rules in [Home Assistant apps](home-assistant-apps.md) and
[custom component](custom-component.md).
