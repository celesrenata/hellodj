# yt-cipher component

YouTube player-script **signature / n-parameter deciphering** HTTP service,
packaged as a **Nix-built OCI image** on a **Nix-built Deno base** — with **no
Ubuntu/Debian base layer** (Requirements 5.1, 5.2, 5.3). This is the AWS
re-platform successor to the external `ghcr.io/kikkia/yt-cipher:master` image;
instead of pulling that upstream (Debian-based) image, the OCI image here is
assembled entirely from Nix closures.

## What it provides (capabilities)

Lavalink's `youtube-source` plugin offloads YouTube player-script deciphering to
this service (via its `remoteCipher` config). yt-cipher is an HTTP wrapper
around [`yt-dlp/ejs`](https://github.com/yt-dlp/ejs) exposing (Requirement 6.1):

- `POST /decrypt_signature` — decrypt a stream signature + `n` param.
- `POST /get_sts` — extract the signature timestamp (`sts`) from a player script.
- `POST /resolve_url` — resolve a raw stream URL into a ready-to-play URL.

Preserving remote cipher keeps YouTube multi-source playback working (R6.1).

## Artifact provenance

The upstream source is fetched and built from Nix (no vendored copy in this
repo) in `flake.nix`, pinned by commit + content hashes. The build reproduces
the upstream `Dockerfile`: fetch a pinned `yt-dlp/ejs` checkout, patch it
(`scripts/patch-ejs.ts` rewrites its imports to `npm:meriyah` / `npm:astring`),
then `deno compile` a self-contained `server` binary.

| Artifact | Source (upstream) | Pin | Notes |
|---|---|---|---|
| `yt-cipher` app | [`kikkia/yt-cipher`](https://github.com/kikkia/yt-cipher) | rev `1e1fd8e…` | Deno HTTP API around `yt-dlp/ejs`; `deno compile`s `server.ts` (+`worker.ts`) into a standalone binary. |
| `yt-dlp/ejs` | [`yt-dlp/ejs`](https://github.com/yt-dlp/ejs) | rev `cd4e87f…` (the upstream `EJS_COMMIT`) | Patched at build time by `scripts/patch-ejs.ts`. |
| Deno runtime | `pkgs.deno` (nixpkgs) | 2.9.4 | Nix-built/packaged; **not** a Debian/Ubuntu layer. |
| `denort` runtime | `dl.deno.land` release zip | 2.9.4, **aarch64** | `deno compile`'s target runtime; fetched as a fixed-output derivation and supplied via `DENORT_BIN` so the compile is offline. |

### Hermetic Deno build (three pinned hashes)

Deno resolves remote `https://deno.land/std@…` + `npm:` imports over the network
and `deno compile` downloads `denort` — none allowed in Nix's build sandbox. So
`flake.nix` uses three pinned hashes:

1. `fetchFromGitHub` `hash` for the app source (and one for `yt-dlp/ejs`).
2. A **fixed-output `denoCache` derivation** (`outputHash`) that patches ejs and
   runs `deno cache` with network allowed, then emits ONLY the content-stable
   dep payloads (`remote/` + `npm/` + `deps/`), stripping Deno's SQLite/`gen`
   caches so the hash is deterministic. Recompute by building `.#denoCache` and
   reading the `got:` hash.
3. A **fixed-output `denortZip`** (`fetchurl` `hash`) for the aarch64 `denort`.

The app build then runs fully offline (`--cached-only`, `DENORT_BIN` set) and
cross-targets `aarch64-unknown-linux-gnu` (AWS Graviton). Bump all pins together
when the `deno` version or upstream revs change. The image is built + pushed by
the CI/CD pipeline on ARM64 CodeBuild; do not build/push locally.

## Base image and CPU architecture

- **Runtime:** `pkgs.deno` (Deno), built/packaged through Nix — **not** a
  Debian/Ubuntu layer (R5.2, R5.3). Upstream ships a Deno application; the
  entrypoint is `deno run --allow-net --allow-read --allow-write --allow-env
  server.ts`.
- **Image build:** `pkgs.dockerTools.buildLayeredImage` — every layer is a Nix
  closure (R5.1). The Deno runtime and the app are separate layers so an app
  bump does not re-push the runtime layer.
- **Default architecture:** `aarch64-linux` (AWS Graviton), matching the fleet
  default (R4.1). `x86_64-linux` is exposed only as a documented fallback.

## Shared secret injection (NOT baked in)

The **shared secret** — the yt-cipher `API_TOKEN` — is **deliberately not** set
in the image. At runtime it is injected from **AWS Secrets Manager** as the
`API_TOKEN` environment variable (R6.1):

```
API_TOKEN=<from AWS Secrets Manager: yt-cipher-secret>
```

yt-cipher rejects any request lacking a valid `Authorization: <API_TOKEN>`
header when a token is set. This is the **same** shared secret used by the
rendered Lavalink config as `remoteCipher.password`; the two **must match** or
cipher requests are rejected. Keeping the secret out of the image and sourcing
both consumers from the one Secrets Manager entry guarantees they stay in sync.

Only non-secret runtime defaults are baked into the image:

| Env var | Baked value | Purpose |
|---|---|---|
| `PORT` | `8001` | Listen port (upstream default). |
| `HOST` | `0.0.0.0` | Bind address. |
| `OVERRIDE_PLAYER_VARIANT` | `IAS` | Upstream-recommended reliable variant (matches legacy deployment). |
| `API_TOKEN` | *(unset)* | **Injected at runtime from Secrets Manager.** |

## Port

Exposed port: **8001** (upstream default). In-cluster consumers reach it at the
service address the Lavalink config's `remoteCipher.url` points at.

## Image layout

```
/opt/yt-cipher/
└── server.ts        # Deno entrypoint (+ patched yt-dlp/ejs deps when wired)
```

Entrypoint (mirrors upstream):

```
deno run --allow-net --allow-read --allow-write --allow-env /opt/yt-cipher/server.ts
```

## Build

```bash
# Build the OCI image (aarch64 Graviton default target)
nix build .#image --system aarch64-linux

# Load into a local container runtime (result is a docker-archive tarball)
docker load < result   # or: podman load < result

# Evaluate/validate the flake structure without building artifacts
nix flake check
```

> With the placeholder app derivation in place, `nix build` produces a
> structurally correct image whose `server.ts` is a placeholder marker rather
> than the runnable upstream application. Wire the real source (see the
> `TODO(artifact-source)` markers in `flake.nix`) before deploying.

## Requirements traceability

- **5.1** — image built by the Nix build system (`dockerTools.buildLayeredImage`).
- **5.2 / 5.3** — no Ubuntu/Debian base; Deno runtime via Nix closures only
  (replaces the external `ghcr.io/kikkia/yt-cipher:master` Debian image).
- **6.1** — preserves YouTube multi-source playback via remote cipher; shared
  secret `API_TOKEN` injected at runtime from Secrets Manager; port 8001.
- **15.1** — self-contained, independently buildable/versionable component
  under `components/yt-cipher/`.
