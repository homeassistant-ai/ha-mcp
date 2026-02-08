---
name: contrib-pr-review
description: Review a contribution PR for safety, quality, and readiness. Checks for security concerns, test coverage, size appropriateness, and intent alignment. Use when reviewing external contributions. Review PRs sequentially, not in parallel.
argument-hint: "<pr-number>"
allowed-tools: Bash, Read, Grep, Glob, WebFetch
---

# Contribution PR Review

Review PR #$ARGUMENTS from external contributor for safety, quality, and readiness.

**IMPORTANT:** Review PRs one at a time. Do not launch multiple review agents in parallel to avoid resource contention.

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

**Output Security Summary:**
```
🔒 Security Assessment:
- Prompt Injection: ✅ None detected / ⚠️ FOUND - [describe]
- AGENTS.md: ✅ No changes / ⚠️ Modified - [reason to review]
- Workflows: ✅ Safe / ⚠️ NEEDS REVIEW - [concerns]
```

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
  # Convert src/ha_mcp/foo.py → tests/*/test_foo.py
  basename=$(basename "$file" .py)
  echo "Checking tests for: $file"
  find tests/ -name "test_${basename}.py" -o -name "test_*${basename}*.py" 2>/dev/null | head -3
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

# Get contribution count (from earlier command)
```

**Assess:**
- **First-time contributor** (0-2 contributions):
  - < 200 lines: ✅ Excellent size
  - 200-500 lines: ⚠️ Large for first PR - may need extra guidance
  - > 500 lines: 🔴 Too large - suggest splitting or more experienced contributor help

- **Regular contributor** (3+ contributions):
  - < 500 lines: ✅ Reasonable
  - 500-1000 lines: ⚠️ Large - ensure good test coverage
  - > 1000 lines: 🔴 Very large - suggest splitting

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

**Note:** Gemini Code Assist already provides code review. Focus on high-level concerns:

- **Consistency with codebase patterns**: Does it follow existing conventions?
- **Architecture alignment**: Does it fit the project structure?
- **Breaking changes**: Any API changes that affect users?
- **Documentation**: Are docstrings/comments appropriate?

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
- Pattern consistency: [assessment]
- Architecture fit: [assessment]
- Breaking changes: ✅ None / ⚠️ Detected - [describe]
- Documentation: [assessment]
```

## Final Review Summary

Provide a **concise summary** for the reviewer:

```
📋 PR #$ARGUMENTS Review Summary

👤 Contributor: [name] ([X] contributions)
📊 Size: [lines] lines ([appropriate/large/too large])

🔒 Security: [SAFE / NEEDS REVIEW - concerns]
🧪 Tests: [well-tested / partially tested / untested]
🎯 Intent: [clear & aligned / unclear / scope issues]
✨ Quality: [good / needs work / excellent]

**Recommendation:**
- ✅ APPROVE - Ready for merge after CI passes
- 💬 REQUEST CHANGES - [specific issues to address]
- 🤔 COMMENT - Needs discussion on [topic]
- 🔴 BLOCK - Security concerns require maintainer review

**Key Points for Reviewer:**
1. [Most important thing to check]
2. [Second concern]
3. [Nice to have improvement]

**Next Steps:**
- [ ] Enable workflows (if not enabled)
- [ ] Review security concerns (if any)
- [ ] Check test coverage
- [ ] Validate intent alignment
```

## Important Notes

- **Security first**: Always flag security concerns immediately
- **Be constructive**: Contributors are donating their time - be welcoming
- **Focus on intent**: Code quality can be iterated; intent misalignment is harder to fix
- **Consider contributor experience**: Adjust expectations based on contribution history
- **Gemini already reviewed code**: Don't duplicate detailed code review
- **When in doubt**: Err on the side of caution and request maintainer review
