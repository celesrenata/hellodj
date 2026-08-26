{
  description = ''
    HelloDJ yt-cipher component — YouTube player signature/n-param deciphering
    HTTP service (upstream kikkia/yt-cipher, a Deno wrapper around yt-dlp/ejs)
    rebuilt as a Nix-built OCI image on a Nix-built Deno base. NO Ubuntu/Debian
    base layers (Requirements 5.1/5.2/5.3).

    The shared secret (the yt-cipher API_TOKEN) is NOT baked into the image; it
    is injected at runtime from AWS Secrets Manager as the API_TOKEN environment
    variable (Requirement 6.1). This is the SAME shared secret used by the
    rendered Lavalink config as `remoteCipher.password` — they must match.

    Listens on port 8001 (upstream default, Requirement 6.1).
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
        # Upstream: kikkia/yt-cipher (branch `master`) — an HTTP API wrapper
        # around yt-dlp/ejs that performs YouTube player-script signature and
        # n-parameter deciphering. It is a Deno application; the entrypoint is
        # `deno run ... server.ts`, and it depends on a patched copy of the
        # yt-dlp/ejs sources (applied by the repo's scripts/patch-ejs.ts).
        #
        # The upstream source is NOT vendored in this repo. The derivation
        # below is a structured placeholder: point `src` at the real source
        # (a fetchFromGitHub of kikkia/yt-cipher plus the pinned yt-dlp/ejs
        # checkout, or a release artifact) to make the build fully realizable.
        # The flake stays evaluable so the image structure, the Deno base, the
        # port, and the Secrets-Manager-injected API_TOKEN contract can be
        # reviewed and asserted without the sources present.
        #
        # TODO(artifact-source): replace mkPlaceholderApp below with a real
        # source fetch/build. Options:
        #   - pkgs.fetchFromGitHub {
        #       owner = "kikkia"; repo = "yt-cipher"; rev = "<pinned>";
        #       hash = "sha256-...";
        #     }
        #     plus a fetch of yt-dlp/ejs pinned to the rev the upstream README
        #     documents, then run scripts/patch-ejs.ts at build time; OR
        #   - a flake input pointing at a pre-built closure in a Nix cache.
        # Deno caches remote deps at first run; for a hermetic image, vendor the
        # Deno dependency cache (DENO_DIR) at build time (deno cache server.ts).
        # ------------------------------------------------------------------

        mkPlaceholderApp = { name, provenance }:
          pkgs.runCommand name
            {
              meta.description =
                "PLACEHOLDER for ${name} — provenance: ${provenance}";
            }
            ''
              # TODO(artifact-source): this emits a placeholder marker instead
              # of the real Deno application sources. Swap this derivation for a
              # fetchFromGitHub/build once the artifact source is wired up. The
              # output PATH and the entrypoint filename are what the image
              # layout depends on, so keep the same server.ts name.
              mkdir -p "$out"
              cat > "$out/server.ts" <<EOF
              // PLACEHOLDER ARTIFACT: ${name}
              // provenance: ${provenance}
              // This is not the real yt-cipher application. Replace the
              // mkPlaceholderApp derivation in flake.nix with a real
              // fetchFromGitHub/build derivation (kikkia/yt-cipher + patched
              // yt-dlp/ejs) and vendor the Deno dependency cache.
              EOF
            '';

        ytCipherApp = mkPlaceholderApp {
          name = "yt-cipher";
          provenance =
            "kikkia/yt-cipher fork, branch master (Deno wrapper around yt-dlp/ejs)";
        };

        # Deno runtime, built/packaged by Nix. No Debian/Ubuntu layers.
        deno = pkgs.deno;

        # ------------------------------------------------------------------
        # OCI image. buildLayeredImage keeps the Deno runtime and the app in
        # separate layers so an app bump does not re-push the Deno layer.
        #
        # API_TOKEN is deliberately absent from the image: it is the shared
        # secret injected at runtime from AWS Secrets Manager (R6.1). Only the
        # non-secret defaults (PORT/HOST/OVERRIDE_PLAYER_VARIANT) are baked in.
        # ------------------------------------------------------------------
        image = pkgs.dockerTools.buildLayeredImage {
          name = "hellodj-yt-cipher";
          tag = "nix";

          # Only Nix-built closures land in the image. No FROM ubuntu/debian.
          contents = [ deno pkgs.cacert ];

          extraCommands = ''
            mkdir -p opt/yt-cipher
            cp ${ytCipherApp}/server.ts opt/yt-cipher/server.ts
          '';

          config = {
            WorkingDir = "/opt/yt-cipher";
            ExposedPorts = { "8001/tcp" = { }; };
            Env = [
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              # Non-secret runtime defaults. The shared secret API_TOKEN is
              # injected at runtime from Secrets Manager and is intentionally
              # NOT set here (R6.1).
              "PORT=8001"
              "HOST=0.0.0.0"
              # Upstream recommends the IAS variant for reliability; the legacy
              # deployment set OVERRIDE_PLAYER_VARIANT=IAS.
              "OVERRIDE_PLAYER_VARIANT=IAS"
            ];
            # Mirrors the upstream Deno entrypoint. Config (incl. the injected
            # API_TOKEN) is read from the environment at runtime, not baked in.
            Entrypoint = [
              "${deno}/bin/deno"
              "run"
              "--allow-net"
              "--allow-read"
              "--allow-write"
              "--allow-env"
              "/opt/yt-cipher/server.ts"
            ];
          };
        };
      in
      {
        packages = {
          default = image;
          image = image;
          # Expose the app derivation for inspection/testing.
          ytCipherApp = ytCipherApp;
        };

        # `nix flake check` evaluates these.
        checks = {
          image-builds = image;
        };
      });
}
