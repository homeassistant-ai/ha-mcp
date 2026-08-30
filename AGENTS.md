# AGENTS.md

Canonical agent guidance for the Home Assistant MCP Server repository.

## Instruction structure

- `AGENTS.md` is the canonical source. `CLAUDE.md` is a symlink to it; edit `AGENTS.md` only.
- This root file owns repository-wide behavior, permissions, scope, and testing policy. Linked documents own topic-specific detail.
- Before working, read every applicable `AGENTS.md` from the repository root through the target directory; narrower files supplement every broader ancestor.
- Follow ordinary Markdown links when the task enters their scope. Do not replace them with `@imports`: imports load every linked byte at startup and defeat progressive disclosure.
- If linked guidance conflicts with this file's behavioral rules, this file controls. Repair the conflicting duplicate rather than choosing silently.

## Repository Structure

This repository uses `worktree/` for isolated branches and `local/` for scratch data; both are gitignored. Source is under `src/ha_mcp/`, component code under `custom_components/ha_mcp_tools/`, tests under `tests/`, the website under `site/`, and Home Assistant app flavors under `homeassistant-addon*/`.

Read the [development reference](docs/agents/development.md) for commands and architecture. Workflow helpers live in `.claude/skills/`; inspect a helper's own instructions when it is explicitly invoked rather than duplicating its inventory here.

## Worktree Workflow

Create feature worktrees under `worktree/` from an up-to-date
`origin/master`. A documentation-only adjustment is the sole exception that
may be committed directly on `master` or `main`:

```bash
git fetch origin master
git worktree add worktree/<name> -b <branch> origin/master
```

Before committing, verify the branch is not `master`/`main` and the working directory is the intended worktree. Preserve unrelated dirty changes in every checkout. Remove a worktree only when its branch is no longer needed and the user has authorized any associated destructive cleanup.
The repository-root checkout is where `master` is kept; `git worktree prune`
removes stale worktree references.

## Project Overview

**Home Assistant MCP Server** is a Python 3.13 FastMCP server that lets AI assistants control and configure Home Assistant through MCP. The repository is `homeassistant-ai/ha-mcp`; the package is `ha-mcp` on PyPI.

## Security

Read [`SECURITY.md`](SECURITY.md) before changing authentication, OAuth, webhooks, network exposure, proxies, filesystem access, or trust boundaries. Its threat model defines the trusted MCP-client assumption, local-network boundary, Bearer-token posture, tenant model, and Home Assistant permission scope.

A security advisory's API body does not include its discussion thread. Confirm maintainer disposition and fix scope from the private thread or a maintainer before implementing an advisory fix.

## External Documentation

Use current primary sources. The [development reference](docs/agents/development.md#external-documentation) links the Home Assistant REST/WebSocket APIs, Home Assistant Core, FastMCP, MCP specification, and app-development documentation.

## Agent-document formatting

Keep this file short enough to load on every task:

- Hard limit: 16,000 Unicode characters and 200 lines.
- Keep only broadly applicable, high-value behavior in the root. Put coding conventions and subsystem procedures in their closest durable owner.
- Retain a short section here for each major topic and state when and why to read its linked document.
- Use ordinary Markdown links, not imports. A link must name the document's scope; avoid blind “see also” references.
- Give each rule one canonical owner. Link instead of copying exact lists, commands, examples, or policy prose into multiple files.
- Keep volatile counts, file inventories, workflow catalogs, historical incidents, and tutorials out of startup context; place them beside the code or process they describe.
- Prefer short directives with concrete triggers. Explain rationale where a future editor might otherwise “simplify” a load-bearing rule.
- Use descriptive headings, fenced code blocks, and CommonMark blank lines around headings and lists. Do not use decorative formatting as structure.
- When guidance changes, check `AGENTS.md`, scoped `AGENTS.md` files, the style guide, contributing docs, tests, and inline references for drift.

## Issue & PR Management

Do not create, edit, label, close, or comment on an issue or pull request without user authorization for that write. Draft the exact proposed text first when approval has not already been given.

The detailed label taxonomy, issue-analysis query, bot behavior, review commands, CI loop, and release automation live in the [GitHub workflow reference](docs/agents/github-workflow.md).

### Automated Code Review

After every push, inspect human and automated feedback. Codex and CodeRabbit findings are hypotheses: verify them against current source, tests, and contracts. Human feedback has priority. Read every CodeRabbit review body in full because collapsed outside-diff and nitpick findings may create no unresolved thread.

CodeRabbit intentionally reviews drafts with `reviews.auto_review.drafts: true` and `auto_pause_after_reviewed_commits: 0`. Bot-authored dependency and promotion pull requests are excluded from automatic review but retain manual review commands. Exact configuration and rationale are in the [GitHub workflow reference](docs/agents/github-workflow.md#automated-review).

For an accepted inline finding, implement the fix, reply with evidence, resolve the thread, and include one pull-request-level summary when the review had inline comments. Leave a thread open only while requesting clarification.

## Git & PR Policies

- Never commit directly to `master` or `main` except for a documentation-only adjustment.
- Never push or open a pull request without explicit user permission.
- Open every pull request as a draft. Mark it ready only when explicitly asked, after refreshing its description and verifying required CI and reviews.
- Never merge, close, delete branches, publish, release, or otherwise finalize work without explicit approval for that exact action.
- Preserve the pull-request template headings and generated review sections.
- Make routine, reversible implementation decisions autonomously. Ask before a choice materially changes scope, public behavior, architecture, or review surface; do not create competing pull requests without approval.
- Choose for long-term codebase health, not implementation speed; maintainability-improving refactors are valid when they fit the user's scope.
- Draft descriptions may remain provisional. Refresh the description before marking ready and after later scope changes while ready. Do not claim readiness from stale checks or a different head SHA.

### Testing and verification

Testing behavior belongs in this root because it applies to every code change:

- Bug fixes require a failing regression test first, then the minimal fix.
- New MCP tools need E2E coverage. Any existing tool without tests gains E2E coverage even when it is not otherwise part of the current pull request. Core changes in `client/`, `server.py`, or `errors.py` need focused coverage.
- Refactors with strong existing coverage, documentation-only changes, minor parameters on well-tested tools, and utilities already exercised by E2E may not need a new test.
- Run the smallest relevant tests after changes. Read [`tests/AGENTS.md`](tests/AGENTS.md) for lanes, markers, polling, and test patterns, and the [development reference](docs/agents/development.md#test-commands) for exact commands.
- Run relevant E2E tests without waiting to be asked. Let pytest report unavailable prerequisites or skips rather than assuming them.
- Run the full E2E suite only before claiming the full suite passes; a focused file is partial evidence and must be described that way.
- Fix unrelated test failures encountered during CI, even when time-consuming, subject to the Boy Scout scope rules below.
- Match verification to risk. Documentation-only work needs structural checks such as links, generated-file drift, size, and workflow shape—not unrelated application E2E.
- Never state that tests, lint, builds, CI, or review are clean without fresh evidence from the relevant command or current pull-request head.

### Boy Scout Rule — Handling Discovered Improvements

Leave touched code better than you found it. Fix-in-place is the default, but the user's scope controls meaningful expansion. “Improve incrementally” means commit-by-commit within this pull request, not across follow-up pull requests.

| Finding | Action |
|---|---|
| Small and clearly related—including missing or weak tests in the touched area and straightforward test-quality fixes | Fix in this pull request, preferably as a distinct commit. |
| Mid-sized or meaningfully expands the diff | Pause before pushing and ask whether to bundle it. |
| Large, unrelated, or a different subsystem | Explain the scope change and ask; do not silently defer or expand. |

About 200 changed lines is a should-I-ask heuristic, not a bundling cap. Work
of any size may stay in the pull request when it is not grossly out of scope;
estimate honestly and do not inflate the estimate to justify deferral.

Never create a follow-up issue or pull request without explicit approval.
Before proposing one, the work must not be achievable by mirroring a nearby
pattern; it must either present a real design choice with named alternatives
or be a genuinely large mechanical migration; it must change the review
surface, have a concrete benefit, and remain actionable. If any test fails,
fix it now or let it go—do not file an issue merely to “track” it. Bot nits
are fixed or dismissed in the current review, not converted into backlog
noise.

Do not use “non-blocking,” “post-merge follow-up,” “nice to have,”
“pre-existing,” or similar phrasing to hide a legitimate current finding.
This list is non-exhaustive: match the intent, and remember that pre-existing
problems are the point of the Boy Scout rule. State the finding and let the
user decide scope.

## CI/CD Workflows

Read the [workflow inventory and invariants](docs/agents/github-workflow.md#cicd-workflows) before editing `.github/workflows/`. In `pr.yml`, HACS and Hassfest must run before any pull-request-controlled code. The AGENTS size check may run first because it only reads this file.

## Development Commands

The minimal setup is `uv sync --group dev`; run stdio with `uv run ha-mcp` and HTTP/settings UI with `uv run ha-mcp-web`. Exact test, lint, Docker, and research commands are in the [development reference](docs/agents/development.md). Contributor setup is in [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Architecture

Tools are lazy-discovered from `tools_*.py`; shared business logic belongs in service modules; WebSocket-backed operations verify state changes; and tools wait for logical completion when possible. Read the [architecture map](docs/agents/development.md#architecture) and the [code review style guide](.gemini/styleguide.md) before structural code changes.

## Terminology: apps, not add-ons

In user- and agent-facing text, write **app (add-on)** on first mention and **app** afterwards. Identifiers require case-by-case verification; established slugs, paths, labels, API routes, and compatibility text may retain the old spelling. The exact exceptions live in the [development reference](docs/agents/development.md#terminology-apps-not-add-ons).

## Writing MCP Tools

Before adding or modifying a tool, read [`.gemini/styleguide.md`](.gemini/styleguide.md). It owns tool naming, decorator order, tags, safety annotations, `ToolError` handling, return shapes, docstrings, consolidation, module size, and progressive disclosure.

## Tool Waiting Behavior

Tools wait for completion when a reliable signal exists; query and fire-and-forget operations return immediately. The canonical categories and shared helpers are in [`.gemini/styleguide.md` → Tool Waiting Behavior](.gemini/styleguide.md#tool-waiting-behavior).

## Custom Component

Before changing `custom_components/ha_mcp_tools/` or a server dependency on it, read the [custom-component guide](docs/agents/custom-component.md). It owns the pending-version cycle, minimum-version gate, backward compatibility, two-entry command surface, and post-merge live-test requirement.
Functionality used by the embedded server must be registered on the shared
command surface available from the server entry; never leave it on the tools
entry alone. Only the privileged filesystem and YAML services remain
intentionally tools-entry-only.

## Translations

Before any translation or translatable-English change, read the [canonical locale guide](src/ha_mcp/settings_ui/locales/README.md). It owns the current language set, four-surface parity, generated projections, best-effort Klingon exception, completeness thresholds, English-source baseline, post-merge translation, and recovery. Never hand-edit generated app catalogs.

## Home Assistant App

Before changing `homeassistant-addon*/`, read the [Home Assistant app guide](docs/agents/home-assistant-apps.md). Webhook Proxy work additionally requires its scoped [`AGENTS.md`](homeassistant-addon-webhook-proxy/AGENTS.md) and always follows the dev-first, promote-only flow.

## API Research

Use `gh search code` and `gh api` against Home Assistant Core rather than cloning it only for a lookup. Commands and source-verification guidance are in the [development reference](docs/agents/development.md#api-research).

## Release Process

Semantic-release prefixes, channels, urgent releases, workflow ownership, and manual dispatch guidance are in the [GitHub workflow reference](docs/agents/github-workflow.md#releases). Component and Home Assistant app changes also follow their linked subsystem release rules.
