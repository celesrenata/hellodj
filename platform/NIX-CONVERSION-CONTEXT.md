# Context for a New Spec: Nix Image Packaging for the Remaining HelloDJ Components

Paste this whole file into a new chat and ask it to **create a spec** (requirements → design → tasks)
for converting the not-yet-packaged HelloDJ platform components to **Nix-built OCI images**.

Suggested opening prompt for the new chat:

> Create a spec named `nix-image-packaging` to add Nix-built OCI image flakes for the HelloDJ platform
> components that don't have one yet, so every component image is Nix-produced with no Ubuntu/Debian/Alpine
> base and the build-stage base-image gate passes (stops SKIPPING) for all of them. Use the context below.

---

## 1. Background

The `aws-saas-replatform` spec (`.kiro/specs/aws-saas-replatform/`) is fully implemented under
`platform/`. A hard requirement of that platform (design Requirement 5.1–5.3) is:

- **Every container image is built with the Nix build system.**
- **No Ubuntu base images. No Debian base images.** (And by extension no Alpine — the platform is
  Nix-native, not distro-based.)
- Default CPU architecture is **AWS Graviton / aarch64**, x86-64 only as a documented per-component
  fallback behind the dependency gate.

The build-stage **base-image gate** (`platform/tools/gate_base_image.py`, backed by
`hellodj_platform_logic.base_image_gate.check_base`, Correctness Property 6) enforces this in the
CDK pipeline: it treats each component's `flake.nix` as the authoritative image definition and
**rejects** any non-Nix base. Components with **no `flake.nix`** are currently reported as
**SKIPPED** ("Nix packaging pending, task 20.1") rather than passing.

**This new spec exists to close that gap: give the remaining components real Nix image flakes so the
gate enforces (not skips) them.**

## 2. Exact current state — which components have a Nix flake and which don't

Under `platform/components/`:

| Component | Runtime | Port | Has `flake.nix`? | Notes |
|---|---|---|---|---|
| `lavalink` | JVM 21 (Temurin) | 2333 | ✅ yes | `dockerTools.buildLayeredImage` on `temurin-jre-bin-21`; placeholder JAR derivations |
| `spotify-stream` | Rust (librespot) | 8802 | ✅ yes | `buildLayeredImage` over `pkgs.librespot` + shell entrypoint |
| `yt-cipher` | Deno | 8001 | ✅ yes | `buildLayeredImage` on `pkgs.deno`; placeholder app |
| `potoken-server` | Node.js | 4416 | ✅ yes | `buildLayeredImage` on `pkgs.nodejs_22`; placeholder app |
| `discord-bot-core` | Python 3.11 (discord.py/wavelink) | — (no server port) | ❌ **NO** | package only: `discord_bot_core/`, `pyproject.toml` |
| `playback-orchestrator` | Python 3.11 | 8080 (HTTP) | ❌ **NO** | package only; single writer to `hellodj-session` |
| `config-renderer` | Python 3.11 | — (init/Job) | ❌ **NO** | renders lavalink `application.yml`; runs as init container/Job |
| `activity-backend` | Python 3.11 (aiohttp) | 8090 | ❌ **NO** | Activity server + WebSocket hub |
| `voice-pipeline` | Python 3.11 (onnxruntime + boto3) | — (no server port) | ❌ **NO** | local ONNX wakeword; STT/intent/TTS via Bedrock/Transcribe/Polly |
| `web-ui` | Python 3.11 (Flask/gunicorn) + Node (Tailwind v4 build) | 8080 | ❌ **NO** (has a **Dockerfile**) | Dockerfile uses `node:22-slim` + `python:3.11-slim` (Debian) — must be replaced by Nix |
| `migration` | Python 3.11 (boto3) | — (one-shot Job) | ❌ **NO** | admin bootstrap migration job |
| `hellodj_platform_logic` | shared Python pkg | — | N/A | not a deployable image; imported by the others |

**So: 7 components need a Nix image flake** — `discord-bot-core`, `playback-orchestrator`,
`config-renderer`, `activity-backend`, `voice-pipeline`, `web-ui`, `migration`.
(The 4 that already have flakes are done; `hellodj_platform_logic` is a library, not an image.)

### The web-ui special case
`platform/components/web-ui/Dockerfile` is a **Debian-based reference** (`node:22-slim` Tailwind v4
CSS build → `python:3.11-slim` gunicorn runtime). Its own header comment says the Nix packaging must
wrap the *same two phases* (a Node CSS build + a Python runtime) as Nix derivations. The Nix flake
must reproduce: the Tailwind v4 CSS compile (`@tailwindcss/cli@4`), vendoring htmx/alpine JS locally
(no runtime CDN), then a Python/gunicorn runtime — all Nix, no Debian.

## 3. The shared-package dependency (important design constraint)

Every Python component imports the shared pure-logic package
`hellodj_platform_logic` (at `platform/components/hellodj_platform_logic/`, declared as the
`hellodj-platform-logic` dependency in each component's `pyproject.toml`). The Nix image for each
Python component **must include `hellodj_platform_logic` on the Python path** (as its own Nix
derivation / package), so the single-source-of-truth decision logic ships in the image. Decide and
document how: a shared Nix package input reused by every component flake is the clean option.

## 4. Reference patterns to follow (already in the repo)

- **Nix OCI image pattern:** `platform/components/lavalink/flake.nix` and
  `platform/components/potoken-server/flake.nix` — both use
  `flake-utils.lib.eachSystem [ "aarch64-linux" "x86_64-linux" ]`,
  `pkgs.dockerTools.buildLayeredImage`, default target `aarch64-linux` (Graviton) with `x86_64-linux`
  as documented fallback, no distro base, `pkgs.cacert` for TLS, a `checks.image-builds` output,
  and a README documenting provenance + the config/secret injection contract.
- **Python packaging:** use `pkgs.python311` + `buildPythonApplication`/`buildPythonPackage` (or
  `poetry2nix`/`uv2nix` if preferred — the components use `pyproject.toml` with setuptools) so the
  component and `hellodj_platform_logic` are Nix-built layers, not `pip install` into a Debian base.
- **Base-image gate contract:** read `platform/tools/gate_base_image.py` and
  `hellodj_platform_logic/base_image_gate.py`. A component is "Nix-produced" when its `flake.nix`
  builds via `dockerTools.*Image` and references no `ubuntu`/`debian` base in an active position.
  Ensure each new flake satisfies this so the gate returns PASS (not SKIP) for it.

## 5. Requirements the new spec should cover (draft)

1. Each of the 7 unpackaged components gets a `flake.nix` producing an OCI image via
   `dockerTools.buildLayeredImage`, aarch64 default, no Ubuntu/Debian/Alpine base (R5.1–5.3).
2. Python components ship `hellodj_platform_logic` and their own package as Nix-built layers, with
   the component's entrypoint (module `main`/gunicorn/Job) as the image `Entrypoint`/`Cmd`.
3. `web-ui`: replace the Debian Dockerfile with a Nix flake that performs the Tailwind v4 CSS build
   (`@tailwindcss/cli@4`) and vendors htmx/alpine, then a Nix Python+gunicorn runtime on port 8080;
   remove or clearly demote the Dockerfile to a non-authoritative reference.
4. Config/secret injection stays runtime (Secrets Manager / DynamoDB env), never baked in — same
   contract the existing flakes document.
5. Correct ports exposed per the table in §2; correct run mode (long-running server vs one-shot Job
   for `config-renderer` and `migration`).
6. After conversion, `python3 tools/gate_base_image.py` reports **PASS for all components and SKIP
   for none** (this is the acceptance signal).
7. Update `platform/tools/gate_base_image.py`'s SKIP handling only if needed; keep
   `base_image_gate.check_base` (and its Property 6 test) unchanged.
8. Keep each image minimal (Graviton, layered, no docs/locales bloat), consistent with the design's
   cost/closure goals.

## 6. Design decisions the new chat should resolve (call these out)

- **Python-in-Nix toolchain:** plain `buildPythonApplication` vs `uv2nix`/`poetry2nix`. The repo uses
  `pyproject.toml` + setuptools and a `.venv-pbt` for tests; pick one and apply it uniformly.
- **Native deps:** `voice-pipeline` needs `onnxruntime` + `numpy` (and boto3) on aarch64 — confirm
  they're in nixpkgs for aarch64 or vendor wheels; this is the component most likely to have an ARM64
  wheel/build wrinkle (it's exactly what the dependency gate exists to catch).
- **web-ui two-stage build in Nix:** how to run the Node/Tailwind build as a Nix derivation and feed
  its output into the Python runtime image (a `runCommand` CSS derivation copied into the layered
  image).
- **Shared `hellodj_platform_logic` packaging:** one shared flake input/derivation reused by all
  Python component flakes vs. each flake re-deriving it.
- **Image tag / registry:** the CDK `WorkloadsStack` pulls `${ecrRegistry}/<component>:<tag>` with a
  pipeline-injected tag; the flakes just need to produce the image, the pipeline handles push/tag.

## 7. Verification the new spec should specify

- `nix flake check --no-build` (and `nix build .#image` where a builder is available) per component
  flake evaluates cleanly (aarch64 target).
- `python3 tools/gate_base_image.py` → every component PASS, zero SKIP.
- The CDK base-image gate step (pipeline, `getBuildCommands` in
  `platform/infra/lib/pipeline-stack.ts`) still wired and green.
- Existing component unit/PBT tests unaffected (they test the Python packages, not the images).

## 8. Pointers (files the new chat should read first)

- `platform/components/lavalink/flake.nix`, `platform/components/potoken-server/flake.nix` — the
  Nix OCI image reference pattern.
- `platform/components/web-ui/Dockerfile` — the Debian two-phase build to reproduce in Nix.
- `platform/tools/gate_base_image.py` + `platform/components/hellodj_platform_logic/base_image_gate.py`
  — the gate the conversion must satisfy.
- `platform/infra/lib/component-workloads.ts` — the authoritative per-component catalog (names,
  ports, placement) the images must match.
- `.kiro/specs/aws-saas-replatform/design.md` (Requirement 5 / "Nix-Only Container Images", the
  Component Decomposition table) and `requirements.md` (Requirement 5).
- Steering: `.kiro/steering/hellodj-architecture.md` and the global NixOS workflow steering
  (Nix-native, declarative, no imperative deploys).
