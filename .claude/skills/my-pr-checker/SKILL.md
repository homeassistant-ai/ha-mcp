---
name: my-pr-checker
description: Manage your own GitHub pull requests — check CI status, inline review comments, PR-level comments, resolve review threads, fix issues, and iterate until all checks pass and threads are resolved. Use for managing your own PRs (not external contributions). Triggers on "check my PR", "check PR", "/my-pr-checker <number>".
argument-hint: "<pr-number>"
allowed-tools: Bash, Read, Edit, Write, Glob, Grep, WebFetch, WebSearch
---

# My PR Checker

Review and resolve all outstanding issues on PR #$ARGUMENTS in `homeassistant-ai/ha-mcp`.

## Step 1: Full PR Assessment

```bash
# PR overview
gh pr view "$ARGUMENTS" --repo homeassistant-ai/ha-mcp \
  --json title,body,state,reviews,statusCheckRollup,headRefName,additions,deletions,changedFiles

# CI checks
gh pr checks "$ARGUMENTS" --repo homeassistant-ai/ha-mcp

# Inline review comments
gh api repos/homeassistant-ai/ha-mcp/pulls/"$ARGUMENTS"/comments \
  --jq '.[] | {id, path, line, author: .user.login, body}'

# PR-level comments
gh api repos/homeassistant-ai/ha-mcp/issues/"$ARGUMENTS"/comments \
  --jq '.[] | {id, author: .user.login, body}'

# Unresolved review threads
gh api graphql -F pr="$ARGUMENTS" -f query='query($pr: Int!) { repository(owner:"homeassistant-ai", name:"ha-mcp") { pullRequest(number:$pr) { reviewThreads(first:100) { nodes { id isResolved path line comments(first:1) { nodes { databaseId body author { login } } } } } } } }'
```

## Step 2: Triage Comments

- **Human comments**: highest priority
- **Bot comments** (Gemini, Copilot, Codex): treat as suggestions — assess whether they prevent a bug or improve maintainability; dismiss with explanation if not

Accept if: prevents a bug, improves clarity for future maintainers, aligns with project conventions, addresses security.
Dismiss if: incorrect suggestion, reduces readability, conflicts with project patterns, already handled elsewhere.

## Step 3: Fix Code Issues

```bash
gh pr checkout "$ARGUMENTS" --repo homeassistant-ai/ha-mcp
# make changes
git add <files>
git commit -m "fix: address review feedback - [description]"
git push
```

Fix unrelated test failures encountered (document in final summary).

After each push, if the scope changed, update the PR description:
```bash
gh pr edit "$ARGUMENTS" --repo homeassistant-ai/ha-mcp --body "..."
```

## Step 4: Resolve Each Thread (both steps required)

```bash
# 1a. Reply on inline thread
gh api repos/homeassistant-ai/ha-mcp/pulls/"$ARGUMENTS"/comments/<COMMENT_ID>/replies \
  -f body="✅ Fixed in [commit]. [explanation]"
# or for dismissed:
gh api repos/homeassistant-ai/ha-mcp/pulls/"$ARGUMENTS"/comments/<COMMENT_ID>/replies \
  -f body="📝 Not addressing because [reason]."

# 1b. PR-level summary comment (when there are multiple inline threads)
gh pr review "$ARGUMENTS" --repo homeassistant-ai/ha-mcp \
  --comment --body "✅ Addressed review feedback in [commit]. [summary]"

# 2. Resolve thread via GraphQL
gh api graphql -f query='mutation($threadId: ID!) { resolveReviewThread(input: {threadId: $threadId}) { thread { id isResolved } } }' \
  -f threadId="<PRRT_...>"
```

**Why:** Unresolved threads block merging even after approval.

## Step 5: Wait and Re-check

```bash
gh pr checks "$ARGUMENTS" --repo homeassistant-ai/ha-mcp --watch
```

Repeat Steps 1–5 until:
- All CI checks green ✅
- All review threads resolved ✅
- No new blocking comments ✅

## Step 6: Final Report

If improvements were identified during review, add them to the PR description's **Future improvements** section first:
```bash
gh pr edit "$ARGUMENTS" --repo homeassistant-ai/ha-mcp --body "..."
```
(Edit the existing `## Future improvements` section, or append it if absent.)

Then post a summary comment:
```bash
gh pr comment "$ARGUMENTS" --repo homeassistant-ai/ha-mcp --body "## PR Assessment Summary

✅ **Status**: Ready for review/merge

**Choices Made:**
- [key decisions when resolving issues]

**Problems Encountered:**
- [issues faced and how resolved]
- [unrelated test failures fixed, if any]
"
```

## Special Operations

When needed:
- **Rebase**: `gh pr checkout "$ARGUMENTS" && git rebase master && git push --force-with-lease`
- **Re-create PR**: `gh pr close "$ARGUMENTS" --repo homeassistant-ai/ha-mcp` then `gh pr create --draft …` with the same branch
- **Delete comment**: `gh api -X DELETE repos/homeassistant-ai/ha-mcp/issues/comments/<id>`
- **Update title**: `gh pr edit "$ARGUMENTS" --repo homeassistant-ai/ha-mcp --title "new title"`

## Rules

- **NEVER merge automatically** unless explicitly asked
- **Be conservative** dismissing feedback — when in doubt, ask the user
- If CI keeps failing on the same issue after 3 attempts, report the blocker rather than looping
