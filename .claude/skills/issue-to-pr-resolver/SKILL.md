---
name: issue-to-pr-resolver
description: Implement a GitHub issue end-to-end — create a worktree branch, implement the feature with tests, create a draft PR, then iteratively resolve all CI failures and review comments until the PR is clean. Use when you need to fully implement a GitHub issue from start to merge-ready. Triggers on "implement issue", "resolve issue", "/issue-to-pr-resolver <number>".
argument-hint: "<issue-number>"
allowed-tools: Bash, Read, Edit, Write, Glob, Grep, WebFetch, WebSearch
---

# Issue-to-PR Resolver

Implement GitHub issue #$ARGUMENTS in `homeassistant-ai/ha-mcp` end-to-end.

## Phase 1: Setup

**Read the issue:**
```bash
gh issue view "$ARGUMENTS" --repo homeassistant-ai/ha-mcp --json title,body,labels,comments,author
```

**Create a worktree from repo root:**
```bash
cd "$(git rev-parse --show-toplevel)"
git checkout master && git pull origin master
git worktree add "worktree/issue-$ARGUMENTS" -b "feature/issue-$ARGUMENTS"
git -C "worktree/issue-$ARGUMENTS" submodule update --init --recursive
cd "worktree/issue-$ARGUMENTS"
```

## Phase 2: Implement

- Analyze codebase structure and patterns before writing code
- Follow project conventions (see `AGENTS.md` for patterns, naming, error handling)
- Write tests — all new MCP tools in `src/ha_mcp/tools/` need E2E tests
- Run tests locally: `cd tests && uv run pytest src/e2e/ -n2 --dist loadscope -v --tb=short`
- Make atomic, well-described commits using conventional commit prefixes

**Philosophy:** Work autonomously. Don't ask about every small decision. Fix unrelated test failures encountered. Document all choices for the final summary.

If a non-obvious choice has significant consequences, create two mutually exclusive PRs (one per approach) and let the user choose.

## Phase 3: Create PR

```bash
git push -u origin "feature/issue-$ARGUMENTS"
PR_NUMBER=$(gh pr create --draft \
  --repo homeassistant-ai/ha-mcp \
  --title "<descriptive title>" \
  --body "Closes #$ARGUMENTS

## What does this PR do?
[description]

## Future improvements
<!-- Out-of-scope improvements noticed during implementation -->
" | grep -oE '[0-9]+$')
```

Wait for CI:
```bash
gh pr checks "$PR_NUMBER" --repo homeassistant-ai/ha-mcp --watch
```

Before marking ready (`gh pr ready`), update the PR description to reflect all changes made.

## Phase 4: Resolution Loop

Repeat until all checks green and no unresolved threads:

**Check for issues:**
```bash
gh pr checks "$PR_NUMBER" --repo homeassistant-ai/ha-mcp
gh api repos/homeassistant-ai/ha-mcp/pulls/"$PR_NUMBER"/comments \
  --jq '.[] | {id, path, line, author: .user.login, body}'
gh api graphql -F pr="$PR_NUMBER" -f query='query($pr: Int!) { repository(owner:"homeassistant-ai", name:"ha-mcp") { pullRequest(number:$pr) { reviewThreads(first:100) { nodes { id isResolved comments(first:1) { nodes { databaseId body } } } } } } }'
```

**Resolve each comment (both steps required):**
```bash
# 1. Reply on the inline thread
gh api repos/homeassistant-ai/ha-mcp/pulls/"$PR_NUMBER"/comments/<COMMENT_ID>/replies \
  -f body="✅ Fixed in [commit]. [explanation]"
# or: -f body="📝 Not addressing because [reason]."

# 2. Resolve the thread via GraphQL
gh api graphql -f query='mutation($threadId: ID!) { resolveReviewThread(input: {threadId: $threadId}) { thread { id isResolved } } }' \
  -f threadId="<PRRT_...>"
```

**After pushing fixes**, wait and re-check:
```bash
gh pr checks "$PR_NUMBER" --repo homeassistant-ai/ha-mcp --watch
```

## Phase 5: Final Report

Once all checks pass and all threads resolved:

```bash
gh pr comment "$PR_NUMBER" --repo homeassistant-ai/ha-mcp --body "## Implementation Summary

**Choices Made:**
- [key technical decisions with rationale]

**Problems Encountered:**
- [issues faced and how resolved]
- [unrelated test failures fixed, if any]
"
```

Report to user: PR number, status, key choices.

## Rules

- **Never commit to master** — always work in the worktree
- **Always create PRs as draft** — never mark ready without user request
- Maximum 5 resolution iterations before reporting blockers
- **Discovered improvements**: fix small things inline; surface mid-sized ones to user before pushing; document large/unrelated ones in the PR description's **Future improvements** section — never open a separate improvement PR without explicit user approval
