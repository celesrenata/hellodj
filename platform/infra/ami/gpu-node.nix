# =============================================================================
# gpu-node.nix — Pre-baked minimal NixOS GPU transcode-node AMI configuration
# =============================================================================
#
# This NixOS module declaratively defines the host system for the HelloDJ
# GPU transcode node group (Decision D3, Requirements 3.11, 5.1-5.3, 10.1,
# 10.2, 17.1). It is baked into an EBS-backed AMI ahead of time via
# `nixos-generators`' `amazon-image` format on aarch64 (AWS Graviton G5g /
# `g5g.xlarge` Spot). Karpenter / the transcode node group launches instances
# directly from that AMI, so boot reduces to:
#
#     kernel -> initrd -> mount pre-realized Nix store -> start transcode unit
#
# There is NO store download, NO `nixos-rebuild`, and NO activation phase on
# the node — the closure is already realized in the image.
#
# This AMI is SPECIFIC to the GPU transcode node group. The app node group
# runs the Nix-built OCI container images on a standard Graviton node group;
# both paths are 100% Nix-built with no Ubuntu/Debian base (R5).
#
# ONE SHARED IMAGE ACROSS ALL STAGES (R8.4): this is the single shared GPU_AMI
# used by the single GPU_Host that runs Beta, Staging, and Production. There is
# NO separate GPU AMI (and no separate GPU instance) per Deployment_Stage
# (R8.3/8.4). The three stages are isolated by distinct Stage_Endpoints (EKS
# namespace + port + DNS hostname `<stage>.<region>.hellodj.bot`, wired in the
# CDK workloads/eks stacks per design §8), NOT by separate images or hosts.
#
# GPU SCALE-TO-ZERO CONTEXT (R8.5/8.6, design §8): the transcode workload that
# runs on this node is fronted by a single shared, time-sliced Karpenter GPU
# NodePool that scales the GPU to zero after a continuous idle window with no
# active transcode workload — default 300 s, configurable within the range
# 60–900 s — and scales back up when a GPU-requiring workload arrives, so the
# GPU bills only under load. That idle-window / scale-to-zero policy is a
# NodePool/cluster concern (modeled by `gpu_idle_decision` in
# hellodj_platform_logic and wired in `platform/infra/lib/eks-stack.ts`), not
# an in-image setting; this AMI is deliberately identical regardless of which
# stage's pods land on the node it boots.
#
# The transcode workload container (`hls-transcode`) runs ON this node — the
# AMI provides the immutable host plus the NVIDIA/NVENC userspace + kernel
# modules that the container's FFmpeg NVENC path binds to.
#
# Hardening / trimming rationale (all declarative here):
#   * No interactive access: OpenSSH, getty, and user accounts are removed.
#     The node is immutable cattle; it is never logged into. This shrinks the
#     boot critical path and removes the SSH attack surface (R17.1 boot
#     critical path / hardening).
#   * Minimal closure: only the transcode/visualizer workload deps, the
#     NVIDIA/NVENC userspace + kernel modules, and the CloudWatch agent are
#     included. Docs, locales, and non-essential units are stripped; initrd is
#     minimized to the drivers needed to mount root.
#   * Logs via CloudWatch agent: a lean amazon-cloudwatch-agent systemd
#     service ships node/container logs + metrics to CloudWatch Logs (and on to
#     the S3 Hive Log_Store), so observability does not depend on host login
#     (R10.1, R10.2).
#   * IAM instance role: the node assumes an instance role for CloudWatch,
#     ECR/Nix cache, and Bedrock/SDK access — NO static credentials on host.
#   * Minimal root storage: trimmed closure + tmpfs-backed HLS scratch means
#     the root gp3 EBS volume is small (~8-16 GiB). HLS segments live on
#     RAM-backed tmpfs during transcode and are served/persisted via
#     S3/CloudFront.
# =============================================================================

{ lib, pkgs, modulesPath, ... }:

let
  # RAM-backed HLS scratch mount point. Mirrors the legacy on-prem tmpfs at
  # /tmp/hellodj_hls (see hellodj-architecture steering: "hls-tmp emptyDir
  # Memory 2Gi"). The transcode container writes HLS segments here; they are
  # served/persisted via S3/CloudFront and never need durable node storage.
  hlsScratchPath = "/var/lib/hls-scratch";

  # Size cap for the HLS scratch tmpfs. Kept small (RAM-backed, ephemeral);
  # segments are flushed to S3 continuously so this never needs to be large.
  hlsScratchSizeMiB = 2048;
in
{
  imports = [
    # nixos-generators provides the amazon-image profile via its `format`
    # attribute; we also pull in the upstream EC2/Amazon image profile so the
    # grub/systemd-boot + growpart + ec2 metadata plumbing is in place. The
    # `amazon-image` format wires the EBS/AMI specifics; this import gives the
    # in-guest EC2 integration (SSM-free, cloud-init-free minimal variant).
    "${modulesPath}/virtualisation/amazon-image.nix"
  ];

  # ---------------------------------------------------------------------------
  # Platform / architecture — AWS Graviton (aarch64). The GPU node shares the
  # ARM64 CPU_Architecture of the rest of the fleet (R3.7, R4.1).
  # ---------------------------------------------------------------------------
  nixpkgs.hostPlatform = lib.mkDefault "aarch64-linux";

  # ---------------------------------------------------------------------------
  # NO interactive access surface (R17.1 hardening / boot-critical-path).
  #
  # The node is immutable cattle. Removing SSH, getty, and login users shrinks
  # the boot critical path and eliminates the remote-login attack surface.
  # ---------------------------------------------------------------------------
  # mkForce: the amazon-image profile enables OpenSSH by default; the transcode
  # node has no interactive access, so we force it off (R17.1).
  services.openssh.enable = lib.mkForce false;

  # Disable every getty (physical + serial autlogin). There is no console
  # login on this node.
  services.getty.autologinUser = null;
  systemd.services."getty@".enable = false;
  systemd.services."serial-getty@".enable = false;

  # Immutable users: no interactive accounts may be created, and the root
  # account has no password and no login shell. mutableUsers=false makes the
  # user set fully declarative (nothing can be added at runtime).
  users.mutableUsers = false;
  users.allowNoPasswordLogin = true;
  users.users.root = {
    hashedPassword = "!"; # locked — no password login
    shell = pkgs.shadow + "/bin/nologin";
  };

  # No sudo surface either — nothing to escalate to.
  security.sudo.enable = false;

  # ---------------------------------------------------------------------------
  # NVIDIA driver + NVENC userspace and kernel modules for the T4G / G5g GPU
  # (R3.11). The hls-transcode container's FFmpeg NVENC path binds to this
  # host-provided userspace + kernel module set.
  # ---------------------------------------------------------------------------
  # Allow the (unfree) NVIDIA driver in the closure.
  nixpkgs.config.allowUnfree = true;

  # X server is off (headless transcode host) but the NVIDIA kernel modules +
  # userspace libraries are still enabled for compute/NVENC.
  services.xserver.enable = false;

  hardware.graphics.enable = true;
  hardware.nvidia = {
    # Manage the kernel modules declaratively; do NOT run a display manager.
    modesetting.enable = true;
    # Open-source kernel modules — supported on the datacenter GPUs used by the
    # G5g family and keeps the closure free of the legacy proprietary kmod
    # where possible.
    open = lib.mkDefault true;
    nvidiaSettings = false; # no GUI settings tool on a headless node
    # NVENC/NVDEC + CUDA userspace comes from the driver package below.
  };

  # Expose the NVIDIA GPU to the video encode group and ensure the NVENC/
  # kernel modules load early. `nvidia` pulls in the DRM/UVM/modeset kmods.
  services.xserver.videoDrivers = [ "nvidia" ];
  boot.kernelModules = [ "nvidia" "nvidia_uvm" "nvidia_modeset" "nvidia_drm" ];

  # Container runtime GPU access: expose the NVIDIA container toolkit so the
  # hls-transcode OCI container can bind /dev/nvidia* and the userspace libs.
  hardware.nvidia-container-toolkit.enable = true;

  # ---------------------------------------------------------------------------
  # CloudWatch agent — lean systemd service shipping node/container logs and
  # metrics to CloudWatch Logs (and onward to the S3 Hive Log_Store).
  # Observability does not depend on host login (R10.1, R10.2).
  # ---------------------------------------------------------------------------
  systemd.services.amazon-cloudwatch-agent = {
    description = "Amazon CloudWatch Agent (logs + metrics -> CloudWatch)";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      # The agent reads its config from the baked file below and authenticates
      # to CloudWatch via the EC2 instance role (no static credentials).
      ExecStart =
        "${pkgs.amazon-cloudwatch-agent}/bin/amazon-cloudwatch-agent"
        + " -config /etc/amazon-cloudwatch-agent/config.json";
      Restart = "always";
      RestartSec = 5;
      # Lean footprint.
      DynamicUser = false;
      User = "root";
    };
  };

  # Minimal CloudWatch agent config: collect node + container logs and basic
  # host/GPU metrics. Log group is namespaced per the design's observability
  # stack; the S3 Hive Log_Store subscription is wired in the analytics stack.
  environment.etc."amazon-cloudwatch-agent/config.json".text = builtins.toJSON {
    agent = {
      metrics_collection_interval = 60;
      run_as_user = "root";
    };
    logs = {
      logs_collected = {
        files = {
          collect_list = [
            {
              file_path = "/var/log/hellodj-transcode/*.log";
              log_group_name = "/hellodj/transcode/node";
              log_stream_name = "{instance_id}";
              retention_in_days = 14;
            }
          ];
        };
      };
    };
    metrics = {
      namespace = "HelloDJ/Transcode";
      append_dimensions = {
        InstanceId = "\${aws:InstanceId}";
      };
      metrics_collected = {
        cpu = { measurement = [ "cpu_usage_active" ]; totalcpu = true; };
        mem = { measurement = [ "mem_used_percent" ]; };
        nvidia_gpu = {
          measurement = [
            "utilization_gpu"
            "utilization_memory"
            "encoder_stats_session_count"
          ];
        };
      };
    };
  };

  # ---------------------------------------------------------------------------
  # tmpfs HLS scratch — RAM-backed segment scratch during transcode. Segments
  # are served/persisted via S3/CloudFront, so this is ephemeral and small.
  # ---------------------------------------------------------------------------
  fileSystems.${hlsScratchPath} = {
    device = "tmpfs";
    fsType = "tmpfs";
    options = [
      "size=${toString hlsScratchSizeMiB}m"
      "mode=1777"
      "noexec"
      "nosuid"
      "nodev"
    ];
  };
  # Make the scratch path discoverable by the transcode workload.
  environment.variables.HELLODJ_HLS_SCRATCH = hlsScratchPath;

  # ---------------------------------------------------------------------------
  # Minimal closure — strip docs, locales, and non-essential units; minimize
  # initrd. Targets a small ~8-16 GiB gp3 root.
  # ---------------------------------------------------------------------------
  documentation.enable = false;
  documentation.man.enable = false;
  documentation.info.enable = false;
  documentation.doc.enable = false;
  documentation.nixos.enable = false;

  # Trim locales to a single UTF-8 locale.
  i18n.defaultLocale = "en_US.UTF-8";
  i18n.supportedLocales = [ "en_US.UTF-8/UTF-8" "C.UTF-8/UTF-8" ];

  # No firewall management daemon churn beyond the minimum; the node's ingress
  # is governed by the EKS/EC2 security groups, not host iptables state.
  networking.firewall.enable = lib.mkDefault true;

  # Minimal initrd: only include the storage/GPU drivers actually needed to
  # mount root and bring the GPU up. nixos-generators' amazon-image profile
  # already pulls the NVMe/EBS drivers; we avoid adding anything broad.
  boot.initrd.systemd.enable = lib.mkDefault true;

  # Keep the system journal small and volatile — logs ship to CloudWatch, not
  # to durable local storage.
  services.journald.extraConfig = ''
    Storage=volatile
    RuntimeMaxUse=64M
    SystemMaxUse=64M
  '';

  # ---------------------------------------------------------------------------
  # Root storage — small gp3. The trimmed closure + tmpfs HLS scratch means no
  # large data disk is needed on the GPU node. `growPartition` lets the root FS
  # expand to whatever the AMI's EBS volume size is (targeted 8-16 GiB gp3).
  # ---------------------------------------------------------------------------
  boot.growPartition = true;

  # ---------------------------------------------------------------------------
  # IAM instance role — NO static credentials baked into the host. The
  # CloudWatch agent, ECR/Nix-cache pulls, and any SDK calls (Bedrock, S3)
  # authenticate via the EC2 instance metadata role attached by the node group
  # / Karpenter provisioner (wired in infra/lib/eks-stack.ts, task 16.3).
  #
  # This assertion documents the contract: there must be no AWS access-key /
  # secret-key material in the image. It is intentionally a no-op guard that
  # keeps the rationale attached to the config.
  # ---------------------------------------------------------------------------
  environment.etc."hellodj/ami-provenance".text = ''
    HelloDJ GPU transcode-node AMI (pre-baked minimal NixOS).
    Auth: EC2 IAM instance role only — no static credentials on host.
    Access surface: no SSH, no getty, no login users (immutable cattle).
    HLS scratch: tmpfs at ${hlsScratchPath} (RAM-backed, ephemeral).
    GPU: NVIDIA/NVENC userspace + kernel modules for G5g (aarch64).
  '';

  # Pin a stable state version for the baked image.
  system.stateVersion = lib.mkDefault "24.05";
}
