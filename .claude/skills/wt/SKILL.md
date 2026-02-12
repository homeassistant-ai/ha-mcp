---
name: wt
description: Create a git worktree in worktree/ subdirectory with up-to-date master
argument-hint: "<branch-name>"
allowed-tools: Bash
---

# Create Git Worktree

Create a new worktree in `worktree/<branch-name>` with branch `<branch-name>`.

## Execution

```bash
# Return to repo root
cd /home/julien/github/ha-mcp

# Pull latest master
git checkout master
git pull origin master

# Create worktree
git worktree add worktree/$ARGUMENTS -b $ARGUMENTS

# Navigate to worktree
cd worktree/$ARGUMENTS

# Confirm location and branch
echo ""
echo "✅ Worktree created and ready:"
echo "   Location: $(pwd)"
echo "   Branch: $(git rev-parse --abbrev-ref HEAD)"
echo ""
echo "You can now work in this isolated environment."
```

## Notes

- Worktree inherits `.claude/agents/` workflows
- To remove when done: `cd ../.. && git worktree remove worktree/$ARGUMENTS`
- List all worktrees: `git worktree list`
