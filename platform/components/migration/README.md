# migration

One-time migration Jobs for the HelloDJ AWS re-platform. This component hosts
two independent one-shot flows:

1. the **clean-slate admin-bootstrap** migration (`python -m migration_job`,
   Requirement 19), documented below; and
2. the **source-credential backfill**
   (`python -m migration_job.backfill_main`, unified-oauth-and-token-watchdog
   Migration & Rollout step 3, R2.6 / R6.5) — see
   [Source-credential backfill](#source-credential-backfill).

## Clean-slate admin-bootstrap migration

One-time **clean-slate** migration Job for the HelloDJ AWS re-platform.

Under the clean-slate policy (Requirement 19) the AWS platform begins fresh. The
**only** data carried forward from the legacy platform is the
`Admin_Bootstrap_Credential`, which is seeded into the Cognito user pool so the
Platform_Owner can log in as the administrator for the first time on AWS. All
other legacy data — playback, session, playlist and configuration — is **not**
migrated; those entities are created anew in DynamoDB as the platform runs.

Requirements: 19.1, 19.2, 19.3, 19.4

## What it does

1. Loads a legacy export (local JSON file or S3 object) into `LegacyRecord`
   objects (injectable source, so the flow is testable without AWS).
2. Runs the shared pure decision function
   `hellodj_platform_logic.migration.filter_legacy` to keep **only** the
   `Admin_Bootstrap_Credential` records (R19.1, R19.2, R19.4).
3. Seeds that single credential into the Cognito user pool
   (`admin_create_user` + `admin_add_user_to_group` into the `admins` group)
   via an injectable `cognito-idp` client (R19.3). Seeding is idempotent, so
   re-running the Job is safe.
4. Runs the fresh-init step, which writes **no** legacy
   playback/session/playlist/config data — optionally probing that the fresh
   DynamoDB tables are reachable (R19.4).

## Layout

```
migration/
├── migration_job/
│   ├── __init__.py                     # Package surface
│   ├── __main__.py                     # admin-bootstrap entrypoint (python -m migration_job)
│   ├── legacy_source.py                # Injectable legacy export loaders (file / S3 / memory)
│   ├── cognito_seeder.py               # Cognito admin seeder (boto3 cognito-idp)
│   ├── fresh_init.py                   # Documented fresh-start step (no legacy data)
│   ├── job.py                          # End-to-end MigrationJob orchestration
│   ├── source_credential_mapping.py    # Pure backfill helpers (name parse + TokenState mappers)
│   ├── source_credential_backfill.py   # SourceCredentialBackfill orchestration + encrypted writer
│   └── backfill_main.py                # backfill entrypoint (python -m migration_job.backfill_main)
├── pyproject.toml
├── requirements.txt
└── README.md
```

## Usage

```bash
# As a module (Kubernetes Job command)
python -m migration_job

# Environment
#   HELLODJ_USER_POOL_ID        Cognito user pool id to seed the admin into (required).
#   HELLODJ_ADMIN_GROUP         Cognito admin group (default: admins).
#   HELLODJ_LEGACY_EXPORT_FILE  Path to a local JSON legacy export, OR
#   HELLODJ_LEGACY_EXPORT_S3    s3://bucket/key location of the JSON export.
#                               Exactly one export var is required.
#   HELLODJ_FRESH_INIT_VERIFY   "1"/"true" to probe fresh DynamoDB tables (default off).
#   AWS_REGION / AWS_DEFAULT_REGION  Region for the AWS clients.
```

### Legacy export shape

A JSON list of record objects; unknown `record_type` values are rejected:

```json
[
  {"record_type": "admin_bootstrap_credential",
   "record_id": "owner", "payload": "{\"username\": \"owner\", \"email\": \"owner@hellodj.bot\"}"},
  {"record_type": "playlist", "record_id": "p1", "payload": "..."}
]
```

Only the `admin_bootstrap_credential` entry is carried forward; everything else
is intentionally discarded (clean slate).

## Independent deployability

This component is independently buildable, versioned, and deployable (R15). It
is a one-time Job — not a long-running service — and holds no AWS dependencies
of its own beyond `boto3`; the clean-slate filter is imported from the shared
`hellodj_platform_logic` package so IaC and runtime share one source of truth.

## Source-credential backfill

One-shot, **idempotent** backfill for the unified-oauth-and-token-watchdog
feature (Migration & Rollout step 3, R2.6 / R6.5). The legacy platform stored
each guild+provider's OAuth tokens as one AWS Secrets Manager secret
(`hellodj/<stage>/guild/<guildId>/<provider>`). This flow reads those secrets
and writes one **envelope-encrypted** `SourceCredential` item per user+provider
into the `hellodj-core` DynamoDB table, keyed by the guild **owner's** Cognito
subject — the exact item a fresh connect writes — so nothing is lost when the
Secrets Manager write grant is later dropped.

### What it does

1. Lists every legacy secret under the `hellodj/<stage>/guild/` name prefix
   (paginated `list_secrets`) and parses `(guildId, provider)` from each name
   (a name that doesn't match the guild shape or names an unsupported provider
   is skipped and counted).
2. Resolves the guild's owning Cognito `sub` from the `GUILD#<gid>` / `OWNER`
   item (`data.owner_sub`, the same item `guild_admin_service` writes). A guild
   with **no resolvable owner** is skipped and counted (there is no user
   partition to write under).
3. Maps the legacy JSON to a `TokenState` using the SAME shapes the web-ui uses
   on a fresh connect (YouTube: `oauth_refresh_token` + `pot_token` /
   `pot_visitor_data` in `extra`; Spotify: refresh-token-centric; Tidal:
   status-only with a far-future expiry so the watchdog skips it).
4. Envelope-encrypts the token blob (KMS `GenerateDataKey` + AES-GCM via the
   shared `hellodj_platform_logic.token_crypto`) and **upserts** the item under
   the optimistic lock, so re-running merges rather than duplicating or
   erroring: `connected_at` is preserved, `updated_at`/`version` advance.
5. Verifies each written item by reading its plaintext status back (never
   decrypting).

Only **counts** are logged — never a token, a decrypted blob, or a secret
string.

### Usage

```bash
# As a module (one-time Kubernetes Job / ops step). Idempotent — safe to re-run.
python -m migration_job.backfill_main

# Environment
#   HELLODJ_STAGE                    Stage used to build+validate the legacy
#                                    secret prefix hellodj/<stage>/guild/ (default: beta).
#   HELLODJ_CORE_TABLE               hellodj-core table name (default: hellodj-core).
#   HELLODJ_SOURCE_CREDS_KMS_KEY_ID  Source-credentials CMK id/ARN for envelope
#                                    encryption (required).
#   AWS_REGION / AWS_DEFAULT_REGION  Region for the AWS clients.
```

The backfill's IAM needs: Secrets Manager `ListSecrets` + `GetSecretValue` on
`hellodj/<stage>/guild/*`, `hellodj-core` read/write, and KMS
`GenerateDataKey`/`Encrypt` on the source-credentials CMK. It never writes or
deletes a legacy secret.
