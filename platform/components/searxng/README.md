# searxng component

Privacy-respecting **SearXNG metasearch engine**, packaged as a **Nix-built OCI
image** on nixpkgs' `searxng` package — with **no Ubuntu/Debian base layer**
(Requirements 5.1, 5.2, 5.3).

## What it provides

The metasearch backend for the voice-pipeline's Bedrock web-search tool
(spec `bedrock-voice-web-search`). It aggregates results from public search
engines and answers `GET /search?format=json`. The only in-cluster consumer is
the `mcp-searxng-enhanced` component, which the voice-pipeline's `web_search`
Bedrock tool calls over MCP-HTTP.

- `GET /search?q=<query>&format=json` — JSON search results
  (`{"results": [{"title","url","content"}, …]}`).
- `GET /` — the search UI (used as the readiness signal).

## Why a baked settings.yml

SearXNG's default `settings.yml` enables **only** the `html` result format, so a
`format=json` request returns `403 Forbidden`. The baked
`/etc/searxng/settings.yml` here enables `search.formats: [html, json]` so the
MCP wrapper can consume results, binds `0.0.0.0:8080`, and disables the limiter
(the only caller is the in-cluster MCP wrapper, not the public internet).

## Secrets

`server.secret_key` is a **non-secret placeholder** — this instance is
internal-only (behind cluster networking, no authenticated session). It is NOT a
real secret and is safe to bake. If a real per-stage key is ever wanted, inject
it at runtime via `SEARXNG_SECRET` from AWS Secrets Manager (R6.1); do not bake
a real key.

## Base image and CPU architecture

- **Runtime:** nixpkgs `searxng` (Python/Flask), built through Nix — **not** a
  Debian/Ubuntu layer (R5.2, R5.3). Entry point `searxng-run`
  (`searx.webapp.run()`).
- **Image build:** `pkgs.dockerTools.buildLayeredImage` — every layer is a Nix
  closure (R5.1).
- **Default architecture:** `aarch64-linux` (AWS Graviton), matching the fleet
  default (R4.1). `x86_64-linux` is a documented fallback.

## Port

Exposed port: **8080**. In-cluster consumers reach it at
`http://searxng:8080/search`.

## Build

```bash
nix build .#image --system aarch64-linux   # ARM64 Graviton default target
docker load < result
nix flake check
```

The image is built + pushed by the CI/CD pipeline on ARM64 CodeBuild; do not
build/push locally.

## Requirements traceability

- **5.1 / 5.2 / 5.3** — Nix-built OCI image, no Debian/Ubuntu base.
- **6.1** — supports the voice-pipeline web-search capability; any real secret
  injected at runtime from Secrets Manager, not baked.
- **15.1** — self-contained, independently buildable component under
  `components/searxng/`.
