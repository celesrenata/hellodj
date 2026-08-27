# Research Notes — Architecture & Cost Baseline

These notes capture verified facts gathered during the requirements phase so the
design phase does not have to guess. All prices are on-demand, us-east-1, gathered
2026-08-24. Pricing changes frequently; the design phase MUST re-verify before
finalizing the cost model. Content rephrased from sources for licensing compliance.

## CPU Architecture Decision: Graviton (ARM64) as Default

The platform targets **ARM64 / AWS Graviton** as the default architecture for cost
(Graviton is materially cheaper than comparable x86 per vCPU). x86 is retained only
as a per-component fallback if an ARM64 dependency is blocked.

### ARM64 dependency verification (from bot/web-ui/tidal-stream/spotify-stream requirements)

| Dependency | ARM64 status | Notes |
|---|---|---|
| ctranslate2 / faster-whisper | ✅ Supported | Explicitly supports AArch64/ARM64 (Ruy, OpenBLAS, oneDNN backends) |
| onnxruntime | ⚠️ CPU wheels exist for aarch64 manylinux; Python-version lag historically. Build from source under Nix (nixpkgs supports aarch64). | Wake word ONNX model — CPU inference only, small model |
| numpy / librosa / soundfile | ✅ Supported | Standard aarch64 manylinux wheels; Nix builds available |
| cryptography | ✅ Supported | Rust-based, aarch64 wheels + Nix |
| yt-dlp | ✅ Pure Python | Architecture-independent |
| FFmpeg (+ x264/x265/opus/dav1d) | ✅ Supported | Nix builds for aarch64; NVENC available on T4G |
| librespot (spotify-stream) | ✅ Rust | aarch64 targets supported |
| tidalapi | ✅ Pure Python | Architecture-independent |
| Lavalink / JVM | ✅ Supported | eclipse-temurin has aarch64 JRE; JVM is arch-portable |
| psycopg2-binary | ⛔ N/A | Being dropped — PostgreSQL replaced by DynamoDB |
| discord.py[voice] / wavelink / aiohttp | ✅ Supported | Native ext (PyNaCl) has aarch64 wheels/Nix builds |

**Conclusion:** No hard ARM64 blocker in the current stack. onnxruntime is the only
"build-from-source under Nix" item, which the all-Nix image strategy already covers.
The requirement will mandate a verification gate before x86 is formally dropped.

## GPU Option: g5g (Graviton2 + NVIDIA T4G) — keeps one architecture

The **g5g** family pairs Graviton2 with an NVIDIA T4G GPU. This lets the ENTIRE
fleet — including the GPU/transcode node — stay ARM64, avoiding an architecture split.

| Instance | vCPU | RAM | GPU | On-demand ~/hr | ~/mo |
|---|---|---|---|---|---|
| g5g.xlarge | 4 | 8 GiB | 1× T4G (16 GiB) | ~$0.42 | ~$307 |

- Spot / reserved T4G can drop to ~$0.085–0.23/hr per GPU.
- x86 GPU fallback (if ever needed): g4dn.xlarge (4 vCPU / 16 GiB / T4) ~$0.526/hr (~$384/mo).
- Current workload barely taxes an Intel iGPU, so a single small GPU node (or spot)
  is expected to suffice; software transcode on Graviton is a cheaper fallback for
  low load.

## Fargate Graviton

- AWS Fargate supports Graviton (ARM64) in all regions.
- Fargate bills per vCPU-hour + per GB-hour; Graviton tasks are cheaper than x86.
- ECS on Fargate Graviton is the baseline for stateless components (bot, web-ui,
  stream sidecars). Exact per-vCPU/GB rate to be re-pulled in design phase.

## 3-Tier Cost Model (to be finalized in design with live pricing)

- **Minimum:** smallest viable Fargate Graviton tasks + spot g5g (or software
  transcode) + on-demand DynamoDB + minimal CloudFront/log retention.
- **Recommended:** right-sized Fargate Graviton + 1× on-demand g5g.xlarge for
  transcode + DynamoDB with DAX for hot paths + full observability.
- **Recommended + headroom:** larger Fargate reservations + g5g headroom / warm
  spare + provisioned DynamoDB + extended log retention and analytics.

## Sources (rephrased for compliance)

- AWS EC2 G5g instances / vantage.sh / economize.cloud — g5g.xlarge pricing & specs
- AWS Fargate pricing page — Graviton support
- CTranslate2 / faster-whisper GitHub — AArch64/ARM64 support statement
- onnxruntime docs / PyPI — aarch64 build guidance
