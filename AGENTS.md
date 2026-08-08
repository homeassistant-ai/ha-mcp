# CLAUDE.md

Guidance for Claude Code when working with this repository.

## Repository Structure

This repository uses a worktree-based development workflow.

**Documentation Setup:**
- This file is `AGENTS.md` (the canonical source)
- `CLAUDE.md` is a symlink pointing to `AGENTS.md`
- Read either file - they're the same content
- Commit changes to `AGENTS.md`, the symlink will automatically reflect them

**Directory Structure:**
```
<repo-root>/                           # Main repository (checkout master here)
├── AGENTS.md                          # This file (canonical source)
├── CLAUDE.md -> AGENTS.md             # Symlink for convenience
├── worktree/                          # Git worktrees (gitignored)
│   ├── issue-42/                      # Feature branch worktree
│   └── fix-something/                 # Fix branch worktree
├── local/                             # Scratch work (gitignored)
└── .claude/skills/                    # Slash-command skills
```

**Quick command:** Use `/wt <branch-name>` skill to create worktree automatically.

## Worktree Workflow

### Creating Worktrees

**ALWAYS create worktrees in the `worktree/` subdirectory**, not at the repository root.

```bash
git worktree add worktree/issue-42 -b issue-42
git worktree add worktree/feat-new-feature -b feat/new-feature
```

**Cleanup:** `git worktree remove worktree/<name>` or `git worktree prune` for stale references.

### Skills

All workflow automation is implemented as skills in `.claude/skills/` and invoked with `/skill-name <args>`:

| Skill | Command | Purpose |
|-------|---------|---------|
| **issue-analysis** | `/issue-analysis <number>` | Deep issue analysis — codebase exploration, implementation planning, architectural assessment. Posts structured comment and applies labels. |
| **issue-to-pr-resolver** | `/issue-to-pr-resolver <number>` | End-to-end issue implementation: worktree creation → implementation with tests → draft PR → iterative CI/review resolution until merge-ready. |
| **my-pr-checker** | `/my-pr-checker <number>` | Review and manage YOUR OWN PRs — check CI, resolve review threads, fix issues, iterate until all checks pass. |
| **contrib-pr-review** | `/contrib-pr-review <number>` | Review external contributor PRs for safety, quality, and readiness. |
| **contributors-update** | `/contributors-update` | Find merged PR authors missing from README and update the contributors list after approval. |
| **wt** | `/wt <branch-name>` | Create git worktree in `worktree/` subdirectory with up-to-date master. |
| **bat-adhoc** | `/bat-adhoc [scenario]` | Ad-hoc bot acceptance testing with dynamically generated scenarios. |
| **bat-story-eval** | `/bat-story-eval --baseline v6.6.1` | Diff-based story evaluation: two-version comparison, regression detection. |

## Project Overview

**Home Assistant MCP Server** - A production MCP server enabling AI assistants to control Home Assistant smart homes. Provides tools for entity control, automations, device management, and more.

- **Repo**: `homeassistant-ai/ha-mcp`
- **Package**: `ha-mcp` on PyPI
- **Python**: 3.13 only

## Security

See [SECURITY.md](SECURITY.md) for the threat model, scope, and reporting
instructions. The threat model section documents the key design decisions that
define what ha-mcp does and doesn't defend against (trusted MCP clients, local
network boundary, OAuth Bearer token design, single-tenant standard mode, HA
permission scope).

**Security advisories:** The API exposes an advisory's body but not its
discussion thread, where the maintainer disposition (dismiss-vs-fix and the
agreed fix scope) lives. Confirm the scope from that thread (GitHub UI or ask a
maintainer) before writing the fix.

## External Documentation

When implementing features or debugging, consult these resources:

| Resource | URL | Use For |
|----------|-----|---------|
| **Home Assistant REST API** | https://developers.home-assistant.io/docs/api/rest | Entity states, services, config |
| **Home Assistant WebSocket API** | https://developers.home-assistant.io/docs/api/websocket | Real-time events, subscriptions |
| **HA Core Source** | `gh api /search/code -f q="... repo:home-assistant/core"` | Undocumented APIs (don't clone) |
| **HA Add-on Development** | https://developers.home-assistant.io/docs/add-ons | Add-on packaging, config.yaml |
| **FastMCP Documentation** | https://gofastmcp.com/getting-started/welcome | MCP server framework |
| **MCP Specification** | https://modelcontextprotocol.io/docs | Protocol details |

## Issue & PR Management

### Automated Code Review

**Codex** reviews PRs automatically (`pr-codex-review-request.yml` /
`pr-codex-review-delivery.yml`; posts as `chatgpt-codex-connector[bot]`).
Gemini Code Assist is retired — Google sunset its GitHub review activities and
the app now only posts sunset-notice banners, so `.gemini/config.yaml`
disables it fully. `.gemini/styleguide.md` remains the repo's review-criteria
document (code quality, test coverage, security patterns, MCP conventions,
safety annotation accuracy): the `@codex review` request comment points Codex
at it explicitly, and the Claude review skills below apply it.

**CodeRabbit** (GitHub app, posts as `coderabbitai[bot]`) reviews drafts too —
`.coderabbit.yaml` sets `reviews.auto_review.drafts: true`, since every PR here
opens as a draft, and `auto_pause_after_reviewed_commits: 0` so it keeps
reviewing every push instead of going quiet after five. That spends the
per-developer hourly review allowance faster; a rate-limited push says so in a
comment and never blocks merge, and CodeRabbit's `rate limit` command reports
whether reviews are available without consuming one. It auto-detects `AGENTS.md` as review criteria;
`.gemini/styleguide.md` is added through
`knowledge_base.code_guidelines.filePatterns` (see the comment there). Repo YAML
outranks the UI settings (only org/workspace Global Overrides beat it) and does
not merge with them — any key it omits falls back to CodeRabbit's schema
defaults, not to UI values. A change to `.coderabbit.yaml` never applies to the
PR making it: on open-source repos CodeRabbit honours only the base branch's
config, so the PR reports `Configuration used: defaults` and the change takes
effect on merge.

**Bot-authored PRs are excluded from automatic review by both tools** —
Dependabot, Renovate, and the `github-actions[bot]` webhook-proxy promote PRs
(dev → stable copies whose content was already reviewed in their dev PRs).
Enforced in `.coderabbit.yaml` `ignore_usernames` and the `pull_request_target`
admission list in `pr-codex-review-request.yml`, pinned to each other by
`test_coderabbit_config.py`. A maintainer can still summon a review on a
promote PR: `@coderabbitai review`, or for Codex a comment that is exactly
`/review` (or `@ghhamcp review`) — the `issue_comment` admission list
deliberately omits `github-actions[bot]` to keep that lever.

**Division of Labor:**
- **Codex (automatic)**: Code quality, test coverage, generic security, MCP conventions
- **CodeRabbit (automatic, drafts included)**: Line-level review against `AGENTS.md` and `.gemini/styleguide.md`, PR walkthrough and summary
- **Claude `/contrib-pr-review` (on-demand)**: Repo-specific security (AGENTS.md, .github/, .claude/), detailed test analysis, PR size assessment, issue linkage
- **Claude `/my-pr-checker` (lifecycle)**: Resolve threads, fix issues, monitor CI, create improvement PRs

### Issue Labels

**Triage-state labels** (applied during manual triage):

| Label | Meaning |
|-------|---------|
| `ready-to-implement` | Clear path, no decisions needed |
| `needs-choices` | Multiple approaches, needs stakeholder input |
| `needs-info` | Awaiting clarification from reporter. `close-needs-info.yml` clocks from the label event: reminders on days 3/5/6, auto-close on day 7 without an author reply; an author reply removes the label |
| `priority: high/medium/low` | Relative priority |
| `triaged` | Automated triage complete (historical — applied by the retired `issue-triage.yml` bot) |
| `triage-failed` | Automated triage failed (historical — applied by the retired `issue-triage.yml` bot) |
| `issue-analyzed` | Deep Claude analysis complete |

**Bug-class labels** (applied via `.github/ISSUE_TEMPLATE/` form selection, CodeRabbit auto-labeling, or manual triage):

| Label | Meaning |
|-------|---------|
| `runtime-bug` | Bug occurring during normal operation (post-startup) |
| `startup-bug` | Bug during startup, install, or connect |
| `agent-behavior` | AI agent behavior or workflow feedback (tool selection, prompt drift, etc.) |

**Scope labels** (manually applied during triage; orthogonal to bug-class — an issue can carry both `runtime-bug` AND a scope marker):

| Label | Meaning |
|-------|---------|
| `addon` | Issue is specific to the Home Assistant Add-on deployment (`homeassistant-addon/`, Supervisor ingress) |
| `docker` | Issue is specific to the Docker / containerized deployment (`Dockerfile`, container env) |
| `javascript` | Issue concerns the project website / Astro app (TypeScript) under `site/` |

**Lifecycle labels** (manually applied; do not double as close-reasons):

| Label | Meaning |
|-------|---------|
| `wontfix` | Issue is valid but will not be addressed. Typically used when closing an issue to record the rejection rationale. |
| `blocked` | Forward progress depends on an unresolved external item (upstream HA change, a sibling PR, a pending design decision). Recorded so a sweeper search can find what's waiting |

**Tracking / automation labels** (applied by tooling):

| Label | Meaning |
|-------|---------|
| `python-upgrade` | Auto-attached to every Renovate-managed PR (including non-Python dependency updates) via `renovate.json` global `labels` array. |

### Issue Analysis Workflow

- **Automated Triage (CodeRabbit)**: `issue_enrichment` in `.coderabbit.yaml`. On new and edited issues CodeRabbit posts an enrichment comment (possible duplicates, related issues and PRs, suggested assignees) and auto-applies labels per `labeling_instructions`. Plans are manual: comment `@coderabbitai plan` on an issue, or tick the Create Plan checkbox in the enrichment comment. (Replaces the retired GitHub Models `issue-triage.yml` bot.)
- **Deep Analysis (Claude)**: When user says "analyze issues", list issues missing `issue-analyzed` label, then invoke `/issue-analysis <number>` for each sequentially (the skill drafts analysis for user approval before posting).

```bash
gh issue list --state open --json number,title,labels --jq '.[] | select(.labels | map(.name) | contains(["issue-analyzed"]) | not) | "#\(.number): \(.title)"'
```

### PR Review Comments

**Always check for comments after pushing to a PR.** They come from bots
(Codex, CodeRabbit, Copilot) or humans. Address human comments with highest
priority; treat bot comments as suggestions to assess, not commands.

**Reply, then resolve.** After addressing an inline comment, reply on its
thread documenting the fix, then mark the thread resolved. When a review has
inline comments, do both: reply per-thread *and* post one PR-level summary
comment. Leave a thread open only when the reply asks the reviewer for
clarification. Unresolved threads block merge even after approval: the merge
button stays disabled until every thread is resolved.

The `/my-pr-checker` skill carries the exact commands (the inline-reply
`pulls/<PR>/comments/<id>/replies` endpoint, the PR-level review, and the
`resolveReviewThread` GraphQL mutation, whose input field is `threadId`, not
`pullRequestReviewThreadId`).

## Git & PR Policies

**CRITICAL - Never commit directly to master, except for documentation-only adjustments.**

You are STRICTLY PROHIBITED from committing to `master` or `main` branch. Always use worktrees for feature work:

```bash
# Use /wt skill or manually:
git worktree add worktree/<branch-name> -b <branch-name>
cd worktree/<branch-name>
```

**Before any commit, verify:**
1. Current branch: `git rev-parse --abbrev-ref HEAD` (must NOT be master/main)
2. In worktree: `pwd` (must be in `worktree/` subdirectory)

**Never push or create PRs without user permission.**

**Always create PRs as draft.** Use `gh pr create --draft`. Only mark a PR as ready for review (`gh pr ready <PR>`) when explicitly requested by the user. **Before marking ready, update the PR description** to reflect all changes made since the PR was created.

### PR Workflow

**After creating or updating a PR, always follow this workflow:**

1. **Update tests if needed**
2. **Commit and push**
3. **Wait for CI** (~3 min for tests to start and complete):
   ```bash
   sleep 180
   ```
4. **Check CI status**:
   ```bash
   gh pr checks <PR>
   ```
5. **Check for review comments** (see "PR Review Comments" section above)
6. **Fix any failures**:
   ```bash
   # View failed run logs
   gh run view <run-id> --log-failed

   # Or find the run ID from PR
   gh pr checks <PR> --json | jq '.[] | select(.conclusion == "failure") | .detailsUrl'
   ```
7. **Address review comments** if any (prioritize human comments)
8. **Update PR description** if the scope changed (only when PR is already marked as ready)
9. **Repeat steps 2-8 until:**
   - ✅ All CI checks green
   - ✅ All comments addressed
   - ✅ PR ready for merge

### PR Execution Philosophy

**Work autonomously during PR implementation:**
- Don't ask the user about every small choice or decision during implementation
- Make reasonable technical decisions based on codebase patterns and best practices
- Fix unrelated test failures encountered during CI (even if time-consuming)
- Document choices for final summary

**Making implementation choices:**
- **DO NOT** choose based on what's faster to implement
- **DO** consider long-term codebase health - refactoring that benefits maintainability is valid
- **For non-obvious choices with consequences**: Create 2 mutually exclusive PRs (one for each approach) and let user choose
- **For obvious choices**: Implement and document in final summary

**When you notice an improvement during a PR**: fix it in place by default. See [Boy Scout Rule — Handling Discovered Improvements](#boy-scout-rule--handling-discovered-improvements) below for the deferral scale.

**Final reporting:** Once the PR is ready, post an Implementation Summary comment on the PR (choices made, problems encountered) and give the user a short summary.

### Boy Scout Rule — Handling Discovered Improvements

**IMPORTANT — Default is fix-in-place.** "Boy Scout Rule" means leave touched code better than you found it. "Improve incrementally" means commit-by-commit within *this* PR — not across follow-up PRs. Deferral is the exception, not the default. Weigh fix-in-place sweeps against regression risk: if a sweep would meaningfully expand the diff or change the review surface, treat it as Mid-sized and ask the user.

**Never open a follow-up PR or issue without explicit user approval.**

When you notice something while working on a PR, apply this scale:

| What you find | Action |
|---|---|
| **Small** — a few lines, clearly in scope (see examples below) | **Fix in this PR** as a separate commit. No mention in PR description. |
| **Mid-sized** — meaningful effort, worth doing but out of scope (e.g. adding a new helper module that doesn't exist yet, a gap that needs non-trivial new test scaffolding, a code-quality issue that's *not* really low) | **Pause before pushing.** Ask the user whether to bundle. |
| **Large / unrelated** — many files, design decisions, different subsystem (e.g. would double the diff size or change the review surface, code quality is *really* low / technical debt) | Mention in PR description only if the user confirms. Open a separate issue **only if** the user asks AND you can state a concrete benefit in one sentence. |

**"Small" examples — fix these inline, no mention needed:**

- Typo, dead import, misnamed local
- Stale docstring/comment or stale reference
- 1–N line cleanup of code in this diff
- Multi-site sweep of the same pattern you can grep for
- Missing test for code you're touching (add the test without refactoring the surrounding code)
- Low coverage for the area you're working in
- Straightforward test-quality fix (better assertions, clearer names, removing duplication)
- "Mirror X parity onto Y" where Y is in the diff
- Migrating a singular→list or similar shape-consistency fix
- Drift between docs and live state you can fix by reading both

**When to ask the user about bundling.** ~200 lines is a *should-I-ask* heuristic, not a bundling cap. Under ~200 lines: bundle without asking. Over ~200 lines: ask the user whether to bundle — but **bundling at any size is fine if the work is not grossly out of scope**. The 200-line mark exists so the user hears about large bundled changes before they land, not to push large work out of the PR. Estimate honestly; do not inflate to manufacture a reason to defer.

**Anti-noise gate — before filing any follow-up issue or PR, all three must be true:**

1. The work is genuinely too large to bundle (i.e. truly out of scope, not just over the ~200-line ask-heuristic above). **All three sub-tests must pass:**
   (a) It cannot be done by mirroring an existing sibling pattern in the same file or a closely-related file.
   (b) You can name the actual design choice in one sentence with two named alternatives, **OR** the work is a genuinely large mechanical migration (e.g. *"replace `requests` with `httpx` across 40 sites"*) that exceeds this PR's scope by size alone.
   (c) It would meaningfully change this PR's review surface, not just add to it.
2. You can name a concrete end-user-facing or maintainer benefit in one sentence.
3. A maintainer reading the issue 6 months later would act on it, not close as stale.

If any are false: fix it now, or let it go. **Do not file an issue to "track" it.**

**Scope is the user's call, not yours.** Before deferring anything, explicitly ask with a specific reason: *"I think this is out of scope because [X]. Fix here or defer?"* — do not silently drop it.

The following phrases are red flags that you're making a scope decision unilaterally (list is non-exhaustive — match on intent, not exact string): "post-merge follow-up", "follow-up consideration", "forward-looking note", "nice to have", "Happy to file an issue", "out of scope for this PR", "not blocking this PR", "pre-existing — not touching it" (pre-existing is not a reason to skip; addressing pre-existing things is the point of this rule), "real design work, not N lines", "worth tracking as a follow-up issue".

**Code-review bot suggestions** (Codex, CodeRabbit, Copilot non-blocking nits): apply inline or dismiss. Never spawn a follow-up issue from a bot suggestion unless the user explicitly confirms it's a large, out-of-scope change. See `.gemini/styleguide.md` § *Non-Blocking Suggestions and Scope* for the bot-side rule.

### Hotfix Process (Critical Bugs Only)

Hotfix = critical production bug in current stable release. Regular fix = bug after latest stable, or non-critical.

**Hotfix branches MUST be based on `stable` tag.** Always verify the buggy code exists in stable first — if not, use `git checkout -b fix/description master` instead.

```bash
git fetch --tags --force
git show stable:path/to/file.py | grep "buggy_code"  # verify code exists in stable
git checkout -b hotfix/description stable
# fix, commit, then:
gh pr create --draft --base master
```

On merge, `hotfix-release.yml` runs semantic-release, creates GitHub release, syncs CHANGELOG to addon, updates `stable` tag (after changelog sync), and builds binaries.

### Test Coverage Requirements

**When tests ARE required:**
- New MCP tools in `src/ha_mcp/tools/` without any E2E tests
- Tools that previously had NO tests — add E2E tests even if not part of current PR
- Core functionality changes in `client/`, `server.py`, or `errors.py` without coverage
- Bug fixes — use TDD: write the failing regression test first, then fix the code so the test passes

**When tests may NOT be required:**
- Refactoring with existing comprehensive test coverage
- Documentation-only changes (`*.md` files)
- Minor parameter additions to well-tested tools
- Internal utilities already covered by E2E tests

**When to open an issue instead:** See § *Boy Scout Rule — Handling Discovered Improvements* for the gate. Never open without explicit user approval.

## CI/CD Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `pr.yml` | PR opened | Lint, type check |
| `e2e-tests.yml` | PR to master | Full E2E tests (~3 min) |
| `publish-dev.yml` | Push to master | Dev release `.devN` |
| `notify-dev-channel.yml` | Push to master (src/) | Comment on PRs/issues with dev testing instructions |
| `semver-release.yml` | Biweekly Wed 10:00 UTC | Stable release (cuts version tag + GitHub release) |
| `release-publish.yml` | After SemVer Release (`workflow_run`) or manual dispatch | Publish stable Docker image (`:latest` + `:stable` + semver) + MCP registry |
| `hotfix-release.yml` | Hotfix PR merged | Immediate patch release |
| `build-binary.yml` | Release | Linux/macOS/Windows binaries |
| `addon-publish.yml` | Release | HA add-on update |
| `sync-tool-docs.yml` | Push to master (`src/ha_mcp/tools/`, `scripts/extract_tools.py`) | Regenerate `tools.json`, README, DOCS.md |
| `locale-sync.yml` | Daily schedule + manual dispatch | Machine-translate stale/missing strings post-merge and push them straight to master |

**Docker image tags** (`ghcr.io/homeassistant-ai/ha-mcp`): stable releases push `:latest` + `:stable` + semver tags (`release-publish.yml`); dev builds push only `:dev` + `:dev-<sha>` (`publish-dev.yml`) — **never `:latest`**, which is reserved for stable. The HA add-on images live in separate repos (`-addon-{arch}`, `-addon-dev-{arch}`) and are selected by an explicit `version:` pin, not by `:latest`.

## Development Commands

### Setup
```bash
uv sync --group dev        # Install with dev dependencies
uv run ha-mcp              # Run MCP server (stdio; needs interactive stdin)
uv run ha-mcp-web          # Run HTTP server; web settings UI at http://localhost:8086/mcp/settings (see src/ha_mcp/settings_ui/AGENTS.md)
cp .env.example .env       # Configure HA connection
```

### Testing
E2E tests are in `tests/src/e2e/` (not `tests/e2e/`). Tests use **testcontainers** to spin up
an isolated Docker HA instance — Docker daemon must be running.

```bash
# Run FULL E2E suite (required before claiming all tests pass)
# -n2 is optimal locally (each worker spins up its own HA container;
# more workers add memory pressure without proportional speedup).
# CI uses -n3 tuned for 2-vCPU GitHub runners with 15GB RAM.
cd tests && uv run pytest src/e2e/ -n2 --dist loadscope -v --tb=short

# Run specific file (partial coverage only — never substitute for full suite)
cd tests && uv run pytest src/e2e/workflows/automation/test_lifecycle.py -v

# Interactive test environment
uv run hamcp-test-env                    # Interactive mode
uv run hamcp-test-env --no-interactive   # For automation
```

**CRITICAL RULES:**
- Always run from the `tests/` directory so pytest picks up the correct `conftest.py`
- Always run the **full suite** before declaring tests pass
- `tests/.env.test` contains placeholder values only; testcontainers sets the real URL dynamically
- Never set `HOMEASSISTANT_URL` manually in your shell before running tests
- **Always run relevant e2e tests after making changes**, without waiting to be asked. Identify the relevant test file(s) for the area you changed and run them. Do not assume Docker is unavailable or prerequisites are missing — just run them and let pytest report what is skipped and why.

Test token centralized in `tests/test_constants.py`.

### Code Quality

C901 (mccabe complexity ≤10) is enforced repo-wide with zero per-file exemptions (issue #925 cleared the grandfathered list) — never reintroduce a `["C901"]` per-file-ignore; extract helpers instead.

```bash
uv run ruff check src/ tests/ --fix
# Note: --fix removes unused imports from non-__init__ modules (lefthook runs it on commit with
# stage_fixed). When adding an import, include its first use in the same change or it gets stripped.
uv run mypy src/
```

### Docker
```bash
# Stdio mode (Claude Desktop) — local-only, no network exposure
docker run --rm -i \
  -e HOMEASSISTANT_URL=... -e HOMEASSISTANT_TOKEN=... \
  ghcr.io/homeassistant-ai/ha-mcp:latest

# HTTP mode (loopback only, same-host LLM client)
# Connect URL: http://127.0.0.1:8086/mcp  (default MCP_SECRET_PATH)
docker run -d -p 127.0.0.1:8086:8086 \
  -e HOMEASSISTANT_URL=... -e HOMEASSISTANT_TOKEN=... \
  ghcr.io/homeassistant-ai/ha-mcp:latest ha-mcp-web

# HTTP mode (LAN-reachable) — generate the secret first so you can configure the MCP client with it
MCP_SECRET="/private_$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')"
echo "MCP_SECRET_PATH=$MCP_SECRET"
docker run -d -p 8086:8086 \
  -e HOMEASSISTANT_URL=... -e HOMEASSISTANT_TOKEN=... \
  -e MCP_SECRET_PATH="$MCP_SECRET" \
  ghcr.io/homeassistant-ai/ha-mcp:latest ha-mcp-web
```

See [SECURITY.md](SECURITY.md) for authentication and network binding details.

## Architecture

```
src/ha_mcp/
├── server.py          # Main server with FastMCP
├── __main__.py        # Entrypoint (CLI handlers)
├── config.py          # Pydantic settings management
├── errors.py          # 38 structured error codes
├── client/
│   ├── rest_client.py       # HTTP REST API client
│   ├── websocket_client.py  # Real-time state monitoring
│   └── websocket_listener.py
├── auth/
│   ├── provider.py          # OAuth provider (HTTP mode)
│   └── consent_form.py      # OAuth consent screen
├── tools/             # 36 modules, auto-discovered
│   ├── registry.py          # Lazy auto-discovery
│   ├── smart_search/        # Fuzzy entity search
│   ├── device_control.py    # WebSocket-verified control
│   ├── best_practice_checker.py # Reactive HA config validator (warns + embeds skill content)
│   ├── tools_*.py           # Domain-specific tools
│   └── util_helpers.py      # Shared utilities
├── utils/
│   ├── fuzzy_search.py      # textdistance-based matching
│   ├── domain_handlers.py   # HA domain logic
│   ├── operation_manager.py # Async operation tracking
│   ├── skill_loader.py      # Skills-vendor file loader (used by ha_get_skill_guide and write tools)
│   ├── usage_logger.py      # Per-tool usage telemetry
│   ├── data_paths.py        # Canonical data directory paths
│   ├── python_sandbox.py    # Sandboxed Python-expression eval for python_transform on config tools
│   ├── kill_signal_diagnostics.py # Kill-signal (SIGTERM/SIGINT/SIGHUP) shutdown diagnostics
│   └── config_hash.py       # Shared optimistic-locking hash (automation/script/scene/dashboard/energy)
└── resources/
    ├── card_types.json
    └── dashboard_guide.md
```

### Key Patterns

**Tools Registry**: Auto-discovers `tools_*.py` modules with `register_*_tools()` functions. No changes needed when adding new modules.

**Lazy Initialization**: Server, client, and tools created on-demand for fast startup.

**Service Layer**: Business logic in `smart_search/`, `device_control.py` separate from tool modules.

**WebSocket Verification**: Device operations verified via real-time state changes.

**Tool Completion Semantics**: Tools should wait for operations to complete before returning, with optional `wait` parameter for control.

## Writing MCP Tools

### Naming Convention
`ha_<verb>_<noun>`:
- `get` — single item (`ha_get_state`)
- `list` — collections (`ha_list_services`)
- `search` — filtered queries (`ha_search`)
- `set` — create/update (`ha_config_set_helper`)
- `delete` — delete dashboards, config entries, or files (`ha_config_delete_dashboard`, `ha_delete_file`)
- `remove` — remove registry items (`ha_remove_entity`, `ha_remove_area_or_floor`)
- `call` — execute (`ha_call_service`, `ha_call_event`)
- `manage` — multi-modal tools combining several operations behind one interface (`ha_manage_addon`)

**Namespace prefixes**: An optional `<namespace>_` prefix between `ha_` and the verb is allowed for grouped tool families that share a domain. The full shape becomes `ha_<namespace>_<verb>_<noun>`:
- `ha_config_<verb>_<noun>` — config-management tools (`ha_config_set_helper`, `ha_config_set_automation`, `ha_config_remove_automation`, `ha_config_delete_dashboard`)
- `ha_dev_<verb>_<noun>` — developer-mode tools (`ha_dev_manage_server`, `ha_dev_manage_settings`); registered only when the `enable_dev_mode` setting is on (Developer section at the bottom of the web settings UI's Server Settings tab)

**Accepted exceptions**: A small set of tools name a single, distinct operation where forcing a `<verb>_<noun>` shape would read worse than the natural name. These are accepted as-is and should not be flagged:
- `ha_restart`, `ha_reload_core`, `ha_eval_template`
- `ha_report_issue`, `ha_import_blueprint`
- `ha_read_file`, `ha_write_file`, `ha_bulk_control`

**Adding new verbs**: When no existing verb fits a new tool's purpose, add the verb to the approved-verbs list above rather than forcing a poor fit. `.gemini/styleguide.md` points back to this section as the single source of truth, so updates here propagate automatically.

### Tool Structure
Create `tools_<domain>.py` in `src/ha_mcp/tools/`. Registry auto-discovers it.

```python
from fastmcp.tools import tool
from .helpers import log_tool_usage, register_tool_methods

class DomainTools:
    def __init__(self, client):
        self._client = client

    @tool(name="ha_<verb>_<noun>", tags={"Category Name"}, annotations={"readOnlyHint": True, "idempotentHint": True})
    @log_tool_usage
    async def ha_<verb>_<noun>(self, param: str) -> dict[str, Any]:
        """<Action verb> <what this tool does -- one sentence>.

        <Optional: second sentence for key behavioral distinction or modes>
        """
        # Add to the docstring above only when genuinely needed:
        # RELATED TOOLS: ha_next(): why to call this after (workflow-entry tools only)
        # EXAMPLES: ha_<verb>_<noun>("realistic_value")  -- non-obvious call patterns only
        # When NOT to use: route to preferred alternatives
        # Caveats: destructive side-effects, non-obvious gotchas
        # For complex schemas: use ha_get_skill_guide

def register_<domain>_tools(mcp, client, **kwargs):
    register_tool_methods(mcp, DomainTools(client))
```

`@tool` (from `fastmcp.tools`) attaches metadata to the method. `@tool` must be the outermost decorator (above `@log_tool_usage`) so that `__fastmcp__` is present on the final method object. `register_tool_methods()` auto-discovers all `@tool`-decorated methods and calls `mcp.add_tool()` for each. The registry discovers `register_*_tools` functions by convention.

### Tool Docstrings

The single-line template is the default -- extend it only where it genuinely helps.

**Required for every tool:**
- Starts with an action verb (`Get`, `List`, `Search`, `Create`, `Update`, `Delete`, `Remove`, `Execute`, `Call`, `Manage`)
- One sentence describing what the tool does (not how)

**Add `RELATED TOOLS` when** the tool is a workflow entry point and the natural next step is not obvious.
Example: `ha_search` hints at `ha_get_state`.

**Add `EXAMPLES` when** the tool has multiple modes or non-obvious parameters.
Omit when a single required parameter makes the call self-evident.

**For multi-line docstrings, follow this structure** (based on
[Anthropic's tool design guidance](https://www.anthropic.com/engineering/writing-tools-for-agents)):
1. What the tool does (required first sentence, action verb)
2. When NOT to use it — name the preferred alternatives
3. When to use it — valid use cases
4. Caveats — consequences, post-actions, destructive side-effects

Consequence statements are plain prose: "This permanently deletes the dashboard.
A backup is created before every edit." Route safety concerns through `annotations`
(`destructiveHint`, `idempotentHint`, `readOnlyHint`), not docstring keywords.

**Defer complex schemas** instead of embedding them:
`# For complex schemas: use ha_get_skill_guide`

**What NOT to include:** full parameter documentation, type descriptions already in the
signature, HA domain internals the model already knows, or motivational prose.


### Tool Tags

Every tool needs `tags={"Category Name"}` (native FastMCP parameter). Drives the README table, `site/src/data/tools.json`, and `homeassistant-addon/DOCS.md`. These are auto-regenerated on merge by `sync-tool-docs.yml` — no manual regeneration needed. For local testing: `python scripts/extract_tools.py`

### Safety Annotations
| Annotation | Default | Use For |
|------------|---------|--------|
| `readOnlyHint: True` | `False` | Tool does not modify its environment |
| `destructiveHint: True` | `True` | Tool may perform destructive updates (only meaningful when `readOnlyHint` is false). Set to `False` for non-destructive writes (e.g., creating a record) |
| `idempotentHint: True` | `False` | Repeated calls with same args have no additional effect (only meaningful when `readOnlyHint` is false) |
| `openWorldHint: True` | `True` | Tool reaches an external, third-party-authored world (HACS store, add-on repositories, GitHub release feeds, arbitrary import URLs). Set to `False` when the tool's domain is the local Home Assistant instance. A tool is also open-world if its output carries externally-authored content back to the client, even when a local integration (HACS, Supervisor, HA Core) makes the actual network call on its behalf — `ha_get_overview` and `ha_get_system_health` embed the update-check field that reaches PyPI / the Supervisor store, while `ha_get_blueprint` and `ha_config_list_dashboard_resources` return externally-authored content from purely local reads. Required on every tool — the default is `true`, so an omitted value silently marks a local tool as open-world |

**Version baseline:** annotations describe a tool's behavior against current
upstream versions of any external engine or component it drives; a side effect
that exists only in outdated external builds does not demote the tool to
write-classified — document the update requirement in the tool's docs instead
(e.g. the screenshot engine's old `settheme` write, #1991).

### Error Handling

**Always use the dedicated error functions** from `errors.py` and `helpers.py`. Never construct raw error dicts manually — the helpers ensure consistent structure, error codes, and suggestions across all tools.

**All tool-level failures must raise `ToolError`** (sets `isError=true` per MCP spec). Batch item failures within result arrays are the only exception — those return structured dicts without raising.

**Pattern A — Exception blocks** (most common): call `exception_to_structured_error` without `return` — it raises `ToolError` by default:
```python
from .helpers import exception_to_structured_error, raise_tool_error
from fastmcp.exceptions import ToolError

try:
    # ... tool logic ...
except ToolError:
    raise  # must re-raise; prevents ToolError being swallowed by outer except
except Exception as e:
    exception_to_structured_error(
        e,
        context={"entity_id": entity_id},
        suggestions=["Verify entity exists", "Check HA connection"],
    )
```

The `except ToolError: raise` guard is required whenever `raise_tool_error()` or validation errors are called inside the same `try` block — without it, `except Exception` catches the `ToolError` and re-maps it to `INTERNAL_ERROR`.

**Pattern B — Input validation errors**: use `raise_tool_error(create_error_response(ErrorCode.VALIDATION_INVALID_PARAMETER, message, context={...}, suggestions=[...]))`.

**Pattern C — Service call failures**: check `result.get("success")` and raise with `ErrorCode.SERVICE_CALL_FAILED` using `result.get("error", "Operation failed")` as the message.

**Pattern D — Batch item failures** (items inside a results list — do NOT raise):
```python
results.append(create_error_response(
    ErrorCode.SERVICE_CALL_FAILED,
    str(e),
    context={"entity_id": eid},
))
```

Only use `raise_error=False` on `exception_to_structured_error` when you need to mutate the dict before raising. Never add `add_timezone_metadata` to errors.

`exception_to_structured_error` auto-classifies 404s, auth errors, timeouts by exception type. Pass `context={"entity_id": ...}` for automatic `ENTITY_NOT_FOUND` on 404s. Available helpers: `create_entity_not_found_error`, `create_connection_error`, `create_auth_error`, `create_service_error`, `create_validation_error`, `create_config_error`, `create_timeout_error`, `create_error_response`.

### Return Values
```python
{"success": True, "data": result}                     # Success
{"success": True, "data": result, "warnings": [...]}  # Degraded (top-level list[str], omit when empty)
raise ToolError(json.dumps({...}))                    # Tool-level failure (isError=true)
{"success": False, "error": {...}}                    # Batch item failure only (in results list)
```

`warnings` is always a top-level `list[str]`, never nested inside `data` and never a singular `"warning": "..."` string. See `tools_config_helpers.py::HelperResponse` / `_helper_response` for the canonical shape and `tests/src/unit/test_helper_response_shape.py` for the contract assertions.

### Tool Consolidation
When a tool's functionality is fully covered by another tool, **remove** the redundant tool rather than deprecating it. Fewer tools reduces cognitive load for AI agents and improves decision-making. Do not add deprecation notices or shims — just delete the tool and update any docstring references to point to the replacement.

This project's tool count exceeds the [10-20 tool threshold](https://ai.google.dev/gemini-api/docs/function-calling) where selection accuracy degrades. Reducing count is a priority — combine frequently chained operations into one tool and ensure each tool has a clear, distinct purpose. See [Anthropic's tool design blog](https://www.anthropic.com/engineering/writing-tools-for-agents) for guidance.

| Pattern | Example | Guideline |
|---------|---------|-----------|
| Tool A is a strict subset of Tool B | `ha_dashboard_find_card` fully covered by `ha_config_get_dashboard` | Consolidate (remove A) |
| Frequently chained operations | Multi-step workflows combined into one tool | Consolidate — reduces round-trips |

**Breaking changes**: only removing functionality with no alternative requires a major bump. Consolidation and renaming are not breaking.

**Context engineering**: provide minimum context; let models fetch more via `ha_get_skill_guide`. Favor statelessness and content-derived hashes for optimistic locking.

### Module Size

Keep modules focused. Past ~1000 lines (Pylint's `max-module-lines` default) a module usually spans multiple concerns and is worth splitting along those concerns. Pick whatever decomposition fits and fix the internal imports (and any test patch targets) that reference the moved code as part of the move. There's no external import contract to preserve: it's one project, and MCP tools are resolved dynamically at runtime by name (and renaming isn't breaking either, see Tool Consolidation).

## Tool Waiting Behavior

**Principle**: MCP tools should wait for operations to complete before returning, not just acknowledge API success.

Tools have an optional `wait` parameter (default `True`) that polls for completion. Use `wait=False` for bulk operations, then batch-verify. Categories:
- **Config ops** (automations, helpers, scripts): Wait by default (poll until entity queryable/removed)
- **Service calls** (lights, switches): Wait for state change on state-changing services (turn_on, turn_off, toggle, etc.)
- **Async ops** (automation triggers, external integrations): Return immediately (not state-changing)
- **Query ops** (get_state, search): Return immediately (no `wait` parameter)

**Shared utilities** in `src/ha_mcp/tools/util_helpers.py`:
- `wait_for_entity_registered(client, entity_id)` — polls until entity accessible via state API
- `wait_for_entity_removed(client, entity_id)` — polls until entity no longer accessible
- `wait_for_state_change(client, entity_id, expected_state)` — polls until state changes

## Custom Component

The `custom_components/ha_mcp_tools/` integration ships separately from the
`ha-mcp` server package (it reaches the HA instance via HACS), so CI cannot
fully validate a component change before merge.

- **Version bumps ride the stable release cycle — do not bump per PR or per
  push.** The component version (`manifest.json` `version` + `COMPONENT_VERSION`
  in `const.py`, kept in lockstep by the parity test) should lead the last
  **stable** release by exactly one pending version, so everything merged since
  the last stable cut ships together under one number on the next stable
  release. Check the pending state with `git show
  stable:custom_components/ha_mcp_tools/const.py | grep COMPONENT_VERSION` vs
  master, then:
  - **Level with stable** (no pending version yet): bump once — patch by
    default — to open the pending version.
  - **Already ahead of stable** (a pending version exists): do **not** bump;
    your change rides under the existing pending version.
  - Raise the pending version further only to **escalate the bump level** — e.g.
    the pending version is a patch but your change warrants a minor — and then
    go straight to that minor, not an extra patch. Never go past the current
    pending version otherwise; per-revision bumps skip never-shipped numbers and
    desync the version from the release cycle.
  - CI enforces the level-with-stable case twice: the PR-level **Component
    Version Gate** fails a component change whose manifest version does not
    strictly lead the mirror's released stable (equal = bump to open the
    pending version; behind = a stale tree or bad merge resurrected an old
    version), and the mirror sync's stable tag step fails loud
    when an already-tagged version's component content has drifted (changes
    merged onto a shipped version would otherwise strand with no installable
    release — the gap is a PR opened while a version is pending that merges
    only after that version goes stable, which re-runs no PR checks).
- **When the change adds a service or argument the server depends on**, this PR
  must **open a fresh pending component version** (bump `manifest.json` +
  `COMPONENT_VERSION`) and raise `MIN_COMPONENT_VERSION` in
  `src/ha_mcp/tools/tools_filesystem.py` to that same new version. This is the
  one case that **overrides** the "already ahead of stable → do not bump" rule
  above: bump here even if a pending version already exists. `get_caller_token`
  reports the manifest version and the server gates on it, so without the gate
  the old and new component are indistinguishable: a caller on the old version
  passes the check and then hits raw "service not found" errors instead of an
  actionable "update" prompt. **Never floor at a version any build lacking the
  behaviour also reports** – an already-shipped version, or a pending version
  that was opened *before* this behaviour landed. Such a build passes the gate
  yet lacks the behaviour, defeating the gate (#1946: the floor was set to a
  1.1.0 that had already shipped without the gated behaviours, so 1.1.0 builds
  split into with/without and the gate could not tell them apart).
- **Keep the component backward-compatible with the released server.** The
  component (HACS) and the server (add-on / PyPI / Docker) follow the same
  release cycle but are updated independently per install, so a new component
  can run against an *older* server. Never remove or tighten an existing service
  schema (e.g. dropping a param from a strict `vol.Schema`) without a shim the
  prior server still satisfies; the version gate can't protect this direction
  (the old server is the caller). Remove the shim once the matching
  `MIN_COMPONENT_VERSION` server is the floor.
- **Live-test on the dev server immediately after merge**, before the next
  stable cut. The component path cannot be fully exercised by CI pre-merge.

## Translations

**One canonical store, generated projections, automated retranslation**
(issue #2083). The settings UI catalogs
(`src/ha_mcp/settings_ui/locales/<code>.json`) are the canonical store for
every string except the component's config flow: the add-on option strings
live there under `addon.<key>.*` (plus `features.<key>.*` for options the
settings UI also shows, and `addon_stable.<key>.*` for a stable-flavor
wording deviation). Both add-on flavors' `translations/*.yaml` and the
`FEATURE_META` block in `settings.js` are **generated** from that store by
`scripts/generate_locales.py` — never edit them by hand;
`test_derived_catalogs_match_the_canonical_store` fails until you regenerate.
Each flavor's key list is its own `config.yaml` `schema:`, so the two YAMLs
are different projections of the one store, and cross-surface wording
identity holds by construction.

A language ships on all four surfaces or not at all —
`tests/src/unit/test_locale_parity.py` enforces it. One Home Assistant language
code (`de`, `es`, `fr`, `it`, `nl`, `pl`, `ru`, `sv`, `zh-Hans`) names every file:
`src/ha_mcp/settings_ui/locales/<code>.json`,
`custom_components/ha_mcp_tools/translations/<code>.json`, and
`homeassistant-addon{,-dev}/translations/<code>.yaml`.
That list of codes is itself pinned by
`test_agents_md_lists_every_shipped_locale`: adding a language means adding its
code here, in the same PR, or the suite goes red. To add a language, add the
two authored catalogs (settings UI + component), regenerate, and let the
translation pipeline below fill the strings. The component catalog may start
empty; the settings one may not start `meta`-only, because four ungated checks
read the shipped catalogs themselves: every decided `Decision` outcome and
every `PredicateOp` operator needs a translated word
(`policies.pending.decision.*`, `policies.operators.*` — a value that still
spells the backend literal counts as untranslated), so does
`policies.pending.already_decided`, the sentence those words are interpolated
into, and at least one translated key must have English that addresses the
reader in the second person, which is where `scripts/translate_locales.py`
reads the catalog's address register.
`policies.operators.exists_long` is the trap in that list: the condition editor
renders it as its own dropdown label, but it is UI-only rather than a
`PredicateOp` member, so no enum-derived check asks for it and a catalog
without it reads English there until the sync fills it. Each surface reads that
register from its own catalog, so a component catalog left at a key or two
rests on whichever of them addresses the reader — losing it costs the engine
the register for every later string of that language and says so only on
stderr, which is why
`test_every_shipped_component_catalog_gets_reader_addressing_samples` pins it.
`src/ha_mcp/settings_ui/locales/README.md` names the tests — including the one
that skips locally until `tests/js/` has its npm dependencies.

Settings UI catalogs are auto-discovered (no registration). Their `messages` may
omit keys — English is the per-key fallback — but may not carry one `en.json`
lacks: nothing renders it. `tool_groups` and `tools` may do neither: each locale
must carry exactly the renderable group headings and every tool name, no key
more and none fewer. The check derives the tool set from
the sources (`scripts/extract_tools.py`), not from the committed
`site/src/data/tools.json` — the check must not depend on a generated
artifact that a separate post-merge workflow keeps current. Separately from
those key rules, both authored surfaces cap how much *text* a catalog
may leave byte-identical to English or omit outright, so a stub cannot ride the
fallbacks: 5% for the settings UI `messages`, its `tools` titles and
descriptions, and each generated add-on projection (per flavor, computed from
the canonical store), and 15% for the component catalogs,
which carry the product names as keys of their own. On top of that share, a
`tools` entry whose title *and* description are *both* byte-identical to English
fails by name however small its share — for feature-gated tools against either
English rendering, the `FEATURE_GATED_TOOLS` stub or the parsed docstring.
Component catalogs need every `strings.json` key with identical
`{placeholders}`.

**Changing an English string is a one-place edit** (`en.json` `messages`, a
tool docstring, or `strings.json` + component `en.json`), and the machine
translates the rest: `scripts/translate_locales.py` reads the English-source
baseline diff (`tests/src/unit/locale_source_baseline.json`), retranslates
the changed or missing keys in every language via the Gemini API
(`GEMINI_API_KEY`; free tier), validates placeholders and markup, regenerates
the derived catalogs, and repins the baseline. The `locale-sync.yml` workflow
runs it AFTER merge, on a daily schedule, and pushes the result straight to
master with the release App credential (the same pattern as the version-bump
bots and `sync-tool-docs.yml`) — so any PR, fork or same-repo, merges
without owing translations, and one sync run picks up everything merged
since the last one. The checks that police translated content (missing or
orphaned keys, staleness against the baseline, cross-surface shared wording,
the untranslated-share ceilings, filled tool sections) are gated behind
`LOCALE_COMPLETENESS_CHECKS=1` and run in that workflow, not in PR CI —
`test_locale_sync_gate_shape.py` pins the wiring. What a PR still owes is
deterministic and engine-free: regenerate the derived catalogs
(`python scripts/generate_locales.py`) when a canonical English string
changes, and placeholder parity on component keys whose English is current.
To choose the wording yourself, translate in your own PR **and run
`python scripts/update_locale_baseline.py` in it** — the repinned baseline
is what tells the next sync your wording already covers the changed English
(hand-edits win); without the repin the sync retranslates the key and
overwrites you. Run `scripts/translate_locales.py` locally instead to
machine-fill in-PR or to use a different engine (it repins for you).
The baseline pins the English each translation was written against, because
key parity cannot see a string whose meaning changed: #1993 flipped a policy
string from ALL-match to ANY-match and left the Chinese text asserting the
opposite. `python scripts/update_locale_baseline.py` repins it manually after
a hand-translation pass.

**A tool docstring is one of those English strings.** `en.json` ships `tools`
empty, so the English a `tools` entry translates is read from the tool
definition in `src/ha_mcp/tools/` — the `title=` kwarg and the summary
paragraph of the docstring, or the `FEATURE_GATED_TOOLS` stub where a gated
tool shows one instead. Editing that summary moves the English out from under
six catalogs; the pipeline retranslates them. One deliberate exception: a
change to a feature-gated tool's PARSED docstring (its stub unchanged) is
stub-review work, not translation work — the pipeline holds that baseline key
stale, and the locale-sync run stays red until a human confirms the stub
still describes the tool and runs `python scripts/update_locale_baseline.py`.

**Rate limits and outages degrade loudly, never silently.** Engine calls are
paced under the free-tier request rate and retry transient errors (429/5xx,
timeouts) with backoff; a request that keeps failing marks its strings failed
and the run continues, and two consecutive dead batches stop the run early
instead of burning the remaining quota. A partial run — a daily-quota hit,
an outage — still commits every finished translation plus
`tests/src/unit/locale_sync_progress.json`, which the next run reads to
resume where it stopped: **re-running the workflow — or just waiting for the
next day's cron — is the entire recovery procedure.** Only a fully
successful run repins the baseline and deletes the progress file, so the
sync runs stay red until every string is translated and nothing
unvalidated ever ships. **The fallback when the engine is down is a human**:
anyone can hand-translate the strings the dry-run
lists, run `python scripts/generate_locales.py` and
`python scripts/update_locale_baseline.py`, and open an ordinary PR — the
next sync run no-ops (it also cleans up any committed progress file).
Hand-edits always win; the machine only ever touches strings whose English
changed. The engine itself is one function (`_call_gemini`) with
`GEMINI_API_URL` / `GEMINI_MODEL` / `GEMINI_API_KEY` overrides for any
Gemini-compatible endpoint, so replacing the provider stays a one-function
change.

The Webhook Proxy add-on and its bundled integration stay **English-only by
decision** — not worth the upkeep. The test records that, so any other new
catalog directory fails until it is either translated everywhere or listed as
English-only alongside them.

## Home Assistant Add-on

**Required files:**
- `repository.yaml` (root) - For HA add-on store recognition
- `homeassistant-addon/config.yaml` - Must match `pyproject.toml` version

**Two add-on flavors:** `homeassistant-addon/` (stable, slug `ha_mcp`) and
`homeassistant-addon-dev/` (dev channel, slug `ha_mcp_dev`) are *separate*
add-ons with *separate* `config.yaml` files.

**Functional config is NOT auto-synced between them.** The release pipeline
only syncs the *version* (the `update-addon-config` job) and the *changelog*
(the `Copy changelog to addon directory` step in `semver-release.yml`) into
`homeassistant-addon/`. Functional keys — `ingress`, `ports`,
`host_network`, `options`/`schema`, etc. — must be edited **by hand** in each
flavor. When you add a non-beta capability to the dev add-on that should also
ship on stable (e.g. `ingress` for the web Settings UI / "Open Web UI" button),
mirror it into `homeassistant-addon/config.yaml` **in the same PR**. Assuming
"the release pipeline handles it" is what kept `ingress` off the stable add-on.
Beta-only keys are the deliberate exception — see the NOTE in
`homeassistant-addon/config.yaml` and `docs/beta.md`.

### Webhook Proxy add-on: dev-first, promote-only

**Any work on the Webhook Proxy add-on must start by reading
[`homeassistant-addon-webhook-proxy/AGENTS.md`](homeassistant-addon-webhook-proxy/AGENTS.md)**
— it owns the full flow (flavors, versioning guard, promotion, testing).
The short version: `homeassistant-addon-webhook-proxy/` (stable) is never
edited directly by a PR in regular operation; every change (code *and* docs)
lands on `homeassistant-addon-webhook-proxy-dev/` with a version bump, and
stable is updated only via the manual promote workflow.

**Docs**: https://developers.home-assistant.io/docs/add-ons

## API Research

Search HA Core without cloning (500MB+ repo):
```bash
# Search for patterns
gh search code "use_blueprint" --repo home-assistant/core path:tests --json path --limit 10

# Fetch file contents (base64 encoded)
gh api /repos/home-assistant/core/contents/homeassistant/components/automation/config.py \
  --jq '.content' | base64 -d > /tmp/ha_config.py
```

## Release Process

Uses [semantic-release](https://python-semantic-release.readthedocs.io/) with conventional commits.

| Prefix | Bump | Changelog |
|--------|------|-----------|
| `fix:`, `perf:`, `refactor:` | Patch | User-facing |
| `feat:` | Minor | User-facing |
| `feat!:` or `BREAKING CHANGE:` | Major | User-facing |
| `chore:`, `ci:`, `test:` | No release | Internal |
| `docs:` | No release | User-facing |
| `*:(internal)` | Same as type | Internal |

**Use `(internal)` scope** for changes that aren't user-facing:
```bash
feat(internal): Log package version on startup  # Internal, not in user changelog
feat: Add dark mode                             # User-facing
```

| Channel | When Updated |
|---------|--------------|
| Dev (`.devN`) | Every master commit |
| Stable | Biweekly (Wednesday 10:00 UTC) |

Manual release: Actions > SemVer Release > Run workflow.
