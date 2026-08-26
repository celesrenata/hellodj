# Pre-baked minimal NixOS GPU transcode-node AMI

This directory builds the **pre-baked, minimal NixOS Amazon Machine Image**
(aarch64 / AWS Graviton) for the HelloDJ **GPU transcode node group**. Karpenter
/ the transcode node group launches `g5g.xlarge` Spot instances directly from
this AMI when the hybrid GPU controller spins the "gas engine" up (Decision D3).

> **Scope:** This AMI is **specific to the GPU transcode node group**. The app
> node group does **not** use an AMI — it runs the Nix-built **OCI container
> images** (see `platform/components/*/flake.nix`) on a standard Graviton node
> group. Both paths are 100% Nix-built with **no Ubuntu/Debian base**
> (Requirement 5). The transcode workload container (`hls-transcode`,
> `platform/components/hls-transcode/`) runs **on** this node; the AMI provides
> the immutable **host** plus the **NVIDIA/NVENC userspace + kernel modules**
> the container's FFmpeg NVENC path binds to.

## Files

| File | Purpose |
|------|---------|
| `gpu-node.nix` | The NixOS module/configuration for the transcode-node host (hardening, NVIDIA/NVENC, CloudWatch agent, tmpfs HLS scratch, minimal closure). |
| `flake.nix` | Build wrapper that uses `nixos-generators`' `amazon-image` format (aarch64) to produce the AMI artifact from `gpu-node.nix`. |

## Building the AMI

The image is aarch64 (Graviton), so build it on an **aarch64 builder** (native
Graviton CI runner or a remote aarch64 builder) — the flake does not
cross-compile.

Using the flake (preferred):

```bash
# from platform/infra/ami/
nix build .#amazonImage
# -> result/ contains the EBS-backed disk image + AMI metadata (nix-support/)
```

Equivalently, using the `nixos-generators` CLI:

```bash
nixos-generate -f amazon --system aarch64-linux -c ./gpu-node.nix
```

The produced artifact is a disk image plus AMI metadata. The upload/register
step (`aws ec2 import-snapshot` / `register-image`, wired into the deployment
pipeline) turns it into a registered AMI whose ID is fed to the transcode node
group / Karpenter provisioner in `infra/lib/eks-stack.ts` (task 16.3).

### Syntax / evaluation check (offline)

`nixos-generators` is a flake input fetched from GitHub, so a full
`nix build` / `nix flake check` requires network access to resolve inputs. To
validate the Nix **syntax** without fetching inputs:

```bash
nix-instantiate --parse gpu-node.nix   # parses the NixOS module
nix-instantiate --parse flake.nix      # parses the flake expression
```

When network is available:

```bash
nix flake check --no-build   # evaluate the flake + configuration
```

## Why a baked AMI here (boot / hardening rationale)

A pre-baked AMI builds the NixOS system closure **ahead of time**. Karpenter
launches instances directly from it, so boot reduces to:

```
kernel -> initrd -> mount pre-realized Nix store -> start transcode systemd unit
```

No store download, no `nixos-rebuild`, no activation phase on the node. The
guest boots in a few seconds; the remaining "launch API -> Ready" time is AWS
**Nitro** provisioning (ENI/EBS attach, VM launch), which the guest cannot
optimize away. The **CPU libx264 floor always covers the GPU-boot + Nitro
window**, so the ≤5s `Interactive_Latency_Budget` holds even during a cold GPU
spin-up (R3.12, R3.13).

Host hardening / trimming rules (all declarative in `gpu-node.nix`):

- **No interactive access (R17.1).** OpenSSH, getty, and user accounts are
  removed. The node is immutable cattle; it is never logged into. This shrinks
  the boot critical path and removes the SSH attack surface.
- **NVIDIA/NVENC (R3.11).** NVIDIA driver + NVENC/NVDEC userspace and kernel
  modules (`nvidia`, `nvidia_uvm`, `nvidia_modeset`, `nvidia_drm`) for the
  T4G/G5g GPU, plus the NVIDIA container toolkit so the `hls-transcode`
  container can bind the GPU.
- **CloudWatch agent (R10.1, R10.2).** A lean `amazon-cloudwatch-agent`
  systemd service ships node/container logs + host/GPU metrics to CloudWatch
  Logs (and onward to the S3 Hive Log_Store), so observability does not depend
  on host login.
- **tmpfs HLS scratch.** RAM-backed tmpfs at `/var/lib/hls-scratch`
  (`HELLODJ_HLS_SCRATCH`) for HLS segments during transcode. Segments are
  served/persisted via S3/CloudFront, so the node needs almost no durable
  storage.
- **Minimal closure / small root.** Docs, man/info, extra locales, and the
  console gettys are stripped; the journal is volatile (logs go to CloudWatch);
  `initrd` is minimized. The trimmed closure + tmpfs scratch target a small
  **~8-16 GiB gp3** root, expanded at boot via `growPartition`.
- **IAM instance role.** The CloudWatch agent, ECR/Nix-cache pulls, and SDK
  calls (Bedrock/S3) authenticate via the **EC2 instance role** — there are
  **no static credentials** on the host.

## Requirements traceability

| Requirement | Where satisfied in `gpu-node.nix` |
|-------------|-----------------------------------|
| 3.11 (warm/shared time-sliced GPU node) | NVIDIA/NVENC modules + container toolkit; node launched warm by Karpenter from this AMI |
| 5.1 / 5.2 / 5.3 (Nix-only, no Debian/Ubuntu) | Entire system built by Nix via `nixos-generators`; no external base image |
| 10.1 / 10.2 (logs + metrics to CloudWatch) | `amazon-cloudwatch-agent` systemd service + `/etc/amazon-cloudwatch-agent/config.json` |
| 17.1 (draining / hardening / boot critical path) | No SSH, no getty, no login users; minimal initrd; volatile journald |
