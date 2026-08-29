#!/usr/bin/env python3
"""Assemble the bot-app-pool JSON from a local ``label: token`` file.

Reads a token file (one ``<label>: <bot_token>`` per line, blank lines
ignored), derives each application's ``client_id`` from the token itself (the
Discord bot token's first dot-separated segment is the base64url-encoded
application id), and emits the Secrets Manager pool JSON:

    [{"label", "client_id", "client_secret", "bot_token"}, ...]

The script NEVER prints a token or writes one to stdout — it writes the pool
JSON to an output file you name, and prints only non-secret diagnostics (labels,
derived client_ids, and whether each entry got a token). ``client_secret`` is
left as an empty string for you to fill in from the Discord developer portal
(needed for invite links / OAuth, not for the gateway connection).

Usage:
    python3 tools/build_bot_app_pool.py tokens bot-app-pool.json
    # then edit bot-app-pool.json to fill in each client_secret, then:
    #   aws secretsmanager put-secret-value \
    #     --secret-id hellodj/<stage>/bot-app-pool \
    #     --secret-string file://bot-app-pool.json --region us-east-1
"""

from __future__ import annotations

import base64
import json
import sys


def _derive_client_id(token: str) -> str:
    """Return the application id encoded in a bot token's first segment.

    A Discord bot token is ``<base64url(app_id)>.<...>.<...>``. Decoding the
    first segment yields the numeric application id. Returns "" when the token
    is malformed so the caller can flag it rather than crash.
    """
    first = token.split(".", 1)[0]
    if not first:
        return ""
    # base64url without padding — pad to a multiple of 4 before decoding.
    padded = first + "=" * (-len(first) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode("ascii")
    except Exception:  # noqa: BLE001 - malformed token → no derivable id
        return ""
    return decoded if decoded.isdigit() else ""


def _parse_tokens(text: str) -> list[tuple[str, str]]:
    """Parse ``<label>: <token>`` lines into (label, token) pairs.

    Blank lines are skipped. A line without a ``:`` separator is treated as a
    bare token with an empty label (the caller defaults the label later).
    """
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if ":" in line:
            label, token = line.split(":", 1)
        else:
            label, token = "", line
        pairs.append((label.strip(), token.strip().strip("`")))
    return pairs


def build_pool(text: str) -> tuple[list[dict[str, str]], list[str]]:
    """Build the pool entries + a list of non-secret diagnostic lines."""
    pool: list[dict[str, str]] = []
    notes: list[str] = []
    for index, (label, token) in enumerate(_parse_tokens(text)):
        resolved_label = label or (f"HelloDJ#{index}" if index else "HelloDJ")
        client_id = _derive_client_id(token)
        pool.append(
            {
                "label": resolved_label,
                "client_id": client_id,
                "client_secret": "",
                "bot_token": token,
            }
        )
        notes.append(
            f"  [{index}] label={resolved_label!r} "
            f"client_id={client_id or '<UNDERIVABLE>'} "
            f"token={'present' if token else 'MISSING'}"
        )
    return pool, notes


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: build_bot_app_pool.py <tokens-file> <output-json>",
            file=sys.stderr,
        )
        return 2
    tokens_path, out_path = argv[1], argv[2]
    with open(tokens_path, encoding="utf-8") as handle:
        pool, notes = build_pool(handle.read())

    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(pool, handle, indent=2)
        handle.write("\n")

    print(f"wrote {len(pool)} pool entrie(s) to {out_path}")
    print("entries (labels + derived client_ids only; tokens NOT shown):")
    print("\n".join(notes))
    underivable = sum(1 for e in pool if not e["client_id"])
    missing_secret = sum(1 for e in pool if not e["client_secret"])
    if underivable:
        print(
            f"WARNING: {underivable} entrie(s) had an underivable client_id "
            "(check the token format).",
            file=sys.stderr,
        )
    print(
        f"NOTE: {missing_secret} entrie(s) have an empty client_secret — fill "
        "these in from the Discord developer portal before the invite/OAuth "
        "flows will work (the gateway connection itself only needs bot_token)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
