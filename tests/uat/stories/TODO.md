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

## Sources

Stories are derived from:
- Real user workflows observed in GitHub issues (#445, #384, #320, #319, #405, etc.)
- Community discussions (#512 Agent Skills, #477 Paulus discussion, #448 community feedback)
- Research repo insights (homeassistant-ai/research: ideas, Paulus notes)
- Tool usage patterns inferred from the 80+ tool codebase
- Common HA community patterns (Reddit, forums, blog posts)
