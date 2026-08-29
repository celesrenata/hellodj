# mcp-searxng-enhanced component

The [`OvertliDS/mcp-searxng-enhanced`](https://github.com/OvertliDS/mcp-searxng-enhanced)
MCP server (category-aware web search, website scraping, date/time), packaged as
a **Nix-built OCI image** from the upstream Python source — with **no
Ubuntu/Debian base layer** (Requirements 5.1, 5.2, 5.3).

## What it provides

The MCP-over-HTTP web-search wrapper for the voice-pipeline's Bedrock
`web_search` tool (spec `bedrock-voice-web-search`). Run in **FastMCP HTTP mode**
(`python mcp_server.py --http`), it exposes the MCP protocol at `/mcp` and
fronts the in-cluster `searxng` metasearch component.

Tools (called via MCP JSON-RPC `tools/call`):

- `search_web` — category-aware SearXNG web search (the voice-pipeline uses this).
- `get_website` — scrape/extract a page's content (Trafilatura; PDF→Markdown).
- `get_current_datetime` — timezone-aware date/time.

The voice-pipeline's `WebSearchClient` POSTs a `tools/call` for `search_web` to
`http://mcp-searxng-enhanced:8000/mcp`; this server queries
`SEARXNG_ENGINE_API_BASE_URL` (the in-cluster `searxng` Service) and returns
result snippets the Bedrock responder folds into a brief spoken answer.

## Artifact provenance

Fetched and built from Nix (no vendored copy) via `fetchFromGitHub` +
`python3.withPackages` in `flake.nix`, pinned by commit + content hash.

| Artifact | Source (upstream) | Pin |
|---|---|---|
| `mcp_server.py` | [`OvertliDS/mcp-searxng-enhanced`](https://github.com/OvertliDS/mcp-searxng-enhanced) | master HEAD `31291d9` (added HTTP Server Mode) |
| Python runtime + deps | `pkgs.python3` + `fastmcp`, `httpx`, `beautifulsoup4`, `pydantic`, `tzdata`, `trafilatura`, `python-dateutil`, `cachetools`, `filetype`, `pymupdf`, `pymupdf4llm`, `lxml-html-clean` (nixpkgs) | — |

### Dependency version note

The upstream `requirements.txt` pins looser upper bounds than nixpkgs' current
versions for a few packages (`trafilatura<2.0` vs nixpkgs `2.2`, `cachetools<6`
vs `7.x`, `pydantic<3`). The app has no lockfile and imports these by public
API, so the nixpkgs versions are used. `trafilatura`/`pymupdf` power the
`get_website` scraping tool; the voice-pipeline only calls `search_web`, so a
scraping-dep drift does not affect the web-search path. If `get_website` is ever
wired for the bot, re-verify against the nixpkgs versions.

## Base image and CPU architecture

- **Runtime:** `pkgs.python3` + Nix-packaged deps — **not** a Debian/Ubuntu
  layer (R5.2, R5.3). Replaces the upstream `overtlids/mcp-searxng-enhanced`
  Debian image.
- **Image build:** `pkgs.dockerTools.buildLayeredImage` — every layer is a Nix
  closure (R5.1).
- **Default architecture:** `aarch64-linux` (AWS Graviton); `x86_64-linux` is a
  documented fallback.

## Configuration (all non-secret, injected at runtime)

| Env var | Baked default | Purpose |
|---|---|---|
| `MCP_HTTP_HOST` | `0.0.0.0` | HTTP-mode bind address. |
| `MCP_HTTP_PORT` | `8000` | HTTP-mode port (serves `/mcp`). |
| `ODS_CONFIG_PATH` | `/config/ods_config.json` | Persistent config path. |
| `SEARXNG_ENGINE_API_BASE_URL` | *(unset — injected)* | In-cluster searxng `/search` endpoint (`http://searxng:8080/search`). |
| `DESIRED_TIMEZONE` | *(unset — injected)* | Timezone for `get_current_datetime`. |

No secrets and no AWS API calls, so the component holds no IAM grant.

## Port

Exposed port: **8000** (HTTP mode). The voice-pipeline reaches it at
`http://mcp-searxng-enhanced:8000/mcp`.

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
- **6.3** — provides the voice-pipeline's web-search capability (brief spoken
  answers to general voice queries via Bedrock + SearXNG).
- **15.1** — self-contained, independently buildable component under
  `components/mcp-searxng-enhanced/`.
