# Manual Test Scripts

This directory contains manual test scripts for stress testing and fuzzing operations that are difficult to automate in the standard E2E test suite.

## Label Operations Fuzzer

### Purpose

The `fuzz_label_operations.py` script is designed to reproduce Issue #396 where 5+ rapid label operations would corrupt the entity registry. It performs hundreds of rapid operations and validates registry health.

### Setup

**Option 1: Local Docker Test Environment**

```bash
cd tests
uv run hamcp-test-env --no-interactive
```

This starts a local Home Assistant instance for testing.

**Option 2: Use Existing Home Assistant**

Use any existing Home Assistant instance (local or remote) and create a long-lived access token.

### Usage

**Set environment variables:**

```bash
export HOMEASSISTANT_URL=http://localhost:8123
export HOMEASSISTANT_TOKEN=your_long_lived_access_token
```

**Basic usage** (100 operations, 5 entities, 10 labels):

```bash
uv run python tests/manual/fuzz_label_operations.py
```

**Stress test** (1000 operations):

```bash
uv run python tests/manual/fuzz_label_operations.py --operations 1000
```

**Custom configuration:**

```bash
uv run python tests/manual/fuzz_label_operations.py \
  --operations 500 \
  --entities 10 \
  --labels 20
```

### Command-line Options

| Option | Default | Description |
|--------|---------|-------------|
| `--operations` | 100 | Number of label operations to perform |
| `--entities` | 5 | Number of entities to use for testing |
| `--labels` | 10 | Number of test labels to create |
| `--url` | `$HOMEASSISTANT_URL` | Home Assistant URL |
| `--token` | `$HOMEASSISTANT_TOKEN` | Long-lived access token |

### What It Tests

The fuzzer performs various label operations to stress test the system:

1. **Add operations**: Append labels one by one (preserves existing)
2. **Remove operations**: Remove specific labels (preserves remaining)
3. **Set operations**: Replace all labels (overwrites)
4. **Rapid cycles**: Quick add/remove/add/remove sequences
5. **Bulk operations**: Set many labels at once
6. **Clear and rebuild**: Remove all labels then add back

**Health checks every 20 operations:**
- Can list all labels
- Can get entity registry entries
- Can perform update operations

### Exit Codes

- `0`: No corruption detected (test passed)
- `1`: Corruption detected or errors encountered (test failed)

### Expected Results

**With fixed implementation (`ha_manage_entity_labels`):**
- Should complete all operations successfully
- No corruption detected even after hundreds of operations
- All health checks pass
- Registry remains accessible

**With old implementation (`ha_assign_label`):**
- Would likely show corruption after 5-10 operations
- Health checks would fail
- Cannot list labels or access entity registry
- UI would become inaccessible

### Example Output

```
================================================================================
LABEL OPERATION FUZZING - Issue #396 Regression Test
================================================================================
Target: http://localhost:8123
Operations: 100
Entities: 5
Labels: 10
================================================================================
Creating 10 test labels...
  Created label 1/10: fuzz_test_label_1
  Created label 2/10: fuzz_test_label_2
  ...

Finding 5 test entities...
  Using entity 1/5: light.bed_light
  Using entity 2/5: light.ceiling_lights
  ...

Starting fuzzing operations...
  [   1/100] add    on light.bed_light           with 1 label(s) ✅
  [   2/100] add    on light.ceiling_lights      with 1 label(s) ✅
  ...
  [  20/100] remove on light.kitchen_lights      with 2 label(s) ✅

  Checking registry health after 20 operations...
  Registry health check passed ✅

  [100/100] add    on light.ceiling_lights      with 1 label(s) ✅

================================================================================
FUZZING COMPLETE
================================================================================
Total operations: 100
Successful: 100
Failed: 0
Time elapsed: 15.32s
Operations/sec: 6.53

Performing final comprehensive health check...
✅ FINAL HEALTH CHECK PASSED - NO CORRUPTION DETECTED
================================================================================
```

### Troubleshooting

**Connection errors:**
```bash
# Verify Home Assistant is running
curl $HOMEASSISTANT_URL/api/

# Check token is valid
curl -H "Authorization: Bearer $TOKEN" $HOMEASSISTANT_URL/api/states
```

**Not enough entities:**
- The script needs light entities
- If you have fewer than requested, it will adjust automatically
- Or add more demo lights to your test environment

**Operation failures:**
- Some operations may fail if entities don't exist
- This is tracked but doesn't indicate corruption
- Corruption is detected by health check failures, not individual operation failures

### Relation to E2E Tests

The comprehensive E2E test `test_label_operations.py::TestRegressionIssue396` provides similar validation in an automated test environment. This manual fuzzing script allows for:

- Higher operation counts (100-1000+ operations)
- Longer stress testing
- Testing against production instances
- Custom scenarios and configurations

### Integration with CI/CD

To integrate into CI pipelines:

```yaml
- name: Fuzz label operations
  run: |
    export HOMEASSISTANT_URL=http://localhost:8123
    export HOMEASSISTANT_TOKEN=${{ secrets.HA_TEST_TOKEN }}
    uv run python tests/manual/fuzz_label_operations.py --operations 500
```

The script will exit with code 1 if corruption is detected, failing the build.
