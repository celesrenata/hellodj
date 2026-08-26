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

The upstream source is **not vendored** in this repository. See the
`TODO(artifact-source)` markers in `flake.nix` for how to wire the real fetch.

| Artifact | Source (upstream) | Notes |
|---|---|---|
| `yt-cipher` app (`server.ts` + deps) | [`kikkia/yt-cipher`](https://github.com/kikkia/yt-cipher), branch **`master`** | Deno HTTP API around `yt-dlp/ejs`. Requires a pinned checkout of `yt-dlp/ejs` patched via the repo's `scripts/patch-ejs.ts`. |
| Deno runtime | `pkgs.deno` (nixpkgs) | Nix-built/packaged; **not** a Debian/Ubuntu layer. |

Because the source is not in the repo, `flake.nix` currently builds it via a
**placeholder derivation** (`mkPlaceholderApp`) that emits a marker `server.ts`
at the correct path. This keeps the flake **evaluable and structurally
reviewable** — the Deno base, image layers, entrypoint, port, and the
Secrets-Manager-injected `API_TOKEN` contract are all real — without shipping
upstream sources. Replace the placeholder with a real
`pkgs.fetchFromGitHub { owner = "kikkia"; repo = "yt-cipher"; … }` (plus the
pinned `yt-dlp/ejs` checkout and a vendored Deno dependency cache) once the CI
artifact channel exists.

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
