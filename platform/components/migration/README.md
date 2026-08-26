# migration

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
│   ├── __init__.py         # Package surface
│   ├── __main__.py         # main() entrypoint (python -m migration_job)
│   ├── legacy_source.py    # Injectable legacy export loaders (file / S3 / memory)
│   ├── cognito_seeder.py   # Cognito admin seeder (boto3 cognito-idp)
│   ├── fresh_init.py       # Documented fresh-start step (no legacy data)
│   └── job.py              # End-to-end MigrationJob orchestration
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
