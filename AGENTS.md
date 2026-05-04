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
└── .claude/agents/                    # Custom agent workflows
```

**Why use `worktree/` subdirectory:**
- Keeps worktrees organized in one place
- Gitignored (won't pollute `git status`)
- All worktrees automatically inherit `.claude/agents/` workflows
- Easy cleanup: `git worktree prune` removes stale references

**Quick command:** Use `/wt <branch-name>` skill to create worktree automatically.

## Worktree Workflow

### Creating Worktrees

**ALWAYS create worktrees in the `worktree/` subdirectory**, not at the repository root.

```bash
# Correct - worktrees go in worktree/ subdirectory
cd <repo-root>
git worktree add worktree/issue-42 -b issue-42
git worktree add worktree/feat-new-feature -b feat/new-feature

# Wrong - don't create worktrees at repo root
git worktree add issue-42 -b issue-42          # ❌ Creates orphaned worktree
git worktree add ../issue-42 -b issue-42       # ❌ Outside repo, no .claude/agents/
```

**Cleanup:** `git worktree remove worktree/<name>` or `git worktree prune` for stale references.

### Agent Workflows

Custom agent workflows are located in `.claude/agents/`:

| Agent | File | Model | Purpose |
|-------|------|-------|---------|
| **issue-analysis** | `issue-analysis.md` | Opus | Deep issue analysis - comprehensive codebase exploration, implementation planning, architectural assessment, complexity evaluation. Complements automated Gemini triage with human-directed deep analysis. |
| **issue-to-pr-resolver** | `issue-to-pr-resolver.md` | Sonnet | End-to-end issue implementation: pre-flight checks → worktree creation → implementation with tests → pre-PR checkpoint → PR creation → iterative CI/review resolution until merge-ready. |
| **my-pr-checker** | `my-pr-checker.md` | Sonnet | Review and manage YOUR OWN PRs - check comments, CI status, resolve review threads, monitor until all checks pass. Use for your PRs, not external contributions. |

## Project Overview

**Home Assistant MCP Server** - A production MCP server enabling AI assistants to control Home Assistant smart homes. Provides 92+ tools for entity control, automations, device management, and more.

- **Repo**: `homeassistant-ai/ha-mcp`
- **Package**: `ha-mcp` on PyPI
- **Python**: 3.13 only

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
- **Claude `contrib-pr-review` (on-demand)**: Repo-specific security (AGENTS.md, .github/), detailed test analysis, PR size assessment, issue linkage
- **Claude `my-pr-checker` (lifecycle)**: Resolve threads, fix issues, monitor CI, create improvement PRs

### Issue Labels
| Label | Meaning |
|-------|---------|
| `ready-to-implement` | Clear path, no decisions needed |
| `needs-choice` | Multiple approaches, needs stakeholder input |
| `needs-info` | Awaiting clarification from reporter |
| `priority: high/medium/low` | Relative priority |
| `triaged` | Automated Gemini triage complete |
| `issue-analyzed` | Deep Claude analysis complete |

### Issue Analysis Workflow

- **Automated Triage (Gemini)**: Runs on new issues via `.github/workflows/gemini-triage.yml`. Adds `triaged` label.
- **Deep Analysis (Claude)**: When user says "analyze issues", list issues missing `issue-analyzed` label, then launch **parallel** `issue-analysis` agents (one per issue, ALL in a single message). Each agent explores the codebase, posts analysis, and updates labels.

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

**Why comment first:**
- Provides context for future reviewers
- Documents decision-making process
- Makes it clear what was done or why suggestion was dismissed

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

**Always create PRs as draft.** Use `gh pr create --draft`. Only mark a PR as ready for review (`gh pr ready <PR>`) when explicitly requested by the user.

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
8. **Repeat steps 2-7 until:**
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

**Final reporting (only after ALL workflow steps complete):**

Once the PR is ready (all checks green, comments addressed), provide:

1. **Comment on the PR** with comprehensive details:
   ```markdown
   ## Implementation Summary

   **Choices Made:**
   - [List key technical decisions and rationale]

   **Problems Encountered:**
   - [Issues faced and how they were resolved]
   - [Unrelated test failures fixed (if any)]

   **Suggested Improvements:**
   - [Optional follow-up work or technical debt noted]
   ```

2. **Short summary for user** when returning control:
   - High-level overview of what was accomplished
   - Any choices that may need user input
   - Current PR status

### Implementing Improvements in Separate PRs

Implement long-term improvements (workflow, code quality, docs, tests, CI) in **separate PRs** — never mix with the main feature PR. Branch from master when possible; only branch from the PR branch if the improvement depends on those changes. For `.claude/agents/` changes, always branch from and PR to master. Mention improvement PRs in the main PR's final comment.

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

### Boy Scout Rule

Improve code incrementally when touching it — especially tool docstrings and tests. Balance against regression risk (complexity, coverage, scope).

| Scenario | Action |
|----------|--------|
| **No tests exist for code you're touching** | Add tests for the specific behavior you're implementing/fixing, without refactoring existing code |
| **Tests exist but coverage is low** | Add tests for gaps if you're already working in that area |
| **Tests exist, quality is low** | Improve test quality if it's straightforward (better assertions, clearer names, remove duplication) |
| **Code quality is really low** | Open an issue describing the technical debt instead of fixing it inline |

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

**When to open an issue instead:** Refactoring would touch many files, requires design decisions, or would significantly expand PR scope.

## CI/CD Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `pr.yml` | PR opened | Lint, type check |
| `e2e-tests.yml` | PR to master | Full E2E tests (~3 min) |
| `publish-dev.yml` | Push to master | Dev release `.devN` |
| `notify-dev-channel.yml` | Push to master (src/) | Comment on PRs/issues with dev testing instructions |
| `semver-release.yml` | Biweekly Wed 10:00 UTC | Stable release |
| `hotfix-release.yml` | Hotfix PR merged | Immediate patch release |
| `build-binary.yml` | Release | Linux/macOS/Windows binaries |
| `addon-publish.yml` | Release | HA add-on update |
| `sync-tool-docs.yml` | Push to master (`src/ha_mcp/tools/`, `scripts/extract_tools.py`) | Regenerate `tools.json`, README, DOCS.md |

## Development Commands

### Setup
```bash
uv sync --group dev        # Install with dev dependencies
uv run ha-mcp              # Run MCP server (92+ tools)
cp .env.example .env       # Configure HA connection
```

### Claude Code Hooks

**Post-Push Reminder** (`.claude/settings.local.json`):
- Reminds to update PR description after `git push`
- Appears in Claude Code output
- Personal workflow helper (gitignored, not committed)

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
# Stdio mode (Claude Desktop)
docker run --rm -i -e HOMEASSISTANT_URL=... -e HOMEASSISTANT_TOKEN=... ghcr.io/homeassistant-ai/ha-mcp:latest

# HTTP mode (web clients)
docker run -d -p 8086:8086 -e HOMEASSISTANT_URL=... -e HOMEASSISTANT_TOKEN=... ghcr.io/homeassistant-ai/ha-mcp:latest ha-mcp-web
```

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
├── tools/             # 28 modules, 92+ tools
│   ├── registry.py          # Lazy auto-discovery
│   ├── smart_search.py      # Fuzzy entity search
│   ├── device_control.py    # WebSocket-verified control
│   ├── tools_*.py           # Domain-specific tools
│   └── util_helpers.py      # Shared utilities
├── utils/
│   ├── fuzzy_search.py      # textdistance-based matching
│   ├── domain_handlers.py   # HA domain logic
│   └── operation_manager.py # Async operation tracking
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

## Setup Wizard (`site/src/pages/setup.astro`)

Single-file Astro page that drives the on-site setup flow. Both the metadata (which clients/platforms/connections/deployments exist) and the per-client instruction prose live in this one file.

**Data** — four pre-sorted JS arrays at the top of the component frontmatter:

```ts
const clientsData = [...]    // 19 supported AI clients
const platformsData = [...]  // macOS / Linux / Windows / Docker
const connectionsData = [...]// local / network / remote
const deploymentData = [...] // uvx / docker / ha-addon / cloudflared / webhook-proxy
```

These feed the picker tiles in the markup section AND the wizard `<script>` block (`state.client`, `state.connection`, etc.).

**Instruction templates** are JS template literals inside the `<script>` block, keyed off `state.client.id` / `platformId` / `state.connection.id` / `state.proxy`. Cross-cutting troubleshooting and restart-related help lives in `site/src/pages/faq.astro`; OS-specific install walkthroughs live in `guide-macos.astro` / `guide-windows.astro`.

**Adding a new client / platform / connection / deployment:**

1. Add an entry to the appropriate inline array (insert at the right `order` position). Keep each array ordered by the `order` field — the wizard renders entries in array order without re-sorting.
2. Add a wizard branch in the `<script>` block keyed off the new entry's `id`. Match neighboring patterns: JSON clients add an `else if` in the JSON config builder; CLI clients add a CLI command emit; UI clients add an `instruction-block` div with click steps. See `cursor` / `chatgpt` / `claude-code` / `cloudflared` for examples.
3. If the addition has cross-cutting troubleshooting content (PATH issues, restart requirements, version requirements), add it to `faq.astro`.

## Writing MCP Tools

### Naming Convention
`ha_<verb>_<noun>`:
- `get` — single item (`ha_get_state`)
- `list` — collections (`ha_list_areas`)
- `search` — filtered queries (`ha_search_entities`)
- `set` — create/update (`ha_config_set_helper`)
- `delete` — delete dashboards, config entries, or files (`ha_config_delete_dashboard`, `ha_delete_file`)
- `remove` — remove registry items (`ha_remove_entity`, `ha_config_remove_area`)
- `call` — execute (`ha_call_service`)
- `manage` — multi-modal tools combining several operations behind one interface (`ha_manage_addon`)

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
        # For complex schemas: use ha_get_skill_home_assistant_best_practices

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
`# For complex schemas: use ha_get_skill_home_assistant_best_practices`

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

`exception_to_structured_error` auto-classifies 404s, auth errors, timeouts by exception type. Pass `context={"entity_id": ...}` for automatic `ENTITY_NOT_FOUND` on 404s. Available helpers: `create_entity_not_found_error`, `create_connection_error`, `create_auth_error`, `create_service_error`, `create_validation_error`, `create_config_error`, `create_timeout_error`, `create_resource_not_found_error`, `create_error_response`.

### Return Values
```python
{"success": True, "data": result}                    # Success
{"success": True, "partial": True, "warning": "..."}  # Degraded
raise ToolError(json.dumps({...}))                   # Tool-level failure (isError=true)
{"success": False, "error": {...}}                   # Batch item failure only (in results list)
```

### Tool Consolidation
When a tool's functionality is fully covered by another tool, **remove** the redundant tool rather than deprecating it. Fewer tools reduces cognitive load for AI agents and improves decision-making. Do not add deprecation notices or shims — just delete the tool and update any docstring references to point to the replacement.

With 92+ tools, this project exceeds the [10-20 tool threshold](https://ai.google.dev/gemini-api/docs/function-calling) where tool selection accuracy degrades ([OpenAI](https://developers.openai.com/api/docs/guides/function-calling), [Google](https://ai.google.dev/gemini-api/docs/function-calling)). Reducing tool count is a priority, but how matters. Anthropic's [tool design blog](https://www.anthropic.com/engineering/writing-tools-for-agents) recommends combining frequently chained operations: "Instead of implementing a `list_users`, `list_events`, and `create_event` tools, consider implementing a `schedule_event` tool which finds availability and schedules an event." Their [context engineering guide](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) warns against "bloated tool sets that cover too much functionality or lead to ambiguous decision points about which tool to use." Each tool should have "a clear, distinct purpose" ([Anthropic](https://www.anthropic.com/engineering/writing-tools-for-agents)).

| Pattern | Example | Guideline |
|---------|---------|-----------|
| Tool A is a strict subset of Tool B | `ha_dashboard_find_card` fully covered by `ha_config_get_dashboard` | Consolidate (remove A) |
| Frequently chained operations | Multi-step workflows combined into one tool | Consolidate — reduces round-trips |

### Breaking Changes Definition

A change is **BREAKING** only if it removes functionality that users depend on without providing an alternative.

**Breaking Changes (require major version bump):**
- Deleting a tool without providing alternative functionality elsewhere
- Removing a feature that has no replacement in any other tool
- Making something impossible that was previously possible

Tool consolidation, refactoring, parameter/return changes, and renaming are **NOT breaking** as long as the same outcome is achievable.

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

## Context Engineering & Progressive Disclosure

Provide minimum context needed; let models fetch more on demand. LLM context is finite — more often means worse.

**Principles:**
- **Favor statelessness** — use content-derived identifiers (hashes, IDs) instead of server-side state. Example: dashboard optimistic locking via content hash.
- **Delegate validation** to HA backend (voluptuous schemas with clear errors). Tool-side logic adds value for: format normalization, JSON string parsing, combining multiple API calls.
- **Progressive disclosure** — docs on demand (`ha_get_skill_home_assistant_best_practices`), workflow hints between tools, error-driven discovery via `suggestions` arrays, layered params (required first, optional with defaults), focused returns (IDs/names; full state via follow-up).

### Testing Model Knowledge

Before adding docs to tool descriptions, test what models already know using a no-context sub-agent (haiku/sonnet). Document only gaps. Always fact-check model claims against HA Core source — models hallucinate plausible syntax.

## Home Assistant Add-on

**Required files:**
- `repository.yaml` (root) - For HA add-on store recognition
- `homeassistant-addon/config.yaml` - Must match `pyproject.toml` version

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

**Insight**: Collection-based components (helpers, scripts, automations) follow consistent patterns.

## Test Patterns

**FastMCP validates required params at schema level.** Don't test for missing required params:
```python
# BAD: Fails at schema validation
await mcp.call_tool("ha_config_get_script", {})

# GOOD: Test with valid params but invalid data
await mcp.call_tool("ha_config_get_script", {"script_id": "nonexistent"})
```

**HA API uses singular field names:** `trigger` not `triggers`, `action` not `actions`.

**E2E tests: poll after creating entities.** After creating an entity (automation, script, helper, etc.), HA needs time to register it. Never search/query immediately — use polling helpers from `tests/src/e2e/utilities/wait_helpers.py`:
```python
from ..utilities.wait_helpers import wait_for_tool_result

# BAD: entity may not be registered yet
create_result = await mcp_client.call_tool("ha_config_set_automation", {"config": config})
result = await mcp_client.call_tool("ha_deep_search", {"query": "my_sensor"})  # may return empty

# GOOD: poll until the entity appears in results
data = await wait_for_tool_result(
    mcp_client,
    tool_name="ha_deep_search",
    arguments={"query": "my_sensor", "search_types": ["automation"], "limit": 10},
    predicate=lambda d: len(d.get("automations", [])) > 0,
    description="deep search finds new automation",
)
```
Other available helpers: `wait_for_entity_state()`, `wait_for_entity_attribute()`, `wait_for_condition()`. See `wait_helpers.py` for the full set.

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

## Skills

Located in `.claude/skills/`:

| Skill | Command | Purpose | When to Use |
|-------|---------|---------|-------------|
| `bat-adhoc` | `/bat-adhoc [scenario]` | Ad-hoc bot acceptance testing - validates MCP tools with dynamically generated scenarios | PR validation, quick regression checks, one-off integration verification |
| `bat-story-eval` | `/bat-story-eval --baseline v6.6.1 [--agents gemini]` | Diff-based story evaluation: triage, pre-built + custom stories, two-version comparison | Version comparison, regression detection, hypothesis-driven testing |
| `contrib-pr-review` | `/contrib-pr-review <pr-number>` | Review external contributor PRs for safety, quality, and readiness | Reviewing PRs from contributors (not from current user). Checks security, tests, size, intent. |
| `wt` | `/wt <branch-name>` | Create git worktree in `worktree/` subdirectory with up-to-date master | Quick worktree creation for feature branches. Pulls master first. |

Invoke any skill with `/<skill-name> --help` for full documentation, or read `.claude/skills/<name>/SKILL.md`.

## Documentation Updates

Update this file when:
- Discovering workflow improvements
- Solving non-obvious problems
- API/test patterns learned

**Rule:** If you struggled with something, document it for next time.
