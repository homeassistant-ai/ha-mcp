---
name: contributors-update
description: Find merged PR authors missing from README and update the contributors list after approval
---

# Contributors Update

## Workflow

### 1. Ensure an up-to-date worktree

Return to the main repository, update `master`, and create a dedicated worktree:

```bash
cd "$(git rev-parse --show-toplevel)"
git checkout master
git pull origin master
git status --short
git worktree add worktree/contributors-update -b contributors-update
cd worktree/contributors-update
```

If the branch or worktree already exists, inspect it and reuse it only when it belongs to this workflow. Never commit directly to `master` or `main`.

### 2. Find the cutoff date

Look for the most recent commit with the marker `[contributors-updated]` in the message:

```bash
git log --all --oneline --grep="\[contributors-updated\]" | head -5
```

**If a marker commit is found:**
- Get its date: `git show <hash> --format="%ci" -s`
- Cutoff = that date **minus 1 week** (as overlap margin)

**If no marker commit is found:**
- Cutoff = today minus 2 months

### 3. List merged PRs since the cutoff date

```bash
gh pr list --repo homeassistant-ai/ha-mcp --state merged --limit 200 \
  --json number,title,author,mergedAt \
  --jq '.[] | select(.mergedAt > "YYYY-MM-DDT00:00:00Z") | "\(.number) \(.author.login) \(.title)"'
```

Replace `YYYY-MM-DDT00:00:00Z` with the computed cutoff date in ISO 8601 format.

### 4. Identify new contributors

Read the current README.md `### Contributors` and `### Maintainers` sections to get all existing handles.

Filter PR authors, excluding:
- Bot accounts (`github-actions`, `dependabot`, `gemini-code-assist`, `copilot`, etc.)
- Existing maintainers and contributors already in the README
- The repo owner (`julienld`)

For each new contributor, look at their merged PR(s) to write a concise one-line description. Use the PR title and description for context.

### 5. Preview and confirm

Show the proposed additions in README format:

```text
New contributors to add:
- **[@username](https://github.com/username)** — Brief description of contribution.
```

**Ask the user:** "Does this look correct? Should I add these to README.md?"

Wait for explicit approval before proceeding.

### 6. Apply and commit after approval

Insert new entries at the end of the `### Contributors` list, just before the `---` separator line.

README format to match:

```markdown
- **[@username](https://github.com/username)** — One-line description of contribution.
```

Keep descriptions factual and concise — what they added or fixed, not praise.

Commit with the marker in the message:

```bash
git add README.md
git commit -m "docs: update contributors list [contributors-updated]"
```

Before pushing or creating a PR, ask the user for explicit permission. When approved, push the worktree branch and create a draft PR:

```bash
git push -u origin contributors-update
gh pr create --draft --base master --head contributors-update
```

Do not remove the worktree until the user asks or the branch is no longer needed.
