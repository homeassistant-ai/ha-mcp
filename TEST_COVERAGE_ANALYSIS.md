# Test Coverage Analysis

An analysis of unit and E2E test coverage in ha-mcp, identifying gaps and proposing improvements prioritized by risk.

## Current State

| Metric | Value |
|--------|-------|
| Source lines (src/) | ~25,700 |
| Test lines (tests/) | ~34,700 |
| Test-to-source ratio | 1.35:1 |
| Unit test files | 31 |
| E2E test files | 48 |
| Total test functions | ~954 |

The project has strong E2E coverage backed by Testcontainers (real Home Assistant instances). Unit test coverage is uneven — some modules are well-tested while others rely entirely on E2E tests or have no tests at all.

---

## Critical Gaps (No Unit Tests)

### 1. `utils/operation_manager.py` — 442 lines, 0 unit tests

**What it does:** Tracks async device operations through their lifecycle (PENDING → COMPLETED/FAILED/TIMEOUT). Manages in-memory operation storage with expiration, cleanup, and state matching.

**Why it matters:** State management bugs here cause silent operation failures — devices appear to respond but operations aren't tracked correctly. Memory leaks from uncleaned operations could degrade long-running sessions.

**What to test:**
- Operation lifecycle: create → update → complete/fail
- `process_state_change()` — matching incoming states to pending operations
- `_matches_expected_state()` — partial attribute matching logic
- Timeout marking during `cleanup_expired_operations()`
- `max_operations` limit enforcement (eviction under pressure)
- `elapsed_ms` / `is_expired` / `duration_ms` property correctness
- Concurrent operations on the same entity
- Edge case: state change arrives for non-existent operation

---

### 2. `utils/fuzzy_search.py` — 339 lines, 0 unit tests

**What it does:** Core entity search algorithm. Implements scoring with weighted factors (entity_id 0.7, friendly_name 0.8, domain 0.6), room/area keyword boosting, and match type classification (exact, partial, fuzzy).

**Why it matters:** This is the primary way AI assistants discover entities. Scoring bugs cause wrong entities to be returned, leading to controlling the wrong device. The algorithm is non-trivial and can't be adequately validated through E2E tests alone.

**What to test:**
- `_calculate_entity_score()` — scoring accuracy for known inputs
- Threshold boundary behavior (scores at 59, 60, 61 with default threshold 60)
- Match type classification (exact vs partial vs fuzzy)
- `calculate_partial_ratio()` and `calculate_token_sort_ratio()` — substring and word-order-independent matching
- `_infer_area_from_name()` — area inference from entity naming patterns
- `get_smart_suggestions()` — suggestion generation
- Empty entity list, single entity, large entity lists
- Tie-breaking when multiple entities score identically
- Special characters and unicode in queries

---

### 3. `client/websocket_client.py` — 656 lines, ~18% coverage (URL construction only)

**What it does:** WebSocket communication with Home Assistant for real-time state monitoring, event subscriptions, and command execution. Handles authentication sequences, message routing, timeouts, and reconnection.

**Existing tests:** Only URL construction (`_build_ws_url()`) is tested (118 lines of tests).

**Why it matters:** WebSocket is the backbone for device verification and real-time events. Connection, authentication, and message routing bugs can cause operations to hang or silently fail.

**What to test:**
- `connect()` authentication sequence (auth_required → send token → auth_ok/auth_invalid)
- `_process_message()` — routing result, event, and pong messages to correct handlers
- `send_command()` — command execution with timeout, pending request tracking
- `subscribe_events()` — event subscription lifecycle
- Timeout handling (30-second default)
- Error handling for malformed JSON messages
- Lock safety across operations (`_ensure_send_lock()`)

---

### 4. `client/websocket_listener.py` — 340 lines, 0 unit tests

**What it does:** Background service that listens for Home Assistant state changes and updates the operation manager. Handles reconnection, health monitoring, and periodic cleanup.

**Why it matters:** This is the glue between WebSocket events and operation tracking. If the listener silently disconnects or fails to process events, device control verification breaks.

**What to test:**
- `_handle_state_change()` — event parsing and operation manager updates
- `_connection_monitor()` — health check and reconnection logic
- `_periodic_cleanup()` — expired operation cleanup
- Service lifecycle (start/stop)
- `get_status()` statistics correctness
- `force_reconnect()` behavior
- Error recovery after connection loss

---

## Important Gaps (Partial Coverage)

### 5. `config.py` — 175 lines, validators not individually tested

**Existing tests:** CLI error message formatting (subprocess tests). No isolated validator tests.

**What to test:**
- `validate_homeassistant_url()` — URL format validation, trailing slash handling
- `validate_homeassistant_token()` — token validation, "demo" token replacement for OAuth mode
- `validate_fuzzy_threshold()` — range enforcement (0-100), boundary values
- `validate_log_level()` — valid/invalid level handling
- `validate_backup_hint()` — hint option validation
- `validate_settings()` — overall validation function
- `get_global_settings()` — singleton behavior

---

### 6. `auth/provider.py` — 831 lines, ~70% coverage

**Existing tests:** Good coverage of basic OAuth flows (909 lines of tests).

**What's missing:**
- Concurrent authorization requests with same transaction ID
- Expired authorization code cleanup (memory leak potential)
- Race conditions: multiple threads exchanging the same auth code
- `_revoke_internal()` — token mapping cleanup logic
- Token refresh with revoked refresh tokens
- Socket timeout scenarios during `_validate_ha_credentials()`

---

### 7. `utils/python_sandbox.py` — 278 lines, ~70% coverage

**Existing tests:** Good baseline with import blocking, eval/exec blocking, basic dunder access prevention.

**What's missing (security-critical):**
- Advanced dunder escapes: `__subclasses__`, `__dict__`, `__getattribute__`, `__globals__`, `__code__`, `__builtins__`
- `getattr(obj, '__class__')` — attribute-based dunder access
- Lambda abuse for code execution
- All 27 SAFE_METHODS verified (only `append`, `get` tested)
- While loops, augmented assignment (+=), set/dict comprehensions (listed as safe but untested)
- Very deep nesting, large expressions
- Unicode/special character handling in expressions

---

### 8. `client/rest_client.py` — 929 lines, core methods untested

**Existing tests:** Only `delete_automation_config()`, `delete_script_config()`, `get_script_config()`, `upsert_script_config()` are unit tested.

**What's missing:**
- `test_connection()` — connection validation
- `get_states()` — entity state retrieval
- `get_entity_state()` — single entity lookup
- `call_service()` — service invocation
- `get_history()` — historical data retrieval
- `get_logbook()` — logbook entries
- HTTP client initialization and header setup
- Timeout and retry behavior
- Response parsing error handling (malformed JSON)
- Async context manager lifecycle

---

## Nice-to-Have Gaps

### 9. `tools/smart_search.py` — 908 lines, area filtering tested only

**Existing tests:** `test_area_filter_search.py` covers registry-based area resolution. No tests for the main `smart_entity_search()` method, parallel execution, or result formatting.

### 10. `tools/device_control.py` — 687 lines, only bulk validation tested

**Existing tests:** `test_bulk_device_control.py` covers input validation for bulk operations. The main `control_device_smart()` flow — domain routing, parameter coercion, WebSocket verification — is untested at unit level.

### 11. `tools/tools_history.py` — `parse_relative_time()` untested

A pure function that converts strings like "24h", "7d", "2w", "1m" to timedeltas. Regex-based with month approximation (30 days). Easy to unit test, currently only exercised through E2E.

### 12. `tools/tools_config_dashboards.py` — 1,656 lines, largest tool module

E2E tests are comprehensive (1,346 lines), but internal functions like `_compute_config_hash()` (determinism, collision behavior) and `safe_execute()` (Python sandbox integration) have no isolated tests.

### 13. `errors.py` — 399 lines, ~80% coverage

Good structural coverage. Missing tests for less common error codes: `ENTITY_INVALID_ID`, `ENTITY_DOMAIN_MISMATCH`, `SERVICE_NOT_FOUND`, `WEBSOCKET_DISCONNECTED`, etc. (18+ untested error code paths).

---

## Recommendations

### Priority 1 — Add unit tests for untested core modules

| Module | Lines | Risk | Effort |
|--------|-------|------|--------|
| `utils/operation_manager.py` | 442 | State management bugs → silent failures | Medium |
| `utils/fuzzy_search.py` | 339 | Wrong entity selection → wrong device controlled | Medium |
| `client/websocket_client.py` (core methods) | 656 | Connection/message bugs → operations hang | High |
| `client/websocket_listener.py` | 340 | Lost events → verification breaks | Medium |

These four modules total ~1,777 lines of untested, critical infrastructure code. They handle async state tracking, entity discovery, and real-time communication — the three pillars of reliable device control.

### Priority 2 — Strengthen existing unit tests

| Module | Gap | Effort |
|--------|-----|--------|
| `config.py` validators | Individual validator isolation | Low |
| `python_sandbox.py` | Advanced escape attempts | Low |
| `rest_client.py` core methods | `test_connection`, `get_states`, `call_service` | Medium |
| `auth/provider.py` | Concurrency and edge cases | Medium |

### Priority 3 — Extract testable logic from tools

Several tool modules embed pure logic that's only validated through E2E:

| Function | Location | What It Does |
|----------|----------|-------------|
| `parse_relative_time()` | `tools_history.py` | Converts "24h"/"7d"/"1m" → timedelta |
| `_compute_config_hash()` | `tools_config_dashboards.py` | Dashboard optimistic locking hash |
| `coerce_int_param()` | `tools_utility.py` | String → int with range validation |
| `_matches_expected_state()` | `operation_manager.py` | Partial attribute matching |

These are pure functions ideal for low-effort, high-value unit tests.

### What NOT to prioritize

- **Adding unit tests for tools that have thorough E2E coverage** (helpers, automations, scripts, labels, areas, zones, groups, todos). The E2E tests for these are comprehensive and test real API behavior.
- **100% error code coverage in `errors.py`**. The structure is simple and well-tested; covering every enum variant adds marginal value.
- **Unit tests for `__main__.py`**. CLI entry point testing is better served by integration/smoke tests, which already exist.
