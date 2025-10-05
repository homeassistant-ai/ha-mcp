# Technical Debt & Future Improvements

This document tracks technical debt items and future enhancement ideas for the ha-mcp project.

## High Priority

### 1. Split registry.py into Separate Modules
**Issue:** `src/ha_mcp/tools/registry.py` is over 1100 lines and growing

**Proposed Structure:**
```
src/ha_mcp/tools/
├── __init__.py
├── registry.py (orchestrator only)
├── backup.py - Backup/restore tools
├── config.py - Configuration management (automations, scripts, helpers)
├── device_control.py - Device control tools
├── search.py - Entity search and discovery
└── system.py - System info, weather, energy, etc.
```

**Benefits:**
- Better code organization
- Easier to maintain and test
- Clearer separation of concerns

**Location:** `src/ha_mcp/tools/registry.py:4-11`

---

### 2. Implement Actual Backup Restore Testing
**Issue:** Current tests only validate restore parameters, don't actually test restore functionality

**Challenges:**
- Need to handle Home Assistant restart
- Must verify system state before/after restore
- Test environment must be able to recover from restore
- Need mechanism to wait for HA to come back online

**Possible Approach:**
1. Create backup of known state
2. Make changes to config
3. Restore from backup
4. Wait for HA restart
5. Verify config returned to original state

**Location:** `tests/src/e2e/workflows/convenience/test_backup.py:146-148`

---

## Medium Priority

### 3. Auto-detect BACKUP_HINT Level
**Issue:** `BACKUP_HINT=auto` currently defaults to `normal`

**Enhancement:** Implement intelligent backup hint detection based on:
- Operation type (delete vs modify)
- Entity type (can definition be fetched?)
- User's backup schedule (from HA config)
- Time since last backup
- Session context (first operation of day/session)

**Configuration:** `src/ha_mcp/config.py:53` - `backup_hint` field

---

## Low Priority / Future Ideas

### 4. WebSocket Connection Pooling
**Issue:** Each backup operation creates a new WebSocket connection

**Enhancement:** Implement connection pooling to reuse WebSocket connections across operations
- Reduce connection overhead
- Improve performance for multiple backup operations
- Add connection health checks

---

### 5. Backup Progress Reporting
**Issue:** Users don't see progress during long backup operations

**Enhancement:** Use MCP Context for progress reporting
- Report percentage complete
- Show estimated time remaining
- Update status messages during poll loop

---

### 6. Incremental Backups
**Issue:** Every backup is full backup (except database exclusion)

**Enhancement:** Support incremental backups
- Only backup changed files
- Faster backup times
- Smaller backup sizes
- Requires HA API support (may not be available)

---

## Process Improvements

### 7. Add Pre-commit Hooks
**Improvement:** Automate code quality checks
- `black` formatting
- `ruff` linting
- `mypy` type checking
- Prevent direct commits to master

---

### 8. Automated Changelog Generation
**Improvement:** Use `semantic-release` more effectively
- Auto-generate CHANGELOG.md
- Link PRs in release notes
- Include breaking change warnings

---

## Contributing

When adding new technical debt items:
1. Add them to this file
2. Reference the specific location in code (file:line)
3. Explain the issue and why it's debt
4. Propose a solution approach
5. Estimate priority (High/Medium/Low)

When resolving debt items:
1. Create a feature branch
2. Reference this document in PR description
3. Update/remove the item from this list
4. Add any new debt discovered during work
