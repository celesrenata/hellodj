# potoken-server component

YouTube **Proof-of-Origin token (POT)** provider, packaged as a **Nix-built OCI
image** on a **Nix-built Node.js base** — with **no Ubuntu/Debian base layer**
(Requirements 5.1, 5.2, 5.3). This is the AWS re-platform successor to the
external `brainicism/bgutil-ytdlp-pot-provider:latest` image; instead of pulling
that upstream (Debian-based) image, the OCI image here is assembled entirely
from Nix closures.

## What it provides (capabilities)

Generates fresh YouTube Proof-of-Origin tokens on demand, defeating the "Sign in
to confirm you're not a bot" gate for WEB-family YouTube clients. The bot's
PoToken refresh task calls this service and pushes the result into Lavalink,
keeping YouTube multi-source playback working (Requirement 6.1).

- `POST /get_pot` — generate a POT (optional `{ "content_binding": "<visitor_data>" }`).
  Response: `{ "poToken": "...", "contentBinding": "...", "expiresAt": "..." }`.
- `GET /ping` — health check.

Token generation uses [LuanRT's BgUtils/Botguard](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
interfacing library.

## Artifact provenance

The upstream source is **not vendored** in this repository. See the
`TODO(artifact-source)` markers in `flake.nix` for how to wire the real build.

| Artifact | Source (upstream) | Notes |
|---|---|---|
| POT provider (`build/main.js`) | [`Brainicism/bgutil-ytdlp-pot-provider`](https://github.com/Brainicism/bgutil-ytdlp-pot-provider), branch **`master`** | Node.js/TypeScript server (the `server/` subproject). Published image entrypoint is `node build/main.js`. |
| Node.js runtime | `pkgs.nodejs_22` (nixpkgs) | Nix-built/packaged; **not** a Debian/Ubuntu layer. |

Because the source is not in the repo, `flake.nix` currently builds it via a
**placeholder derivation** (`mkPlaceholderApp`) that emits a marker
`build/main.js` at the correct path. This keeps the flake **evaluable and
structurally reviewable** — the Node base, image layers, entrypoint, port, and
the Secrets-Manager-injected secret contract are all real — without shipping
upstream sources. Replace the placeholder with a real `pkgs.buildNpmPackage`
build of `Brainicism/bgutil-ytdlp-pot-provider` (its `server/` directory, which
compiles to `build/main.js`) once the CI artifact channel exists.

## Base image and CPU architecture

- **Runtime:** `pkgs.nodejs_22` (Node.js), built/packaged through Nix — **not**
  a Debian/Ubuntu layer (R5.2, R5.3). The upstream image's entrypoint is
  `node build/main.js`.
- **Image build:** `pkgs.dockerTools.buildLayeredImage` — every layer is a Nix
  closure (R5.1). The Node runtime and the app are separate layers so an app
  bump does not re-push the runtime layer.
- **Default architecture:** `aarch64-linux` (AWS Graviton), matching the fleet
  default (R4.1). `x86_64-linux` is exposed only as a documented fallback.

## Shared secret injection (NOT baked in)

The upstream POT provider has **no built-in authentication** — in the legacy
deployment it ran as an unauthenticated in-cluster service and the bot's PoToken
refresh task degraded gracefully if it was unavailable. On AWS the endpoint sits
inside the VPC behind cluster networking, so no secret is strictly required.

Where a shared secret is used to protect the endpoint, it is **deliberately not**
baked into the image; it is injected at runtime from **AWS Secrets Manager** as
an environment variable (R6.1). Keeping any such secret out of the image and
sourcing it from the one Secrets Manager entry keeps producers and consumers in
sync. Only non-secret runtime defaults are baked into the image:

| Env var | Baked value | Purpose |
|---|---|---|
| `PORT` | `4416` | Listen port (upstream default). |
| *(shared secret)* | *(unset)* | **Injected at runtime from Secrets Manager**, when used. |

## Port

Exposed port: **4416** (upstream default). In-cluster consumers (the bot's
PoToken refresh task) reach it at the service address for this component.

## Image layout

```
/opt/potoken-server/
└── build/
    └── main.js      # compiled Node.js POT provider server
```

Entrypoint (mirrors the upstream image):

```
node /opt/potoken-server/build/main.js
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
> structurally correct image whose `build/main.js` is a placeholder marker
> rather than the runnable upstream server. Wire the real source (see the
> `TODO(artifact-source)` markers in `flake.nix`) before deploying.

## Requirements traceability

- **5.1** — image built by the Nix build system (`dockerTools.buildLayeredImage`).
- **5.2 / 5.3** — no Ubuntu/Debian base; Node.js runtime via Nix closures only
  (replaces the external `brainicism/bgutil-ytdlp-pot-provider:latest` Debian
  image).
- **6.1** — preserves YouTube multi-source playback via on-demand PoToken
  generation; any endpoint-protecting shared secret injected at runtime from
  Secrets Manager; port 4416.
- **15.1** — self-contained, independently buildable/versionable component
  under `components/potoken-server/`.
