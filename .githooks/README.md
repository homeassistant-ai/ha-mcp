# Git Hooks

This directory contains git hooks that enforce project workflows.

## Installation

### Automatic (Recommended)

```bash
# Configure git to use .githooks/ directory
git config core.hooksPath .githooks
```

### Manual

```bash
# Copy hooks to .git/hooks/
cp .githooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

## Hooks

### pre-commit

Enforces worktree-based workflow for feature branches:
- Blocks commits to `feat/*`, `fix/*`, `chore/*`, etc. from main repository
- Guides developers to use `worktree/` subdirectory
- Allows bypass with `git commit --no-verify` when needed
- Always allows commits to master/main branch

**Why enforce worktrees:**
- Keeps main repo clean
- Provides isolated environment per feature
- Worktrees inherit `.claude/agents/` workflows
- Easy cleanup with `git worktree prune`

**Example workflow:**
```bash
# Create worktree for new feature
git worktree add worktree/feat/my-feature -b feat/my-feature
cd worktree/feat/my-feature

# Work normally
git add .
git commit -m "feat: add feature"
git push

# Clean up when done
cd ../..
git worktree remove worktree/feat/my-feature
```
