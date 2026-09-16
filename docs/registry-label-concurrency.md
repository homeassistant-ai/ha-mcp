# Registry label concurrency

Home Assistant accepts complete label lists, not atomic add/remove commands.
HA-MCP serializes the fresh read, label merge, write, and existing verification
for each registry resource in one event loop. The shared lock is keyed by
registry kind and resource ID, so tool instances and clients share it while
different resources can progress independently. IDs shared by different HA
connections are conservatively serialized. Idle locks are weakly held and
removed automatically.

## Update inventory

| Path | Update semantics | Coordination |
| --- | --- | --- |
| `ha_config_set_label(areas=...)` | Read each area's labels and add one label | Hold the area lock through the write and post-write verification. |
| `ha_set_entity(labels=..., label_operation="add"/"remove")` | Read entity labels and merge/filter them | Hold the entity lock from the read through the registry update, including bulk calls. |
| `ha_set_entity(label_operation="set")` | Replace labels explicitly | Use the same entity lock; replacement can intentionally remove earlier additions. |
| `ha_set_area_or_floor(kind="area", labels=...)` | Replace labels explicitly | Use the same area lock through the existing verification. |
| Helper create/update label setters | Replace entity labels explicitly | Share the entity lock, including config-entry and fallback helper paths. |
| `ha_set_device(labels=...)` | One complete replacement; no additive operation | No client-side read-modify-write to protect. Home Assistant applies the single write. |
| `ha_set_entity(categories=...)` | Send only the requested scope assignments | Home Assistant merges scopes in its synchronous registry update; no client-side merge. |
| Label/category registry metadata and other entity/device registry fields | Send only explicitly supplied fields | No client-side label merge. |
| Template helper identity restore | Restore the saved entity ID/name, then verify | Lock both the created and saved entity IDs in sorted order, re-read after acquiring them, and hold through verification. |
| Area backup restore | Deliberately replace saved registry values | Share the area lock for the replacement write; the wider restore remains non-atomic. |

The lock does not coordinate separate processes/event loops, the Home Assistant
UI, automations, or other integrations. Home Assistant provides no conditional
label write, so this is not a distributed transaction. Caller-built replacement
lists are still replacements, even if based on an earlier read.

Failures retain their existing classification and partial-progress reporting.
There is no automatic retry: a transport failure may follow a committed write.
Exception and cancellation unwinding releases the lock, and a later call reads
fresh state. Bulk updates lock one resource at a time and remain non-atomic. Template identity
restore stops with an identity mismatch if the entity moves to a third ID while
waiting; it does not write using an unlocked identity. Locking adds no sleeps or
retries, and registry requests retain their existing timeouts.
