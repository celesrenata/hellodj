#!/usr/bin/env python3
"""One-shot Secrets Manager backup. Dumps every hellodj secret (value + metadata)
to a JSON file. Live token material — the output is gitignored."""
import json
import subprocess
import sys
import os
import datetime

out_path = sys.argv[1] if len(sys.argv) > 1 else "secrets-manager-backup.json"
region = "us-east-1"
env = dict(os.environ, AWS_PROFILE="hellodj")


def aws(*args):
    return json.loads(subprocess.check_output(
        ["aws", *args, "--region", region, "--output", "json"], env=env))


names = aws("secretsmanager", "list-secrets", "--max-results", "100",
            "--query", "SecretList[].Name")

backup = {
    "_meta": {
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "region": region,
        "account": "874927898283",
        "profile": "hellodj",
        "secret_count": len(names),
    },
    "secrets": {},
}

for name in names:
    desc = aws("secretsmanager", "describe-secret", "--secret-id", name)
    val = aws("secretsmanager", "get-secret-value", "--secret-id", name)
    entry = {
        "arn": desc.get("ARN"),
        "name": desc.get("Name"),
        "description": desc.get("Description"),
        "kms_key_id": desc.get("KmsKeyId"),
        "tags": desc.get("Tags", []),
        "rotation_enabled": desc.get("RotationEnabled", False),
        "version_id": val.get("VersionId"),
        "version_stages": val.get("VersionStages", []),
    }
    if "SecretString" in val:
        entry["secret_string"] = val["SecretString"]
    if "SecretBinary" in val:
        entry["secret_binary_b64"] = val["SecretBinary"]
    backup["secrets"][name] = entry
    print(f"backed up: {name}", file=sys.stderr)

with open(out_path, "w") as f:
    json.dump(backup, f, indent=2)
os.chmod(out_path, 0o600)
print(f"\nWrote {len(names)} secrets to {out_path}", file=sys.stderr)
