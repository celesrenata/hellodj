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
| `token_watchdog.py` | Durable token-refresh watchdog: refreshes near-expiry source credentials on a background loop (per-item isolation, optimistic-lock multi-replica safe). Runs on a daemon thread next to the health server. |
| `watchdog_bootstrap.py` | Env-driven, degrade-safe construction of the watchdog (CoreTable + KMS + `SourceCredentialService` + `{provider: RefreshClient}`); starts it or logs "degraded: watchdog disabled". |

## Token-refresh watchdog

The orchestrator hosts the durable **token-refresh watchdog** for the
`unified-oauth-and-token-watchdog` feature. Because this container is already
standing (run loop + DynamoDB access) and survives a bot pod bounce, refresh
lives here instead of an in-bot task. Each tick enumerates near-expiry
credentials (a key-projected scan that never decrypts), refreshes each via the
matching provider `RefreshClient`, and writes the encrypted result back through
the shared `SourceCredentialService` optimistic lock — so it is idempotent and
safe across replicas. One item's failure is isolated (logged, recorded as
`refresh_status=failed`, prior blob intact) and never stops the pass or crashes
the loop. With no datastore / KMS / provider clients configured it degrades to a
no-op and the health server is unaffected.

Env: `HELLODJ_CORE_TABLE`, `HELLODJ_SOURCE_CREDS_KMS_KEY_ID`,
`TOKEN_WATCHDOG_INTERVAL`, `TOKEN_WATCHDOG_THRESHOLD`, and the provider OAuth
client id/secret pairs (`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`,
`SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET`, `TIDAL_CLIENT_ID`/`TIDAL_TOKEN_URL`).

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
