# lavalink component

Custom Lavalink audio server packaged as a **Nix-built OCI image** on a
**Nix-built JVM 25 (Eclipse Temurin) aarch64 base** — with **no
Ubuntu/Debian/Alpine base layer** (Requirements 5.1, 5.3, 3.5). This is the AWS
re-platform successor to the legacy `kube/lavalink/Dockerfile` (which used an
`eclipse-temurin:21-jre` Debian-based image); the OCI image here is assembled
entirely from Nix closures.

This component is a **thin consumer** of the authoritative Lavalink fork flake
(`github:hellodj/Lavalink/dev`). It does **not** build its own image or jars —
it re-exports the fork's Nix-built `#image` (and the jar outputs). Keeping ONE
image definition in the fork avoids drift (Design §4, Requirement 4.5).

## What it provides (capabilities)

The custom JAR + plugin set delivers the audio pipeline HelloDJ depends on:

- **fMP4 HLS streaming** — a lavaplayer patch (from the `hellodj/Lavalink`
  fork, consuming `hellodj/lavaplayer`) that lets Lavalink stream
  fragmented-MP4 HLS manifests, required for **Tidal HLS** manifest playback.
- **SABR (Server Adaptive Bitrate)** — via the custom `youtube-plugin-sabr.jar`
  (from `hellodj/youtube-source`). As of mid-2026 YouTube serves SABR-only
  streams to WEB-family clients; the official `youtube-plugin` (1.18.2) returns
  "No supported audio streams available" without this. **Do not** swap back to
  the official plugin unless SABR support has been verified upstream.
- **LavaSrc** — Spotify/Tidal source resolution via `lavasrc-plugin.jar` (from
  `hellodj/LavaSrc`).
- Standard **Lavalink v4** server protocol on port **2333**.

## Artifact provenance

The three jars are the **real Nix-built outputs of the sibling Fork_Flakes**,
consumed transitively through the authoritative Lavalink fork's `#image`. There
are **no** placeholder derivations in this component any more.

| Artifact | Source (fork flake) | Notes |
|---|---|---|
| `Lavalink.jar` | `github:hellodj/Lavalink/dev` `#lavalinkJar` | Custom build: lavaplayer fMP4 HLS patch + Lavalink v4 server. Upstream remote is `lavalink-devs/Lavalink`. |
| `youtube-plugin-sabr.jar` | `github:hellodj/youtube-source/main` `#youtubeSabrPlugin` (via the fork's `#image`) | SABR-capable YouTube source. |
| `lavasrc-plugin.jar` | `github:hellodj/LavaSrc/tidal-v2-api` `#lavasrcPlugin` (via the fork's `#image`) | Spotify/Tidal source resolution. |

Each jar is a real, runnable jar (manifest + compiled `.class` files) and
contains **no `PLACEHOLDER ARTIFACT` marker** (R4.6). The fork flake's checks
assert this (a jar/plugin resolution failure fails the image build fast and
names the missing artifact — R4.7).

## Base image and CPU architecture

- **Runtime:** `pkgs.temurin-jre-bin-25` (JVM 25 LTS, Eclipse Temurin), built
  through Nix — **not** a Debian/Ubuntu/Alpine layer (R3.5, R5.1). This
  replaces the previous Temurin 21 base.
- **Image build:** `pkgs.dockerTools.buildLayeredImage` (in the fork flake) —
  every layer is a Nix closure (R5.1). The JRE, the app JAR, and the plugins are
  separate layers so a JAR/plugin bump does not re-push the large JRE layer.
- **Default architecture:** `aarch64-linux` (AWS Graviton), matching the fleet
  default. `x86_64-linux` is exposed only as a documented fallback.

## Config injection (NOT baked in)

`application.yml` is **deliberately not** included in the image (R4.8). At
runtime the **`config-renderer`** component renders a complete `application.yml`
(from Secrets Manager + DynamoDB) and mounts/injects it at:

```
/opt/Lavalink/application.yml
```

The image's `WorkingDir` is `/opt/Lavalink`, and Lavalink reads its config from
the working directory, so the injected file is picked up automatically.

## Image layout

```
/opt/Lavalink/
├── Lavalink.jar                     # custom fMP4 HLS + SABR + Lavalink v4
├── plugins/
│   ├── youtube-plugin-sabr.jar
│   └── lavasrc-plugin.jar
└── application.yml                  # (injected at runtime by config-renderer)
```

Entrypoint (mirrors the legacy image):

```
java -Djdk.tls.client.protocols=TLSv1.1,TLSv1.2 -jar /opt/Lavalink/Lavalink.jar
```

Exposed port: **2333** (Lavalink v4 protocol).

## Build

```bash
# Build the OCI image (aarch64 Graviton default target) — delegates to the fork
nix build .#image --system aarch64-linux

# Load into a local container runtime (result is a docker-archive tarball)
docker load < result
# or: podman load < result

# Evaluate/validate the flake structure without building artifacts
nix flake check

# LOCAL verification with a working fork checkout (committed input stays github:)
nix build .#image --override-input lavalink-fork path:/…/Lavalink
```

## Requirements traceability

- **3.5** — image base is a Nix-built Temurin 25 JRE (`temurin-jre-bin-25`),
  replacing the Temurin 21 base.
- **4.5** — the `mkPlaceholderJar` derivations are removed; the jars are the
  real sibling Fork_Flake outputs consumed via the authoritative fork.
- **4.6** — no `PLACEHOLDER ARTIFACT` marker in any bundled jar.
- **5.1** — image built by the Nix build system (`dockerTools.buildLayeredImage`
  in the fork flake); no distro base.
