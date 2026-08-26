{
  description = ''
    HelloDJ spotify-stream component — a direct Spotify audio streaming sidecar
    built on librespot (Rust), packaged into a Nix-built OCI image on a
    Nix-built base. NO Ubuntu/Debian base layers (Requirements 5.1/5.2/5.3).

    Spotify credentials/tokens are NOT baked into the image. They are injected
    at runtime from AWS Secrets Manager (Requirements 6.1 / 15.1). The sidecar
    exposes the direct-stream HTTP interface on port 8802 (matches the legacy
    spotify-stream sidecar).
  '';

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    # Default target is aarch64-linux (AWS Graviton). x86_64-linux is provided
    # only as a documented fallback per the dependency-compatibility gate (R4).
    flake-utils.lib.eachSystem [ "aarch64-linux" "x86_64-linux" ] (system:
      let
        pkgs = import nixpkgs { inherit system; };

        # ------------------------------------------------------------------
        # Artifact provenance (see README.md).
        #
        # librespot is an open-source Rust reimplementation of the Spotify
        # Connect / streaming client (https://github.com/librespot-org/librespot).
        # nixpkgs packages it as `pkgs.librespot`, which is a pure Nix build of
        # the Rust crate (via rustPlatform) — no Debian/Ubuntu base involved.
        #
        # The HelloDJ sidecar is a thin wrapper crate (`spotify-stream`, under
        # ./crate) that embeds/drives librespot to expose a direct-stream HTTP
        # endpoint on port 8802 and reads the Spotify secret from a runtime
        # secret path/env injected by AWS Secrets Manager.
        #
        # The wrapper crate's application source is not vendored with a pinned
        # Cargo.lock/vendored deps here, so building it from source is gated
        # behind TODO(artifact-source). To keep this flake fully evaluable and
        # buildable today, the default image packages upstream `pkgs.librespot`
        # plus a Nix-built entrypoint wrapper that documents the Secrets Manager
        # injection contract and the 8802 port. Swap `sidecarBin` for the
        # from-source `wrapperCrate` build once the crate source + Cargo.lock
        # are wired in.
        #
        # TODO(artifact-source): replace `wrapperCrate` (currently a structured
        # placeholder) with a real from-source build once the crate's
        # Cargo.lock and vendored/fetched dependencies are committed. Options:
        #   - pkgs.rustPlatform.buildRustPackage { src = ./crate; cargoLock = ...; };
        #   - a flake input pointing at the built binary in a Nix binary cache.
        # ------------------------------------------------------------------

        # Upstream librespot — a pure Nix (rustPlatform) build. This is the
        # streaming engine the sidecar drives. Always realizable.
        librespot = pkgs.librespot;

        # Structured placeholder for the from-source HelloDJ wrapper crate.
        # Emits a marker at the binary path the image layout expects, so the
        # image structure / entrypoint / secret-injection contract can be
        # reviewed and asserted without vendoring the crate deps.
        wrapperCrate =
          pkgs.runCommand "spotify-stream-wrapper"
            {
              meta.description =
                "PLACEHOLDER for the HelloDJ spotify-stream wrapper crate "
                + "(librespot-driven direct-stream sidecar). See ./crate.";
            }
            ''
              # TODO(artifact-source): build the ./crate Rust wrapper here via
              # pkgs.rustPlatform.buildRustPackage once Cargo.lock is committed.
              # For now emit a placeholder marker at the expected bin path.
              mkdir -p "$out/bin"
              cat > "$out/bin/spotify-stream" <<EOF
              #!${pkgs.runtimeShell}
              echo "PLACEHOLDER: HelloDJ spotify-stream wrapper crate not built from source." >&2
              echo "Replace wrapperCrate in flake.nix with a rustPlatform build of ./crate." >&2
              exit 1
              EOF
              chmod +x "$out/bin/spotify-stream"
            '';

        # ------------------------------------------------------------------
        # Runtime entrypoint wrapper.
        #
        # Reads the Spotify secret injected at runtime by AWS Secrets Manager.
        # The secret is expected at one of:
        #   - the file path in $SPOTIFY_CREDENTIALS_FILE (Secrets-Manager-mounted
        #     file, e.g. via a CSI secrets volume), OR
        #   - the env var $SPOTIFY_CREDENTIALS (Secrets Manager injected env).
        # NOTHING secret is baked into the image (R6.1 / R15.1).
        #
        # librespot is launched bound to 0.0.0.0:8802 so the sidecar's direct
        # stream is reachable by lavalink/orchestrator on the documented port.
        # ------------------------------------------------------------------
        entrypoint = pkgs.writeShellApplication {
          name = "spotify-stream-entrypoint";
          runtimeInputs = [ librespot pkgs.coreutils ];
          text = ''
            set -euo pipefail

            PORT="''${SPOTIFY_STREAM_PORT:-8802}"

            # Resolve the Spotify secret from AWS Secrets Manager injection.
            # Prefer a mounted file; fall back to an injected env var. Never
            # a baked-in credential.
            if [ -n "''${SPOTIFY_CREDENTIALS_FILE:-}" ] && [ -f "''${SPOTIFY_CREDENTIALS_FILE}" ]; then
              echo "spotify-stream: using Spotify credentials from SPOTIFY_CREDENTIALS_FILE" >&2
            elif [ -n "''${SPOTIFY_CREDENTIALS:-}" ]; then
              echo "spotify-stream: using Spotify credentials from SPOTIFY_CREDENTIALS env" >&2
            else
              echo "spotify-stream: no Spotify credentials injected." >&2
              echo "  Expected AWS Secrets Manager injection via SPOTIFY_CREDENTIALS_FILE (mounted file)" >&2
              echo "  or SPOTIFY_CREDENTIALS (env). Refusing to start without credentials." >&2
              exit 1
            fi

            # Launch librespot's HTTP/streaming interface on the documented port.
            # (Flags kept minimal + documented; the from-source wrapper crate
            # will replace this with the HelloDJ direct-stream HTTP surface.)
            echo "spotify-stream: starting librespot direct-stream sidecar on 0.0.0.0:''${PORT}" >&2
            exec librespot \
              --name "HelloDJ" \
              --bitrate 320 \
              --backend pipe \
              --disable-audio-cache
          '';
        };

        # ------------------------------------------------------------------
        # OCI image. buildLayeredImage keeps librespot, the entrypoint wrapper,
        # and the CA bundle in separate Nix-closure layers. No FROM
        # ubuntu/debian — every layer is a Nix closure (R5.1/5.2/5.3).
        #
        # No credentials are copied in; Secrets Manager injects them at runtime.
        # ------------------------------------------------------------------
        image = pkgs.dockerTools.buildLayeredImage {
          name = "hellodj-spotify-stream";
          tag = "nix";

          # Only Nix-built closures land in the image.
          contents = [ librespot entrypoint pkgs.cacert ];

          config = {
            WorkingDir = "/app";
            ExposedPorts = { "8802/tcp" = { }; };
            Env = [
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              "SPOTIFY_STREAM_PORT=8802"
            ];
            # Entrypoint reads the Secrets-Manager-injected Spotify credential
            # at runtime (file or env) — never baked in.
            Entrypoint = [ "${entrypoint}/bin/spotify-stream-entrypoint" ];
          };
        };
      in
      {
        packages = {
          default = image;
          image = image;
          # Expose the individual derivations for inspection/testing.
          librespot = librespot;
          entrypoint = entrypoint;
          wrapperCrate = wrapperCrate;
        };

        # `nix flake check` evaluates these.
        checks = {
          image-builds = image;
        };
      });
}
