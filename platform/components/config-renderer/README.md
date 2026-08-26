# config-renderer

Renders the complete Lavalink `application.yml` for the HelloDJ AWS platform.

This component replaces the legacy SQLite-backed renderer
(`bot/render_lavalink_config.py`). Credentials come from **AWS Secrets Manager**
and non-secret configuration comes from **DynamoDB** (the `hellodj-core` single
table). There is **no SQLite** and no local credential database.

It is designed to run as an **init container / pre-deploy Job**: it renders the
config to a target path (default `/out/application.yml`) that the `lavalink`
container mounts read-only, then exits.

Requirements: 6.1, 7.3, 15.1

## Layout

```
config-renderer/
├── config_renderer/
│   ├── __init__.py        # Package surface
│   ├── __main__.py        # main() entrypoint (python -m config_renderer)
│   ├── model.py           # LavalinkCredentials / LavalinkSettings dataclasses
│   ├── secrets_source.py  # Secrets Manager reader (boto3)
│   ├── config_source.py   # DynamoDB config reader (CoreTable)
│   └── renderer.py        # Pure YAML rendering of application.yml
├── tests/
│   ├── test_model.py
│   ├── test_renderer.py
│   └── test_sources.py    # moto-backed Secrets Manager + DynamoDB
├── pyproject.toml
└── requirements.txt
```

## Usage

```bash
# As a module (init container / Job command)
python -m config_renderer /out/application.yml

# Environment
#   HELLODJ_LAVALINK_SECRET_ID   Secrets Manager secret id/ARN (JSON blob).
#   HELLODJ_CORE_TABLE           DynamoDB core table name (default hellodj-core).
#   AWS_REGION / AWS_DEFAULT_REGION  Region for the AWS clients.
```

The rendered config preserves the YouTube client cascade
(`TV`, `TVHTML5_SIMPLY`, `ANDROID_VR`, `MUSIC`, `WEB`) and the LavaSrc provider
order (SoundCloud first, then YouTube ISRC/text) from the platform architecture.
