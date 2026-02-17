# Story Backlog

Weighted backlog of user acceptance stories. Weight = importance (1-5).
Stories with weight >= 4 are candidates for the core set.
Lower-weight stories are good for comprehensive coverage.

## Implemented (in catalog/)

| ID | Weight | Category | Story |
|----|--------|----------|-------|
| s01 | 5 | automation | Create sunset/sunrise lights automation |
| s02 | 5 | automation | Create motion-activated light with timeout |
| s03 | 5 | troubleshoot | Debug why automation didn't fire |
| s04 | 4 | dashboard | Create room overview dashboard |
| s05 | 4 | entity | Discover and explore available entities |
| s06 | 4 | script | Create multi-action goodnight routine |
| s07 | 4 | automation | Update existing automation (add condition) |
| s08 | 4 | automation | Create complex multi-condition automation |
| s09 | 3 | helper | Create vacation mode toggle + automation |
| s10 | 3 | organization | Create areas and organize entities |
| s11 | 3 | troubleshoot | Analyze entity history and patterns |
| s12 | 3 | organization | Create and assign labels to entities |

## Backlog (not yet implemented)

### Weight 4 (High Priority)

| Weight | Category | Story | Notes |
|--------|----------|-------|-------|
| 4 | automation | Migrate automation from template to native syntax | From research: #445 template overuse |
| 4 | script | Debug a failing script via traces | Like s03 but for scripts |
| 4 | dashboard | Update existing dashboard - add/remove cards | Read-modify-write pattern |
| 4 | automation | Create automation from blueprint | Blueprint-based workflows |
| 4 | entity | Bulk entity state check and report | "Are all doors locked? All lights off?" |

### Weight 3 (Medium Priority)

| Weight | Category | Story | Notes |
|--------|----------|-------|-------|
| 3 | calendar | Create recurring calendar events | Calendar CRUD |
| 3 | helper | Create counter + automation to track events | Counter helper lifecycle |
| 3 | script | Create script with choose/if blocks | Conditional script logic |
| 3 | dashboard | Create energy monitoring dashboard | Multiple entity history cards |
| 3 | entity | Find entities without areas and organize them | Organization + discovery |
| 3 | automation | Create automation with wait_for_trigger | Advanced automation pattern |
| 3 | organization | Set up floor hierarchy (floor → area → device) | Floor management |
| 3 | troubleshoot | Check logbook for unexpected state changes | "What changed while I was away?" |
| 3 | system | Check for available updates and report status | System maintenance |

### Weight 2 (Lower Priority)

| Weight | Category | Story | Notes |
|--------|----------|-------|-------|
| 2 | organization | Create and manage entity groups | Group CRUD |
| 2 | automation | Disable/enable automations in bulk | Batch operations |
| 2 | entity | Rename entities with better names | Entity registry updates |
| 2 | helper | Create input_select for mode switching | Mode-based automation |
| 2 | todo | Create and manage a todo list | Todo CRUD |
| 2 | script | Create script with delay and notifications | Timed sequences |
| 2 | dashboard | Find and update a specific card on dashboard | Card search + update |
| 2 | system | Generate a bug report | Bug report tool |
| 2 | zone | Create and manage zones | Geofencing setup |

### Weight 1 (Nice to Have)

| Weight | Category | Story | Notes |
|--------|----------|-------|-------|
| 1 | entity | Check camera image | Camera tool |
| 1 | addon | List and check add-on status | Add-on management |
| 1 | integration | Set up a config entry flow | Integration config |
| 1 | system | Check HA system info and config | System diagnostics |

## Results Tracking

Results are appended to a JSONL file after each run. Each line records one story result:

```jsonl
{"sha":"8b521d4","version":"v6.6.1","branch":"v6.6.1","timestamp":"2026-02-13T10:00:00+00:00","agent":"gemini","story":"s01","category":"automation","weight":5,"passed":true,"test_duration_ms":45000,"total_duration_ms":62000,"tool_calls":5,"tool_failures":0,"turns":3,"session_file":"/home/user/.gemini/tmp/.../session-*.json","tokens_input":145000,"tokens_output":650,"tokens_cached":110000,"tokens_thoughts":550}
```

- `sha`: exact commit for reproducibility
- `version`: human-readable from `git describe --tags` (e.g., `v6.6.1` or `v6.6.1-5-gabc1234`)
- `branch`: the `--branch` flag value (tag/branch installed via uvx), or `null` for local code
- `test_duration_ms`: duration of the test phase only
- `total_duration_ms`: includes setup + test (total wall time)
- `session_file`: path to agent session file (for white-box analysis)
- `tokens_input` / `tokens_output` / `tokens_cached` / `tokens_thoughts`: token usage from session file
- **Billable tokens** = `tokens_input - tokens_cached + tokens_output + tokens_thoughts` (cached tokens are free)

**Local runs:** `local/uat-results.jsonl` (gitignored)
**Future CI:** Orphan branch `uat-results` with `history.jsonl`

**Running with results tracking:**
```bash
# Run against current local code
python tests/uat/stories/run_story.py --all --agents gemini

# Run against a specific release tag
python tests/uat/stories/run_story.py --all --agents gemini --branch v6.6.1

# Custom results file location
python tests/uat/stories/run_story.py --all --agents gemini --results-file /tmp/results.jsonl
```

**Querying results:**
```bash
# All results for a specific version
jq 'select(.branch == "v6.6.1")' local/uat-results.jsonl

# Pass rate by story
jq -s 'group_by(.story) | .[] | {story: .[0].story, pass_rate: ([.[] | select(.passed)] | length) / length}' local/uat-results.jsonl

# Compare two runs
diff <(jq -s 'sort_by(.story) | .[] | {s:.story,p:.passed}' run1.jsonl) <(jq -s 'sort_by(.story) | .[] | {s:.story,p:.passed}' run2.jsonl)
```

## Sources

Stories are derived from:
- Real user workflows observed in GitHub issues (#445, #384, #320, #319, #405, etc.)
- Community discussions (#512 Agent Skills, #477 Paulus discussion, #448 community feedback)
- Research repo insights (homeassistant-ai/research: ideas, Paulus notes)
- Tool usage patterns inferred from the 80+ tool codebase
- Common HA community patterns (Reddit, forums, blog posts)
