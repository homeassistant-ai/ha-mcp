## What does this PR do?

<!-- Brief description of your changes -->

## Type of change
- [ ] 🐛 Bug fix
- [ ] ✨ New feature
- [ ] 📚 Documentation
- [ ] 🔧 Maintenance/refactor
- [ ] 🧪 Tests only
- [ ] 💥 Breaking change

## Testing

<!-- Describe what was exercised: the tests added or changed and what they
     cover, plus any manual or agent checks. Do NOT list CI results, pass
     counts, run links, or per-lane status. Reviewers see those in the PR's
     checks, and they go stale on the next push. -->

- [ ] I have tested these changes with a LLM agent (tested directly on a live HA instance, or directly on an emulated HA instance such as the live dev environment)
- [ ] All automated tests pass (`uv run pytest`)
- [ ] Code passes lint, format, and type checks (`uv run ruff check`, `uv run ruff format --check` on changed files, `uv run mypy src/`)

## Future improvements

<!-- Use this section ONLY for genuinely out-of-scope work the user (PR author)
     has explicitly confirmed should be deferred. If nothing qualifies, DELETE
     this section entirely.

     A discovered improvement is "out of scope" when BOTH are true:
     - It touches a different subsystem, requires design decisions, or would
       meaningfully change this PR's review surface.
     - The user has confirmed deferral after being asked.

     Do NOT list:
     - Anything you can fix inline (typos, dead imports, drift, "mirror X
       parity onto Y", "migrate singular → list", sweeps of the same pattern
       across files)
     - Bot non-blocking suggestions
     - "Consider X" without acceptance criteria

     If you (the AI agent) are unsure whether a finding is out of scope, ASK
     the user rather than self-bucketing it here. See AGENTS.md §
     "Boy Scout Rule — Handling Discovered Improvements" for the full rubric. -->

## Checklist
- [ ] I have updated documentation if needed
