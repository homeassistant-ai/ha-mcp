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
/home/julien/github/ha-mcp/           # Main repository (checkout master here)
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
cd /home/julien/github/ha-mcp
git worktree add worktree/issue-42 -b issue-42
git worktree add worktree/feat-new-feature -b feat/new-feature

# Wrong - don't create worktrees at repo root
git worktree add issue-42 -b issue-42          # ❌ Creates orphaned worktree
git worktree add ../issue-42 -b issue-42       # ❌ Outside repo, no .claude/agents/
```

**Working in a worktree:**
```bash
# Navigate to your worktree
cd worktree/issue-42

# Work normally - you have full access to .claude/agents/
git status
git commit -m "feat: implement feature"
git push

# When done, return to main repo and clean up
cd /home/julien/github/ha-mcp
git worktree remove worktree/issue-42
```

**Cleaning up stale worktrees:**
```bash
# If worktree directories were deleted but git still tracks them
git worktree prune
```

### Agent Workflows

Custom agent workflows are located in `.claude/agents/`:

| Agent | File | Model | Purpose |
|-------|------|-------|---------|
| **issue-analysis** | `issue-analysis.md` | Opus | Deep issue analysis - comprehensive codebase exploration, implementation planning, architectural assessment, complexity evaluation. Complements automated Gemini triage with human-directed deep analysis. |
| **issue-to-pr-resolver** | `issue-to-pr-resolver.md` | Sonnet | End-to-end issue implementation: pre-flight checks → worktree creation → implementation with tests → pre-PR checkpoint → PR creation → iterative CI/review resolution until merge-ready. |
| **my-pr-checker** | `my-pr-checker.md` | Sonnet | Review and manage YOUR OWN PRs - check comments, CI status, resolve review threads, monitor until all checks pass. Use for your PRs, not external contributions. |

## Project Overview

**Home Assistant MCP Server** - A production MCP server enabling AI assistants to control Home Assistant smart homes. Provides 80+ tools for entity control, automations, device management, and more.

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

**Two-Tier System:**

- **Automated Triage (Gemini)**: Runs automatically on new issues via `.github/workflows/gemini-triage.yml`. Performs quick completeness check and adds initial guidance. Adds `triaged` label when complete.

- **Deep Analysis (Human-Directed - Claude)**: Comprehensive codebase exploration, implementation planning, and architectural assessment. Use for issues requiring detailed planning or architectural decisions.

**When the user says "analyze issues" or "deep analysis":**

1. **List issues needing deep analysis**:
   ```bash
   gh issue list --state open --json number,title,labels --jq '.[] | select(.labels | map(.name) | contains(["issue-analyzed"]) | not) | "#\(.number): \(.title)"'
   ```

2. **Report the list to the user** showing all issues that need deep analysis

3. **Launch parallel issue-analysis agents** - one Task tool call per issue, ALL IN THE SAME MESSAGE:
   ```
   # In a SINGLE assistant message, make multiple Task tool calls:
   <Task tool call: subagent_type="issue-analysis", prompt="Analyze issue #42 on homeassistant-ai/ha-mcp">
   <Task tool call: subagent_type="issue-analysis", prompt="Analyze issue #43 on homeassistant-ai/ha-mcp">
   <Task tool call: subagent_type="issue-analysis", prompt="Analyze issue #44 on homeassistant-ai/ha-mcp">
   # ... one for each issue needing deep analysis
   ```

4. **Each issue-analysis agent independently**:
   - Fetches and analyzes the issue
   - Performs deep codebase exploration
   - Assesses implementation approaches and complexity
   - Evaluates priority relative to other issues
   - Updates labels (`ready-to-implement`, `needs-choice`, `needs-info`, priority)
   - Adds the `issue-analyzed` label
   - Posts detailed analysis comment to the issue

5. **Collect and summarize results** from all parallel agents

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
gh api repos/homeassistant-ai/ha-mcp/pulls/<PR>/comments --jq '.[] | {path: .path, line: .line, author: .author.login, created_at: .created_at}'

# Check for unresolved review threads
gh pr view <PR> --json reviews --jq '.reviews[] | select(.state == "COMMENTED") | .body'
```

**Resolve threads:**
After addressing a comment, **ALWAYS post a comment explaining the resolution, then mark the thread as resolved**:

```bash
# 1. FIRST: Post comment explaining what was done
gh pr review <PR> --comment --body "✅ Fixed in [commit]. [Explanation]"
# OR for dismissed suggestions:
gh pr review <PR> --comment --body "📝 Not addressing because [reason]."

# 2. THEN: Resolve the thread
gh api graphql -f query='mutation($threadId: ID!) {
  resolveReviewThread(input: {pullRequestReviewThreadId: $threadId}) {
    thread { id isResolved }
  }
}' -f threadId=<thread_id>
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

**Example PR comment:**
```markdown
## Implementation Summary

**Choices Made:**
- Used context-aware recursion to preserve `conditions` in choose blocks while normalizing at root level
- Added both unit tests (fast feedback) and E2E tests (real API validation)
- Fixed unrelated `test_script_traces` failure by adding polling logic

**Problems Encountered:**
- Initial implementation incorrectly passed `in_choose_or_if` flag recursively, causing conditions inside sequence blocks to not be normalized
- Gemini suggested logbook verification in E2E test, but manual trigger bypasses conditions - simplified to structural validation instead

**Suggested Improvements:**
- Consider adding integration test with actual state changes to verify choose block execution (currently only validates structure)
```

### Implementing Improvements in Separate PRs

**When you identify improvements with long-term benefit, implement them in separate PRs:**

**Types of improvements to implement:**
- Workflow improvements (updates to CLAUDE.md/AGENTS.md)
- Code quality improvements (refactoring, better patterns)
- Documentation improvements
- Test infrastructure improvements
- Build/CI improvements

**Branching strategy:**
```bash
# Prefer branching from master when possible
git checkout master
git pull
git checkout -b improve/description

# Only branch from PR branch if improvement depends on PR changes
git checkout feature/main-pr-branch
git checkout -b improve/description-depends-on-main-pr
```

**Rules:**
1. **Separate PR required** - never mix improvements with main feature PR
2. **Branch from master** when possible (most improvements are independent)
3. **Branch from PR branch** only if improvement depends on PR changes
4. **Avoid merge conflicts** - keep improvements focused and minimal
5. **Only implement long-term benefits** - skip "nice to have" without clear value
6. **For `.claude/agents/` changes**: Always branch from and PR to master

**Workflow:**
1. Complete main PR (all checks green, comments addressed)
2. Identify improvements during work
3. Create separate PR(s) for improvements
4. Mention improvement PRs in main PR final comment
5. Return control to user with status of all PRs

**Example final comment mentioning improvements:**
```markdown
## Implementation Summary

**Main PR (#123):**
- ✅ All checks passing, ready for merge
- Feature X implemented with tests

**Improvement PRs created:**
- PR #124: Update CLAUDE.md with better CI failure debugging commands
- PR #125: Refactor common validation logic into shared utility

**Choices Made:** [...]
**Problems Encountered:** [...]
```

### Hotfix Process (Critical Bugs Only)

**When to use hotfix vs regular fix:**
- **Hotfix**: Critical production bug in current stable release that needs immediate patch
- **Regular fix**: Bug introduced after latest stable release, or non-critical fixes

**Important**: Hotfix branches MUST be based on the `stable` tag. The code you're fixing must exist in stable.

**Before creating a hotfix, verify the code exists in stable:**
```bash
# Check what version stable points to
git fetch --tags --force
git log -1 --oneline stable

# Verify the buggy code exists in stable
git show stable:path/to/file.py | grep "buggy_code"
```

**If the code doesn't exist in stable**, use a regular fix branch from master instead:
```bash
# Example: jq dependency added in v5.0.0, but stable was at v4.22.1
# → Cannot hotfix, must use regular fix branch
git checkout -b fix/description master
```

**Creating a hotfix:**
```bash
git checkout -b hotfix/description stable
# Make your fix
git add . && git commit -m "fix: description"
gh pr create --base master
```

**Hotfix workflow execution:**
When hotfix PR merges, `hotfix-release.yml` runs:
1. Validates branch is based on stable tag
2. Runs semantic-release (creates version tag, updates CHANGELOG.md)
3. Creates draft GitHub release
4. Copies CHANGELOG.md to `homeassistant-addon/` and pushes to master
5. Updates `stable` tag to point to new release commit
6. Builds binaries and publishes release

The `stable` tag is updated AFTER the changelog sync, ensuring it points to the exact release commit, not subsequent maintenance commits.

### Boy Scout Rule

**Principle**: "Always leave the code cleaner than you found it." — Robert C. Martin, *Clean Code*

This principle guides incremental quality improvements during implementation work. The goal is continuous, low-risk enhancement without introducing regressions.

**Where this principle applies most strongly:**

1. **Tool descriptions** - Always improve clarity, accuracy, and usefulness when touching tool docstrings
2. **Tests** - See testing guidelines below

**For production code (non-test, non-docs):**

Balance improvement against regression risk. Consider:
- Code complexity and brittleness
- Test coverage for the affected area
- Scope of your current work
- Impact of potential bugs

**Testing guidelines:**

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

**Examples:**

```python
# GOOD: Adding test for new behavior without refactoring
def test_new_automation_trigger():
    # Test the specific feature you added
    pass

# GOOD: Improving tool description clarity
"""
Create a helper entity in Home Assistant.

OLD: "Make helper with config"
NEW: "Create a helper entity (input_boolean, counter, etc.) with the specified configuration.
     Use ha_get_domain_docs('input_boolean') for schema details."
"""

# BAD: Large refactor while implementing a feature
# Instead: Open issue #XYZ "Refactor helper creation logic" and stay focused on your feature

# GOOD: Low-risk quality improvement
# Renaming a confusing variable while fixing a bug in that function

# BAD: High-risk refactor
# Extracting shared logic into new abstractions while fixing a bug
# Instead: Fix the bug, then open an issue to track the refactor opportunity
```

**When to open an issue instead:**

- Refactoring would touch many files
- Unclear how to improve without breaking changes
- Improvement requires design decisions
- Would significantly expand PR scope

**Remember**: Small, continuous improvements beat large, risky refactors. Better tests, clearer docs, and obvious code wins compound over time.

## CI/CD Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `pr.yml` | PR opened | Lint, type check |
| `e2e-tests.yml` | PR to master | Full E2E tests (~3 min) |
| `publish-dev.yml` | Push to master | Dev release `.devN` |
| `notify-dev-channel.yml` | Push to master (src/) | Comment on PRs/issues with dev testing instructions |
| `semver-release.yml` | Weekly Tue 10:00 UTC | Stable release |
| `hotfix-release.yml` | Hotfix PR merged | Immediate patch release |
| `build-binary.yml` | Release | Linux/macOS/Windows binaries |
| `addon-publish.yml` | Release | HA add-on update |

## Development Commands

### Setup
```bash
uv sync --group dev        # Install with dev dependencies
uv run ha-mcp              # Run MCP server (80+ tools)
cp .env.example .env       # Configure HA connection
```

### Claude Code Hooks

**Post-Push Reminder** (`.claude/settings.local.json`):
- Reminds to update PR description after `git push`
- Appears in Claude Code output
- Personal workflow helper (gitignored, not committed)

### Testing
E2E tests are in `tests/src/e2e/` (not `tests/e2e/`).

```bash
# Run E2E tests (requires Docker daemon)
uv run pytest tests/src/e2e/ -v --tb=short

# Run specific test
uv run pytest tests/src/e2e/workflows/automation/test_lifecycle.py -v

# Interactive test environment
uv run hamcp-test-env                    # Interactive mode
uv run hamcp-test-env --no-interactive   # For automation
```

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
├── tools/             # 28 modules, 80+ tools
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

## Writing MCP Tools

### Naming Convention
`ha_<verb>_<noun>`:
- `get` — single item (`ha_get_state`)
- `list` — collections (`ha_list_areas`)
- `search` — filtered queries (`ha_search_entities`)
- `set` — create/update (`ha_config_set_helper`)
- `delete` — remove (`ha_config_delete_automation`)
- `call` — execute (`ha_call_service`)

### Tool Structure
Create `tools_<domain>.py` in `src/ha_mcp/tools/`. Registry auto-discovers it.

```python
def register_<domain>_tools(mcp, client, **kwargs):
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    @log_tool_usage
    async def ha_<verb>_<noun>(param: str) -> dict[str, Any]:
        """One-line summary starting with action verb."""
        # For complex schemas, add: "Use ha_get_domain_docs('<domain>') for details."
```

### Safety Annotations
| Annotation | Default | Use For |
|------------|---------|--------|
| `readOnlyHint: True` | `False` | Tool does not modify its environment |
| `destructiveHint: True` | `True` | Tool may perform destructive updates (only meaningful when `readOnlyHint` is false). Set to `False` for non-destructive writes (e.g., creating a record) |
| `idempotentHint: True` | `False` | Repeated calls with same args have no additional effect (only meaningful when `readOnlyHint` is false) |

### Error Handling

**Always use the dedicated error functions** from `errors.py` and `helpers.py`. Never construct raw error dicts manually — the helpers ensure consistent structure, error codes, and suggestions across all tools.

**Domain-specific errors** (`errors.py`) — use these when the error type is known:
```python
from ..errors import create_entity_not_found_error, create_validation_error, create_service_error

# Entity lookup failures (404 / not found)
return create_entity_not_found_error(entity_id, details=str(e))

# Invalid parameters
return create_validation_error("Invalid format", parameter="entity_ids", details=str(e))

# Service call failures
return create_service_error(domain, service, message=f"Service call failed: {e}", details=str(e))
```

Available helpers: `create_entity_not_found_error`, `create_connection_error`, `create_auth_error`, `create_service_error`, `create_validation_error`, `create_config_error`, `create_timeout_error`, `create_resource_not_found_error`, and the generic `create_error_response`.

**Catch-all exception handler** (`helpers.py`) — use in `except Exception` blocks:
```python
from .helpers import exception_to_structured_error

except Exception as e:
    return exception_to_structured_error(e, context={"entity_id": entity_id})
```

**Pattern for tools**: Use `exception_to_structured_error` as the catch-all — it already classifies 404s, auth errors, timeouts, etc. based on exception type and message. Pass `context={"entity_id": ...}` so it can produce `ENTITY_NOT_FOUND` for 404 errors automatically. No manual 404 string matching needed:
```python
try:
    result = await client.get_entity_state(entity_id)
    return await add_timezone_metadata(client, result)
except Exception as e:
    error_response = exception_to_structured_error(e, context={"entity_id": entity_id})
    return await add_timezone_metadata(client, error_response)
```

### Return Values
```python
{"success": True, "data": result}                    # Success
{"success": True, "partial": True, "warning": "..."}  # Degraded
{"success": False, "error": {...}}                    # Failure
```

### Tool Consolidation
When a tool's functionality is fully covered by another tool, **remove** the redundant tool rather than deprecating it. Fewer tools reduces cognitive load for AI agents and improves decision-making. Do not add deprecation notices or shims — just delete the tool and update any docstring references to point to the replacement.

### Breaking Changes Definition

A change is **BREAKING** only if it removes functionality that users depend on without providing an alternative.

**Breaking Changes (require major version bump):**
- Deleting a tool without providing alternative functionality elsewhere
- Removing a feature that has no replacement in any other tool
- Making something impossible that was previously possible

**NOT Breaking Changes (these are improvements):**
- Tool consolidation (combining multiple tools into one) — **encouraged**
- Tool refactoring (restructuring how tools work internally)
- Parameter changes (as long as same outcome achievable via other means)
- Return value restructuring (as long as data still accessible)
- Tool renaming with functionality preserved

**Rationale:** Tool consolidation reduces token usage and cognitive load for AI agents. Refactoring improves maintainability. Only mark as breaking when functionality is genuinely lost forever, not when it's restructured or consolidated.

## Tool Waiting Behavior

**Principle**: MCP tools should wait for operations to complete before returning, not just acknowledge API success.

**Implementation (#381)**: Tools have an optional `wait` parameter (default `True`) that controls whether they poll for completion:

```python
# Config operations wait by default
await ha_config_set_helper(...)  # Polls until entity registered

# Opt-out for bulk operations
for config in configs:
    await ha_config_set_automation(config, wait=False)
await _verify_all_created(entity_ids)  # Batch verification
```

**Tool Categories**:
- **Config ops** (automations, helpers, scripts): Wait by default (poll until entity queryable/removed)
- **Service calls** (lights, switches): Wait for state change on state-changing services (turn_on, turn_off, toggle, etc.)
- **Async ops** (automation triggers, external integrations): Return immediately (not state-changing)
- **Query ops** (get_state, search): Return immediately (no `wait` parameter)

**Shared utilities** in `src/ha_mcp/tools/util_helpers.py`:
- `wait_for_entity_registered(client, entity_id)` — polls until entity accessible via state API
- `wait_for_entity_removed(client, entity_id)` — polls until entity no longer accessible
- `wait_for_state_change(client, entity_id, expected_state)` — polls until state changes

## Context Engineering & Progressive Disclosure

This project applies [context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) and [progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/) principles to tool design. These complementary approaches help manage cognitive load for both the LLM and the end user.

### Context Engineering

Context engineering treats LLM context as a finite resource with diminishing returns. Rather than front-loading all information, provide the minimum context needed and let the model fetch more when required.

**Guiding principles:**
- **Favor statelessness** — Avoid server-side session tracking or MCP-side state when possible. Use content-derived identifiers (hashes, IDs) that the client can pass back. Example: dashboard updates use content hashing for optimistic locking—hash is computed on read, verified on write to detect conflicts, no session state needed.
- Delegate validation to backend systems when they already handle it well (HA Core uses voluptuous schemas with clear error messages)
- Keep tool parameters simple—let the backend API handle type coercion, defaults, and validation
- Rely on documentation tools rather than embedding extensive docs in every tool description
- Trust that model knowledge + on-demand docs = sufficient context

**Example - pass-through approach:**
```python
# Let HA validate and return its own error messages
message = {"type": f"{helper_type}/create", "name": name}
for param, value in [("latitude", latitude), ("longitude", longitude)]:
    if value is not None:
        message[param] = value
result = await client.send_websocket_message(message)
```

**When tool-side logic adds value:**
- Format normalization for UX convenience (e.g., `"09:00"` → `"09:00:00"`)
- Parsing JSON strings from MCP clients that stringify arrays
- Combining multiple HA API calls into one logical operation

### Progressive Disclosure

[Jakob Nielsen's progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/) principle: show essential features first, reveal complexity gradually. This applies directly to [LLM context management](https://www.inferable.ai/blog/posts/llm-progressive-context-encrichment)—giving LLMs more context often makes them perform worse by diluting attention.

**How we apply this in ha-mcp:**

| Pattern | Example |
|---------|---------|
| **Docs on demand** | Tool descriptions reference `ha_get_domain_docs()` instead of embedding full documentation |
| **Hints in UX flow** | First tool in a workflow hints at related tools (e.g., `ha_search_entities` suggests `ha_get_state`) |
| **Error-driven discovery** | When a tool fails, the error response hints at `ha_get_domain_docs()` for syntax help |
| **Layered parameters** | Required params first, optional params with sensible defaults |
| **Focused returns** | Return essential data; let user request details via follow-up tools |

**Practical examples in this codebase:**
- `ha_config_set_helper` has minimal docstring, points to `ha_get_domain_docs()` for each helper type
- Search tools return entity IDs and names; full state requires `ha_get_state`
- Error responses include `suggestions` array guiding next steps

### Testing Model Knowledge

Before adding extensive documentation to tool descriptions, test what models already know. Use a **no-context sub-agent** to probe baseline knowledge:

```
Task tool with model=haiku or model=sonnet:
"Without searching or fetching anything, answer from your training data only:
 How do you create a [X] in Home Assistant via WebSocket API?
 What parameters are required vs optional?
 Be honest if you're uncertain."
```

This reveals:
- What the model knows from training (no need to document)
- What gaps exist (target these with `ha_get_domain_docs()` hints)
- Confidence levels across model tiers (haiku vs sonnet vs opus)

**Important: Fact-check model claims.** Models can hallucinate plausible-sounding syntax. Always verify against actual source code or documentation:
```bash
# Check HA Core for actual API schema
gh api /repos/home-assistant/core/contents/homeassistant/components/{domain}/__init__.py \
  --jq '.content' | base64 -d | grep -A 20 "CREATE_FIELDS\|vol.Schema"
```

**Example findings from helper analysis:**
| Model | counter | schedule | zone | tag |
|-------|---------|----------|------|-----|
| Haiku | ~60% confident | ~30% uncertain | ~50% | ~20% |
| Sonnet | ~80% accurate | ~75% knows format | ~85% | ~50% |

This informs whether to embed docs (low model knowledge) or just hint at `ha_get_domain_docs()` (sufficient model knowledge).

### References
- [Anthropic: Effective Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Context Engineering Guide](https://www.promptingguide.ai/guides/context-engineering-guide)
- [Nielsen Norman Group: Progressive Disclosure](https://www.nngroup.com/articles/progressive-disclosure/)
- [Progressive Context Enrichment for LLMs](https://www.inferable.ai/blog/posts/llm-progressive-context-encrichment)

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
| Stable | Weekly (Tuesday 10:00 UTC) |

Manual release: Actions > SemVer Release > Run workflow.

## Custom Agents

Located in `.claude/agents/`:

| Agent | Purpose |
|-------|---------|
| `issue-analysis` | Deep issue analysis: codebase exploration, implementation planning, complexity assessment |
| `issue-to-pr-resolver` | End-to-end: issue → branch → implement → PR → CI green |
| `my-pr-checker` | Review YOUR OWN PRs: comments, CI status, resolve threads, monitor until ready |

## Skills

Located in `.claude/skills/`:

| Skill | Command | Purpose | When to Use |
|-------|---------|---------|-------------|
| `bat` | `/bat [scenario]` | Bot Acceptance Testing - validates MCP tools work correctly from real AI agent CLIs (Claude/Gemini) | PR validation, regression detection, end-to-end integration verification |
| `contrib-pr-review` | `/contrib-pr-review <pr-number>` | Review external contributor PRs for safety, quality, and readiness | Reviewing PRs from contributors (not from current user). Checks security, tests, size, intent. |
| `wt` | `/wt <branch-name>` | Create git worktree in `worktree/` subdirectory with up-to-date master | Quick worktree creation for feature branches. Pulls master first. |

### BAT (Bot Acceptance Testing)

**Usage:** `/bat [scenario-description]`

Quick summary:
- Validates MCP tools work correctly from a real AI agent's perspective (Claude/Gemini CLIs)
- Runner at `tests/uat/run_uat.py` returns concise summary to stdout, full results to temp file
- Use for PR validation, regression detection, and end-to-end integration verification
- Progressive disclosure: only read `results_file` when you need to dig deeper

For complete workflow, scenario design guidelines, examples, and output format, invoke `/bat --help` or read `.claude/skills/bat/SKILL.md`.

### Contributor PR Review

**Usage:** `/contrib-pr-review <pr-number>`

Review external contributor PRs with comprehensive security-first analysis:
- **Security assessment** - prompt injection, AGENTS.md changes, workflow modifications
- **Test coverage** - checks for pre-existing tests and new tests (uses both naming conventions and grep for function/class names)
- **Contributor experience** - assesses both project contributions and overall GitHub experience
- **PR size appropriateness** - validates size matches contributor experience level
- **Intent alignment** - checks issue linkage and scope

**When to use:** Reviewing PRs from external contributors (not your own PRs). Provides structured review framework focusing on safety and quality.

See `.claude/skills/contrib-pr-review/SKILL.md` for full documentation.

## Documentation Updates

Update this file when:
- Discovering workflow improvements
- Solving non-obvious problems
- API/test patterns learned

**Rule:** If you struggled with something, document it for next time.
