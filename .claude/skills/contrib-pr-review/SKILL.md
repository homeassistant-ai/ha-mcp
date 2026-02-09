---
name: contrib-pr-review
description: Review a contribution PR for safety, quality, and readiness. Checks for security concerns, test coverage, size appropriateness, and intent alignment. Use when reviewing external contributions.
argument-hint: "<pr-number>"
allowed-tools: Bash, Read, Grep, Glob, WebFetch
---

# Contribution PR Review

Review PR #$ARGUMENTS from external contributor for safety, quality, and readiness.

## Context

**PR Metadata:**
```
!`gh pr view $ARGUMENTS --repo homeassistant-ai/ha-mcp --json author,additions,deletions,files,commits,closingIssuesReferences,isDraft,reviews,url,title,body`
```

**Contributor Stats:**
```
!`gh api /repos/homeassistant-ai/ha-mcp/pulls/$ARGUMENTS --jq '{author: .user.login, user_id: .user.id}' | jq -r '.author' | xargs -I {} gh api /repos/homeassistant-ai/ha-mcp/contributors --jq '.[] | select(.login == "{}") | {login: .login, contributions: .contributions}'`
```

**Files Changed:**
```
!`gh api /repos/homeassistant-ai/ha-mcp/pulls/$ARGUMENTS/files --jq '.[] | {filename: .filename, status: .status, additions: .additions, deletions: .deletions, changes: .changes, patch: .patch}' | head -50`
```

## Review Protocol

### 1. Security Assessment (CRITICAL - Do First)

**Check for dangerous changes:**

```bash
# Get full diff
gh pr diff $ARGUMENTS --repo homeassistant-ai/ha-mcp > /tmp/pr_$ARGUMENTS.diff

# Check for sensitive file changes
gh api /repos/homeassistant-ai/ha-mcp/pulls/$ARGUMENTS/files --jq '.[].filename' | grep -E '(AGENTS\.md|CLAUDE\.md|\.github/|\.claude/)'
```

**Assess each category:**

- **Prompt Injection Risks**:
  - Search diff for suspicious patterns: user input → prompts/tools/descriptions
  - Check for: `f"..."`, string interpolation in tool descriptions, eval/exec, unescaped user content
  - **Flag immediately if found** - requires maintainer review

- **AGENTS.md/CLAUDE.md Changes**:
  - Are changes necessary for the PR's purpose?
  - Do they add backdoors, change security policies, or modify review processes?
  - **Warn reviewer** if changes seem unrelated to PR intent

- **.github/ Workflow Changes**:
  - Are workflow files modified?
  - Do they add secrets access, change permissions, or execute untrusted code?
  - **Critical**: Check for `pull_request_target` (runs in base repo context - dangerous)
  - **Block if suspicious** - maintainer must review

**Internal Assessment (do not include in PR comment unless issues found):**

If security issues found:
- Immediately comment on PR with specific concerns
- Do NOT approve until maintainer reviews
- Example: "⚠️ This PR modifies workflow files with secrets access. Needs maintainer review before merge."

### 2. Enable Workflows (If Safe)

If security assessment passes and PR has workflow changes or new workflows:

```bash
# Check current workflow status
gh api /repos/homeassistant-ai/ha-mcp/pulls/$ARGUMENTS/requested_reviewers

# Enable workflows if not enabled (requires WRITE permission)
# This command may fail if already enabled - that's OK
gh api -X PUT /repos/homeassistant-ai/ha-mcp/actions/workflows/pr.yml/enable 2>/dev/null || echo "Workflows already enabled or no permission"
```

### 3. Test Coverage Assessment

**Pre-existing tests** (easier review if modified code is already tested):

```bash
# For each modified source file, check if tests exist
gh api /repos/homeassistant-ai/ha-mcp/pulls/$ARGUMENTS/files --jq '.[] | select(.filename | startswith("src/")) | .filename' | while read file; do
  basename=$(basename "$file" .py)
  echo "Checking tests for: $file"

  # Method 1: Look for test files by naming convention
  find tests/ -name "test_${basename}.py" -o -name "test_*${basename}*.py" 2>/dev/null | head -3

  # Method 2: Grep for function/class names from the modified file
  # Extract function/class names and search for them in tests
  grep -E '^(def|class|async def) [a-zA-Z_]' "$file" 2>/dev/null | head -5 | while read line; do
    name=$(echo "$line" | sed -E 's/.*(def|class) ([a-zA-Z_][a-zA-Z0-9_]*).*/\2/')
    if [ -n "$name" ]; then
      grep -r "$name" tests/ 2>/dev/null | head -1
    fi
  done
done
```

**New tests added**:

```bash
# Check if PR adds or modifies tests
gh api /repos/homeassistant-ai/ha-mcp/pulls/$ARGUMENTS/files --jq '.[] | select(.filename | startswith("tests/")) | {filename: .filename, status: .status, additions: .additions}'
```

**Output Test Summary:**
```
🧪 Test Coverage:
- Pre-existing tests: ✅ Modified code has tests / ⚠️ No tests for modified code
- New tests: ✅ PR adds X test files / ⚠️ No new tests
- Assessment: [Easy/Medium/Hard to review based on test coverage]
```

### 4. PR Size & Contributor Experience

**Calculate PR size and assess appropriateness:**

```bash
# From metadata: additions + deletions
total_lines=$(gh pr view $ARGUMENTS --repo homeassistant-ai/ha-mcp --json additions,deletions --jq '.additions + .deletions')
echo "Total lines changed: $total_lines"

# Get contributor experience
author=$(gh pr view $ARGUMENTS --repo homeassistant-ai/ha-mcp --json author --jq -r '.author.login')

# Check 1: Contributions to this project
project_contributions=$(gh api /repos/homeassistant-ai/ha-mcp/contributors --jq ".[] | select(.login == \"$author\") | .contributions" || echo "0")

# Check 2: Total GitHub commits (overall experience)
total_commits=$(gh api /users/$author --jq '.public_repos + .total_private_repos' 2>/dev/null || echo "unknown")

echo "Contributor: $author"
echo "Project contributions: $project_contributions"
echo "GitHub experience: $total_commits repos"
```

**Assess:**
- **First-time to project** (0-2 project contributions):
  - Check overall GitHub experience (repos, total commits)
  - < 200 lines: ✅ Excellent size
  - 200-500 lines: ⚠️ Large for first PR - may need extra guidance
  - > 500 lines: 🔴 Too large - suggest splitting

- **Regular contributor** (3+ project contributions):
  - < 500 lines: ✅ Reasonable
  - 500-1000 lines: ⚠️ Large - ensure good test coverage
  - > 1000 lines: 🔴 Very large - suggest splitting

- **Experienced GitHub user** (many repos/commits overall):
  - Adjust expectations - they may be new to this project but experienced overall

**Output Size Summary:**
```
📏 PR Size:
- Lines changed: [total]
- Contributor: [first-time / regular] ([X] contributions)
- Assessment: [size appropriateness]
```

### 5. Intent & Issue Linkage

**Check linked issues:**

```bash
# From metadata: closingIssuesReferences
gh pr view $ARGUMENTS --repo homeassistant-ai/ha-mcp --json closingIssuesReferences --jq '.closingIssuesReferences[] | {number: .number, title: .title}'
```

**If issue linked:**
- Read issue to understand expected outcome
- Compare PR changes to issue requirements
- **Does PR solve the issue?** Check:
  - All requirements addressed
  - No scope creep (extra features not requested)
  - Solution approach aligns with any discussed approaches in issue

**If no issue linked:**
- **Is this a bug fix?** Should reference issue
- **Is this a feature?** Should have issue for discussion
- **Is this a typo/docs?** OK without issue
- **Recommend** creating issue for tracking if it's a substantial change

**Output Intent Summary:**
```
🎯 Intent & Linkage:
- Linked issue: #X "title" / ⚠️ No issue linked
- Solves issue: ✅ Fully addresses requirements / ⚠️ Partial / ❌ Doesn't match
- Scope: ✅ Focused / ⚠️ Scope creep detected
```

### 6. Code Quality Overview

**Note:** Gemini Code Assist provides automated code review on all PRs. This step focuses on what Gemini cannot assess:

- **Architecture alignment**: Does it fit the project structure? (service layer usage, etc.)
- **Breaking changes**: Does it remove functionality without replacement? (Tool consolidation/refactoring is NOT breaking)
- **Repo-specific patterns**: Context engineering, progressive disclosure, MCP-specific conventions

**Breaking change assessment:**
- ✅ NOT Breaking: Tool consolidation, refactoring, parameter changes with same outcome achievable
- ⚠️ BREAKING: Removes functionality with no alternative, makes previously possible actions impossible

**Quick checks:**

```bash
# Check if ruff/mypy would complain (from workflow logs if available)
gh pr checks $ARGUMENTS --repo homeassistant-ai/ha-mcp | grep -E "(ruff|mypy|lint)"

# Check for common issues in diff
grep -E "(TODO|FIXME|XXX|HACK)" /tmp/pr_$ARGUMENTS.diff
```

**Output Quality Summary:**
```
✨ Code Quality:
- Architecture fit: [assessment - service layer, context engineering]
- Breaking changes: ✅ None / ⚠️ Detected - [describe what's genuinely lost]
- Gemini reviews: [check if Gemini flagged anything critical]
```

## Final Review Summary

After completing the analysis, draft a comment for the PR following these guidelines:

**Comment Length:**
- **Good to merge:** 10-15 lines
- **Changes needed:** Max 25 lines

**Style:**
- No emojis
- Markdown formatting OK (bold, lists, code blocks)
- Present inline in chat (not in a file)
- Always ask user before posting

**Structure for "Good to Merge" (10-15 lines):**
```
[Positive opening line about the contribution]

[1-2 sentences on what works well - focus on functionality, tests, architecture]

[Any minor suggestions or notes - optional, technical only]

[Closing line about readiness to merge]
```

**Note:** Do NOT mention security assessment in comment unless issues were found. Security checks are internal.

**Structure for "Changes Needed" (max 25 lines):**
```
[Positive opening line acknowledging the work]

[Brief summary of the issue being solved]

**[Concern 1]:**
[1-2 lines explanation + suggestion - focus on: tests, functionality, architecture, breaking changes]

**[Concern 2]:** (if applicable)
[1-2 lines explanation + suggestion]

**[Concern 3]:** (if applicable)
[1-2 lines explanation + suggestion]

[Closing line about next steps]
```

**Note:** Security concerns should be raised immediately when found, not in final structured comment.

**Example - Good to Merge:**
```
Great work on [feature/fix]. [Performance/quality metric] is impressive.

The implementation follows existing patterns and the [specific aspect] is well-designed. [Optional: Minor note about something noticed].

Ready to merge once CI passes.
```

**Example - Changes Needed:**
```
Thanks for tackling [problem]. [Metric/impact] shows this addresses a real need.

**Test coverage:**
Missing tests for the new [feature]. Please add at least one E2E test validating [behavior]. Performance tests not required.

**[Second concern if applicable]:**
[Brief explanation and request]

Once [change 1] and [change 2] are addressed, this should be good to merge.
```

## Important Notes

- **Security is checked, not publicized**: Always check security (step 1), but only mention in comment if issues found
- **Be constructive**: Contributors are donating their time - be welcoming
- **Focus on intent**: Code quality can be iterated; intent misalignment is harder to fix
- **Consider contributor experience**: Adjust expectations based on contribution history
- **Gemini already reviewed code**: Don't duplicate detailed code review
- **When in doubt**: Err on the side of caution and request maintainer review
