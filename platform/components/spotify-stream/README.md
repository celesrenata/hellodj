# spotify-stream component

Direct **Spotify** audio streaming sidecar, built on **librespot** (Rust) and
packaged as a **Nix-built OCI image** on a **Nix-built base** — with **no
Ubuntu/Debian base layer** (Requirements 5.1, 5.2, 5.3). This is the AWS
re-platform successor to the legacy `spotify-stream` sidecar (previously
`registry.celestium.life/hellodj/spotify-stream`), reassembled entirely from
Nix closures.

## What it provides (capabilities)

The sidecar streams Spotify audio **directly** (bypassing YouTube mirroring),
preserving multi-source playback (Requirement 6.1). It exposes a direct-stream
HTTP interface that `lavalink` / `playback-orchestrator` pull from, matching the
legacy sidecar contract.

- **Streaming engine:** librespot — the open-source Rust reimplementation of the
  Spotify Connect / streaming client.
- **Direct stream:** audio is served on the sidecar port, so Spotify tracks play
  from Spotify rather than being resolved to a YouTube mirror.

## Port

- **8802/tcp** — the direct-stream interface. This matches the legacy
  `spotify-stream` sidecar port documented in the HelloDJ architecture, and is
  declared as the image's `ExposedPorts` in `flake.nix`. Override with
  `SPOTIFY_STREAM_PORT` if needed.

## Artifact provenance

| Artifact | Source | Notes |
|---|---|---|
| `librespot` | [`librespot-org/librespot`](https://github.com/librespot-org/librespot), packaged as `pkgs.librespot` | Open-source Rust Spotify streaming client. Built purely by Nix (`rustPlatform`) — **no Debian/Ubuntu base**. Always realizable in the flake. |
| `spotify-stream` wrapper | `./crate` (this repo) | Thin HelloDJ wrapper crate that drives librespot, exposes the direct-stream HTTP surface on 8802, and reads the Spotify secret from the Secrets-Manager injection point. |

The upstream **librespot** build is a real, buildable Nix derivation. The
HelloDJ **wrapper crate** (`./crate`) does not yet vendor its full dependency
set / `Cargo.lock`, so building it from source is gated behind a
`TODO(artifact-source)` marker in `flake.nix`. Until that is wired up, the image
packages upstream `pkgs.librespot` plus a Nix-built entrypoint wrapper that
enforces the Secrets Manager injection contract and the 8802 port — keeping the
flake **fully evaluable and buildable** and the structure reviewable. Replace
`wrapperCrate` in `flake.nix` with a `pkgs.rustPlatform.buildRustPackage` of
`./crate` once its `Cargo.lock` is committed.

## Base image and CPU architecture

- **Runtime:** `pkgs.librespot` (Rust binary) + a `writeShellApplication`
  entrypoint, both built/pulled through Nix — **not** a Debian/Ubuntu layer
  (R5.2, R5.3).
- **Image build:** `pkgs.dockerTools.buildLayeredImage` — every layer is a Nix
  closure (R5.1). librespot, the entrypoint wrapper, and the CA bundle are
  separate layers.
- **Default architecture:** `aarch64-linux` (AWS Graviton), matching the fleet
  default (R4.1). `x86_64-linux` is exposed only as a documented fallback.

## Credentials (AWS Secrets Manager — NOT baked in)

Spotify credentials/tokens are **never** baked into the image or the binary.
They are injected at **runtime** from **AWS Secrets Manager**, read in priority
order:

1. **`SPOTIFY_CREDENTIALS_FILE`** — path to a Secrets-Manager-mounted file
   (e.g. via the AWS Secrets & Config Provider CSI volume).
2. **`SPOTIFY_CREDENTIALS`** — a Secrets Manager injected environment variable.

If neither is present, the sidecar **refuses to start** so a misconfigured
deployment fails fast instead of serving without credentials. The secret value
is never logged — only whether and from where it was resolved. This replaces the
legacy approach where the sidecar shared the `hellodj.db` SQLite token store on
a mounted volume (R6.1 / R15.1; the data-layer requirements remove SQLite).

## Image layout / entrypoint

```
/app/                         # WorkingDir
librespot                     # (from pkgs.librespot, on PATH)
spotify-stream-entrypoint     # Nix entrypoint wrapper (reads Secrets Manager secret)
```

Entrypoint: `spotify-stream-entrypoint`, which resolves the Secrets-Manager
credential (file or env), then launches the librespot direct-stream sidecar
bound to `0.0.0.0:8802`.

Exposed port: **8802** (direct Spotify stream).

## Build

```bash
# Build the OCI image (aarch64 Graviton default target)
nix build .#image --system aarch64-linux

# Load into a local container runtime (result is a docker-archive tarball)
docker load < result
# or: podman load < result

# Evaluate/validate the flake structure without building artifacts
nix flake check
```

## Rust crate skeleton (`./crate`)

A minimal `spotify-stream` crate documents the intended wrapper surface:

- `load_spotify_credentials()` — resolves the secret from
  `SPOTIFY_CREDENTIALS_FILE` then `SPOTIFY_CREDENTIALS`, erroring if absent.
- `resolve_port()` — honors `SPOTIFY_STREAM_PORT`, defaulting to **8802**.

Its dependency set (the librespot crates + an async HTTP server) and a committed
`Cargo.lock` are still needed before the from-source Nix build path is enabled;
see the `TODO(artifact-source)` markers. `cargo check` / `cargo test` on the
skeleton is optional (a Rust toolchain may be absent in this environment).

## Requirements traceability

- **5.1** — image built by the Nix build system (`dockerTools.buildLayeredImage`
  over a Nix-built `pkgs.librespot`).
- **5.2 / 5.3** — no Ubuntu/Debian base; librespot + entrypoint via Nix closures
  only.
- **6.1** — preserves multi-source playback via direct Spotify streaming.
- **15.1** — self-contained, independently buildable/deployable component under
  `components/spotify-stream/`.
