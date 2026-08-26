# playback-orchestrator

The **playback-orchestrator** is the HelloDJ platform component responsible for
turning a raw play request into a routed, filtered, persisted playback action.
It is the **single writer** for session and queue state on the DynamoDB
`hellodj-session` hot table (DAX-fronted), keeping session/queue mutations
serialized behind an optimistic lock.

It is packaged as an independently deployable, independently versioned unit
(Requirement 15) and imports the shared decision/data-access logic from
`hellodj_platform_logic` so infrastructure and runtime share one source of
truth.

## Modules

| Module | Responsibility |
|--------|----------------|
| `classifier.py` | Pure content classification (audio / video / radio) from a query, mode, or attachment MIME type. |
| `content_filter.py` | Per-guild content filtering rules (artist / track / domain / keyword). |
| `user_bans.py` | Per-guild playback ban list. |
| `persistence.py` | Unified queue/session persistence — the **single writer** to `hellodj-session` via `data_access.SessionTable.put_state` (optimistic lock, DAX hot path). |
| `router.py` | Routes a play request through ban check → classification → content filter → persistence. |

## Requirements covered

- **6.1** multi-source playback routing preserved
- **6.4** unified playback system across sources
- **7.4 / 7.5** search-cache and session/queue served from DynamoDB hot path
- **15.1 / 15.3** independently deployable and versioned component

## Running the tests

```bash
# Make the shared package importable, then run the component tests.
PYTHONPATH="$(git rev-parse --show-toplevel)/platform/components" \
    pytest components/playback-orchestrator/tests -q
```
