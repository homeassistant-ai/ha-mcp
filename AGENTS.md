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

### Automated Code Review (Gemini Code Assist)

**Gemini Code Assist** runs automatically on all PRs, providing immediate feedback on:
- Code quality (correctness, efficiency, maintainability)
- Test coverage (enforces `src/` modifications must have tests)
- Security patterns (eval/exec, SQL injection, credentials)
- Tool naming conventions and MCP patterns
- Safety annotation accuracy
- Return value consistency

**Configuration**: `.gemini/styleguide.md` and `.gemini/config.yaml`

**Division of Labor:**
- **Gemini (automatic)**: Code quality, test coverage, generic security, MCP conventions
- **Claude `/contrib-pr-review` (on-demand)**: Repo-specific security (AGENTS.md, .github/), detailed test analysis, PR size assessment, issue linkage
- **Claude `/my-pr-checker` (lifecycle)**: Resolve threads, fix issues, monitor CI, create improvement PRs

### Issue Labels

**Triage-state labels** (applied by `gemini-triage.yml` or manual triage):

| Label | Meaning |
|-------|---------|
| `ready-to-implement` | Clear path, no decisions needed |
| `needs-choices` | Multiple approaches, needs stakeholder input |
| `needs-info` | Awaiting clarification from reporter |
| `priority: high/medium/low` | Relative priority |
| `triaged` | Automated Gemini triage complete |
| `triage-failed` | Automated Gemini triage failed; circuit breaker that blocks retrigger on comments. Clear it (or run via `workflow_dispatch`) to retry |
| `issue-analyzed` | Deep Claude analysis complete |

**Bug-class labels** (applied via `.github/ISSUE_TEMPLATE/` form selection or manual triage):

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

- **Automated Triage (Gemini)**: Runs on new issues via `.github/workflows/gemini-triage.yml`. Adds `triaged` label.
- **Deep Analysis (Claude)**: When user says "analyze issues", list issues missing `issue-analyzed` label, then invoke `/issue-analysis <number>` for each sequentially (the skill drafts analysis for user approval before posting).

```bash
gh issue list --state open --json number,title,labels --jq '.[] | select(.labels | map(.name) | contains(["issue-analyzed"]) | not) | "#\(.number): \(.title)"'
```

### PR Review Comments

**Always check for comments after pushing to a PR.** Comments may come from bots (Gemini Code Assist, Copilot) or humans.

**Priority:**
- **Human comments**: Address with highest priority
- **Bot comments**: Treat as suggestions to assess, not commands. Evaluate if they add value.

**Check for comments:**
```bash
# Check all PR comments (general comments on the PR)
gh pr view <PR> --json comments --jq '.comments[] | {author: .author.login, created: .createdAt}'

# Check inline review comments (specific to code lines)
gh api repos/homeassistant-ai/ha-mcp/pulls/<PR>/comments --jq '.[] | {id, path, line, author: .user.login, created_at}'

# Check for unresolved review threads
gh pr view <PR> --json reviews --jq '.reviews[] | select(.state == "COMMENTED") | .body'
```

**Resolve threads:**
After addressing a comment, **ALWAYS post a comment explaining the resolution, then mark the thread as resolved**.

When there are inline review comments, do **both**: reply on each inline thread *and* post a PR-level review comment summarising the changes. The inline replies document the per-thread resolution where future readers expect it; the PR-level comment gives a single summary for anyone scanning the PR timeline.

**Always resolve the inline thread after replying**, unless the reply is asking the reviewer for further clarification (in which case leave the thread open so they can respond). An unresolved thread signals "still needs attention"; don't leave resolved work in that state. Unresolved threads also **block the PR from merging even after a maintainer has approved it** — the merge button stays disabled until every thread is marked resolved.

```bash
# 1a. Reply on each inline thread via the /replies sub-endpoint.
#     <comment-id> is the numeric ID from:
#       gh api repos/homeassistant-ai/ha-mcp/pulls/<PR>/comments --jq '.[].id'
gh api repos/homeassistant-ai/ha-mcp/pulls/<PR>/comments/<comment-id>/replies \
  -f body="✅ Fixed in [commit]. [Explanation]"
# OR for dismissed suggestions:
gh api repos/homeassistant-ai/ha-mcp/pulls/<PR>/comments/<comment-id>/replies \
  -f body="📝 Not addressing because [reason]."

# 1b. Also post a PR-level review comment summarising the batch of changes:
gh pr review <PR> --comment --body "✅ Addressed review feedback in [commit]. [Summary]"

# If there are no inline comments (just a general review), the PR-level
# review comment alone is sufficient.

# 2. THEN: Resolve each thread. The GraphQL input field is `threadId` — NOT
#    `pullRequestReviewThreadId`, which GitHub rejects. The thread node ID
#    (PRRT_...) comes from a reviewThreads query; match databaseId against
#    the inline-comment numeric ID to pick the right one:
gh api graphql -f query='
query {
  repository(owner: "homeassistant-ai", name: "ha-mcp") {
    pullRequest(number: <PR>) {
      reviewThreads(first: 100) {
        nodes {
          id isResolved path line
          comments(first: 1) { nodes { databaseId } }
        }
      }
    }
  }
}'

gh api graphql -f query='mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}' -f threadId=<PRRT_...>
```

## Git & PR Policies

**CRITICAL - Never commit directly to master.**

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

**Code-review bot suggestions** (Gemini Code Assist, CodeRabbit, Copilot non-blocking nits): apply inline or dismiss. Never spawn a follow-up issue from a bot suggestion unless the user explicitly confirms it's a large, out-of-scope change. See `.gemini/styleguide.md` § *Non-Blocking Suggestions and Scope* for the bot-side rule.

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
- Bug fixes without regression tests

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

**Docker image tags** (`ghcr.io/homeassistant-ai/ha-mcp`): stable releases push `:latest` + `:stable` + semver tags (`release-publish.yml`); dev builds push only `:dev` + `:dev-<sha>` (`publish-dev.yml`) — **never `:latest`**, which is reserved for stable. The HA add-on images live in separate repos (`-addon-{arch}`, `-addon-dev-{arch}`) and are selected by an explicit `version:` pin, not by `:latest`.

## Development Commands

### Setup
```bash
uv sync --group dev        # Install with dev dependencies
uv run ha-mcp              # Run MCP server
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
```bash
uv run ruff check src/ tests/ --fix
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
├── tools/             # 36 modules, auto-discovered
│   ├── registry.py          # Lazy auto-discovery
│   ├── smart_search.py      # Fuzzy entity search
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

**Service Layer**: Business logic in `smart_search.py`, `device_control.py` separate from tool modules.

**WebSocket Verification**: Device operations verified via real-time state changes.

**Tool Completion Semantics**: Tools should wait for operations to complete before returning, with optional `wait` parameter for control.

## Writing MCP Tools

### Naming Convention
`ha_<verb>_<noun>`:
- `get` — single item (`ha_get_state`)
- `list` — collections (`ha_list_services`)
- `search` — filtered queries (`ha_search_entities`)
- `set` — create/update (`ha_config_set_helper`)
- `delete` — delete dashboards, config entries, or files (`ha_config_delete_dashboard`, `ha_delete_file`)
- `remove` — remove registry items (`ha_remove_entity`, `ha_remove_area_or_floor`)
- `call` — execute (`ha_call_service`, `ha_call_event`)
- `manage` — multi-modal tools combining several operations behind one interface (`ha_manage_addon`)

**Namespace prefixes**: An optional `<namespace>_` prefix between `ha_` and the verb is allowed for grouped tool families that share a domain. The full shape becomes `ha_<namespace>_<verb>_<noun>`:
- `ha_config_<verb>_<noun>` — config-management tools (`ha_config_set_helper`, `ha_config_set_automation`, `ha_config_remove_automation`, `ha_config_delete_dashboard`)

**Accepted exceptions**: A small set of tools name a single, distinct operation where forcing a `<verb>_<noun>` shape would read worse than the natural name. These are accepted as-is and should not be flagged:
- `ha_restart`, `ha_reload_core`, `ha_eval_template`
- `ha_report_issue`, `ha_import_blueprint`
- `ha_read_file`, `ha_write_file`, `ha_deep_search`, `ha_bulk_control`
- `ha_install_mcp_tools`

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
Example: `ha_search_entities` hints at `ha_get_state`.

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
