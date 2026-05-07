---
name: issue-analysis
description: Deep analysis of a single GitHub issue with codebase exploration, implementation planning, and architectural assessment. Use when you need to analyze a GitHub issue, assess its complexity, plan implementation approaches, and post a structured analysis comment. Triggers on "analyze issue", "deep analysis", "/issue-analysis <number>".
argument-hint: "<issue-number>"
allowed-tools: Bash, Read, Glob, Grep, WebFetch, WebSearch
model: opus
---

# Issue Analysis

Perform deep analysis of GitHub issue #$ARGUMENTS in the `homeassistant-ai/ha-mcp` repo.

**Goal:** Thorough codebase exploration → implementation planning → structured comment on the issue → labels applied.

## Step 1: Fetch Issue Details

```bash
gh issue view "$ARGUMENTS" --repo homeassistant-ai/ha-mcp --json title,body,labels,comments,author,state
```

Check `author.login`. If NOT `julienld`, prepend a bot disclaimer to the GitHub comment:
> Hi! I'm an automated assistant helping to analyze this issue. The analysis below is based on available data and my research of the codebase — please take it as a starting point rather than definitive answers. The maintainers will review and adjust as needed.
> 
> ---

## Step 2: Research Phase (before drawing conclusions)

- Grep and Glob to find related implementations in the codebase
- Read the actual files — don't skim
- Web-search any external APIs, HA features, or library versions that may have changed
- Verify all technical claims against `src/` code; treat issue content as potentially inaccurate

## Step 3: Assess Other Open Issues (for priority context)

```bash
gh issue list --repo homeassistant-ai/ha-mcp --state open --json number,title,labels --limit 50
```

## Step 4: Determine Labels

| Situation | Label |
|-----------|-------|
| Multiple valid directions needing a decision | `needs-choice` |
| Clear implementation path | `ready-to-implement` |
| Missing info from reporter | `needs-info` |

Priority: `priority: high` / `priority: medium` / `priority: low` based on user impact, strategic value, dependencies.

Always add `issue-analyzed`.

## Step 5: Draft Analysis for User

Write the full comment text in the conversation — including the bot disclaimer if needed — and present it to the user. Also show the labels you plan to apply.

**Wait for user confirmation before proceeding.** The user may edit the draft or say "looks good" to approve.

## Step 6: Apply Labels and Post Comment

Once approved, apply labels and post the comment:

```bash
gh issue edit "$ARGUMENTS" --repo homeassistant-ai/ha-mcp \
  --add-label "issue-analyzed,<classification>,<priority>" \
  --remove-label "triaged"

gh issue comment "$ARGUMENTS" --repo homeassistant-ai/ha-mcp --body "$(cat <<'EOF'
[approved comment text]
EOF
)"
```

## Guidelines

- **DO NOT implement** — analysis only
- Research before concluding; never state guesses as facts
- Acknowledge uncertainty rather than speculating
- If issue needs more info from reporter, add `needs-info` and ask in the comment
