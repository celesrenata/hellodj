{
  description = ''
    HelloDJ pre-baked minimal NixOS GPU transcode-node AMI.

    Builds an EBS-backed Amazon Machine Image (aarch64 / AWS Graviton G5g) from
    the `gpu-node.nix` NixOS configuration using nixos-generators' `amazon-image`
    format. Karpenter / the transcode node group launches `g5g.xlarge` Spot
    instances directly from this AMI so cold-boot reduces to
    "kernel -> initrd -> mount pre-realized Nix store -> start transcode unit"
    (no store download, no nixos-rebuild, no activation phase).

    This AMI is SPECIFIC to the GPU transcode node group. App nodes run the
    Nix-built OCI container images instead. Both paths are 100% Nix-built with
    NO Ubuntu/Debian base (Requirement 5).

    ONE SHARED IMAGE ACROSS ALL STAGES (R8.4): a single build of this flake's
    `amazonImage` output is the one shared GPU_AMI for the single GPU_Host that
    runs Beta, Staging, and Production. There is no per-stage GPU AMI and no
    per-stage GPU instance (R8.3/8.4); the three stages are isolated by distinct
    Stage_Endpoints (EKS namespace + port + `<stage>.<region>.hellodj.bot`),
    NOT by separate images.

    GPU SCALE-TO-ZERO CONTEXT (R8.5/8.6, design §8): the shared, time-sliced
    Karpenter GPU NodePool that launches instances from this AMI scales the GPU
    to zero after a continuous idle window with no active transcode workload
    (default 300 s, configurable within 60–900 s) and scales back up on GPU
    workload arrival, so the GPU bills only under load. That policy lives in the
    NodePool/cluster config (`platform/infra/lib/eks-stack.ts`, modeled by
    `gpu_idle_decision`), not in this image — the AMI is identical regardless of
    stage.

    All flake inputs reference upstream via `github:owner/repo/branch` (never
    `path:`), so `nix flake update <input>` synchronizes future upstream merges
    (R11.1/11.3, NixOS declarative workflow).

    Requirements: 3.11 (warm/shared time-sliced GPU), 5.1/5.2/5.3 (Nix-only,
    no Debian/Ubuntu), 8.4/8.5 (single shared GPU AMI, scale-to-zero context),
    10.1/10.2 (CloudWatch logs+metrics), 11.1/11.3 (github: pins, no path:),
    17.1 (hardening / boot critical path).

    Build:
      nix build .#amazonImage          # default aarch64 amazon-image
    or, equivalently, using the nixos-generators CLI:
      nixos-generate -f amazon --system aarch64-linux -c ./gpu-node.nix

    The produced artifact is a raw/VHD disk image plus AMI metadata that the
    upload step (aws ec2 import-image / register-image, wired in the pipeline)
    registers as an AMI for the transcode node group. See README.md.
  '';

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixos-generators = {
      url = "github:nix-community/nixos-generators";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, nixos-generators }:
    let
      # The GPU node is AWS Graviton (aarch64). The build itself is performed on
      # an aarch64 builder (native Graviton CI or remote aarch64 builder); we do
      # not cross-compile the image here.
      system = "aarch64-linux";

      pkgs = import nixpkgs { inherit system; };

      # -----------------------------------------------------------------------
      # The bakeable AMI artifact. `nixos-generators` `amazon-image` format
      # produces the EBS-backed disk image + AMI metadata from gpu-node.nix.
      # This is exactly what `nixos-generate -f amazon` produces (task 17.2);
      # the flake output and the CLI share the same `amazon` format generator.
      # -----------------------------------------------------------------------
      amazonImage = nixos-generators.nixosGenerate {
        inherit system;
        format = "amazon";
        modules = [ ./gpu-node.nix ];
      };

      # =======================================================================
      # GPU AMI build integration check (task 17.2, R5.2 / R12.4).
      # =======================================================================
      #
      # R12.4: "WHEN `nixos-generate -f amazon` (or the `infra/ami` flake build)
      # is run, THE build SHALL exit with status 0 and produce the GPU_AMI image
      # artifact." This mirrors the fork flakes' integration-check pattern (e.g.
      # lavaplayer's `hermeticBuild`, Lavalink's `imageLayout`): the check takes
      # the built artifact as a BUILD INPUT, so Nix must first realize
      # `amazonImage` — i.e. run the same `amazon`-format generator that
      # `nixos-generate -f amazon` runs — to completion (exit 0) before this
      # check's own build begins. If the AMI build fails, its non-zero exit
      # propagates and the check never runs (fail-fast). Reaching this check
      # therefore already proves the "exit 0" half of R12.4; the check then
      # asserts the "produced the GPU_AMI image artifact" half against the
      # realized output.
      #
      # `nixos-generators`' `amazon` format output is a store path containing:
      #   * a disk image file — the EBS-backed image (typically `nixos.vhd`,
      #     or a `*.raw` / `*.img` / `*.qcow2` depending on the generator
      #     version), and
      #   * `nix-support/image-info.json` — the AMI metadata (the disk image
      #     filename, logical/physical size, boot mode, etc.) that the pipeline
      #     upload/register step (`aws ec2 import-snapshot` / `register-image`)
      #     consumes to register the AMI.
      #
      # The check asserts BOTH are present and non-empty, so a build that
      # somehow produced an empty or metadata-less result is rejected.
      #
      # BUILDER-AVAILABILITY GATING (mirrors the fork flakes + README): this is
      # an aarch64-linux image (AWS Graviton). It builds only WHERE a Nix
      # builder for aarch64-linux is available — a native aarch64 runner or a
      # configured aarch64 remote builder — exactly as R12.2/R12.4 scope the
      # build-producing checks ("WHERE a Nix builder is available for the target
      # system"). On a host without that builder the derivation cannot be
      # realized and is skipped by the harness, the same way the fork `#image`
      # builds are gated; the check itself adds no new gating logic — it simply
      # inherits the artifact's own build requirements by depending on it.
      # -----------------------------------------------------------------------
      amazonImageBuildCheck =
        pkgs.runCommand "gpu-ami-amazon-image-build-check" { } ''
          set -eu
          img="${amazonImage}"
          echo "GPU AMI build integration check (task 17.2, R5.2/R12.4)"
          echo "  Realized amazon-image artifact: $img"

          # Reaching this point means Nix already realized `amazonImage` (the
          # `nixos-generate -f amazon` generator) to completion with exit 0 —
          # the "exit 0" half of R12.4. Now assert it PRODUCED the GPU_AMI
          # artifact: AMI metadata + a non-empty disk image.

          # --- AMI metadata (nix-support/image-info.json) must be present -----
          meta="$img/nix-support/image-info.json"
          if [ ! -s "$meta" ]; then
            echo "FAIL (R12.4): AMI metadata $meta missing/empty" >&2
            echo "--- artifact tree ---" >&2
            find "$img" -maxdepth 2 >&2 || true
            exit 1
          fi
          echo "OK: AMI metadata present:"
          cat "$meta"
          echo

          # --- a non-empty EBS-backed disk image must be present --------------
          # The amazon format emits one disk image; accept the common
          # extensions across nixos-generators versions (.vhd/.raw/.img/.qcow2).
          disk=""
          for f in "$img"/*.vhd "$img"/*.raw "$img"/*.img "$img"/*.qcow2; do
            [ -e "$f" ] || continue
            disk="$f"
            break
          done
          if [ -z "$disk" ] || [ ! -s "$disk" ]; then
            echo "FAIL (R12.4): no non-empty AMI disk image found in $img" >&2
            echo "--- artifact tree ---" >&2
            find "$img" -maxdepth 2 >&2 || true
            exit 1
          fi
          echo "OK: EBS-backed AMI disk image present: $disk"

          echo "OK (R5.2/R12.4): amazon-image build exited 0 and produced the" \
               "GPU_AMI artifact (disk image + AMI metadata)"
          touch "$out"
        '';
    in
    {
      # -----------------------------------------------------------------------
      # The bakeable AMI artifact (see `amazonImage` above).
      # -----------------------------------------------------------------------
      packages.${system} = {
        amazonImage = amazonImage;

        # `nix build` with no attribute targets the AMI.
        default = amazonImage;
      };

      # -----------------------------------------------------------------------
      # The NixOS system configuration behind the image, exposed so it can be
      # evaluated / introspected (e.g. `nix eval`, tests) without building the
      # full disk image.
      # -----------------------------------------------------------------------
      # gpu-node.nix already imports the amazon-image virtualisation profile via
      # `modulesPath`, so it is a self-contained NixOS configuration.
      nixosConfigurations.gpu-node = nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [ ./gpu-node.nix ];
      };

      # `nix flake check` evaluates/builds the AMI artifact and runs the build
      # integration check (task 17.2).
      checks.${system} = {
        # Realizing this forces the `amazon`-format build (== `nixos-generate
        # -f amazon`) to exit 0 (R5.2/R12.4).
        amazonImage = amazonImage;

        # The GPU AMI build integration check (task 17.2): asserts the build
        # exits 0 AND produces the GPU_AMI artifact (disk image + AMI metadata).
        amazonImageBuild = amazonImageBuildCheck;
      };
    };
}
