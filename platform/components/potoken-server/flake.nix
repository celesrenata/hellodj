{
  description = ''
    HelloDJ potoken-server component — YouTube Proof-of-Origin token (POT)
    provider (upstream Brainicism/bgutil-ytdlp-pot-provider, a Node.js/
    TypeScript server using LuanRT's BgUtils/Botguard) rebuilt as a Nix-built
    OCI image on a Nix-built Node.js base. NO Ubuntu/Debian base layers
    (Requirements 5.1/5.2/5.3).

    The upstream POT provider has no built-in auth. Where a shared secret is
    used to protect the in-cluster endpoint, it is NOT baked into the image; it
    is injected at runtime from AWS Secrets Manager as an environment variable
    (Requirement 6.1). Only non-secret runtime defaults are baked in.

    Listens on port 4416 (upstream default, Requirement 6.1).
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
        # Upstream: Brainicism/bgutil-ytdlp-pot-provider (branch `master`) — a
        # Node.js/TypeScript server that generates YouTube Proof-of-Origin
        # tokens via LuanRT's BgUtils/Botguard interfacing library. The
        # published image's entrypoint is `node build/main.js` and it exposes
        # port 4416. It exposes `POST /get_pot` and `GET /ping`.
        #
        # The upstream source is NOT vendored in this repo. The derivation
        # below is a structured placeholder: point `src` at the real source
        # (a fetchFromGitHub of Brainicism/bgutil-ytdlp-pot-provider built with
        # pkgs.buildNpmPackage, producing the compiled build/main.js) to make
        # the build fully realizable. The flake stays evaluable so the image
        # structure, the Node base, the port, and the Secrets-Manager-injected
        # secret contract can be reviewed and asserted without the sources.
        #
        # TODO(artifact-source): replace mkPlaceholderApp below with a real
        # source fetch/build. Recommended:
        #   pkgs.buildNpmPackage {
        #     pname = "bgutil-pot-provider";
        #     version = "<pinned>";
        #     src = pkgs.fetchFromGitHub {
        #       owner = "Brainicism"; repo = "bgutil-ytdlp-pot-provider";
        #       rev = "<pinned>"; hash = "sha256-...";
        #     };
        #     # server/ subdir holds the Node server; build emits build/main.js
        #     npmDepsHash = "sha256-...";
        #   }
        # ------------------------------------------------------------------

        mkPlaceholderApp = { name, provenance }:
          pkgs.runCommand name
            {
              meta.description =
                "PLACEHOLDER for ${name} — provenance: ${provenance}";
            }
            ''
              # TODO(artifact-source): this emits a placeholder marker instead
              # of the real compiled Node server. Swap this derivation for a
              # buildNpmPackage/fetchFromGitHub build once the artifact source
              # is wired up. The output PATH and the entrypoint filename
              # (build/main.js) are what the image layout depends on, so keep
              # the same layout.
              mkdir -p "$out/build"
              cat > "$out/build/main.js" <<EOF
              // PLACEHOLDER ARTIFACT: ${name}
              // provenance: ${provenance}
              // This is not the real bgutil POT provider. Replace the
              // mkPlaceholderApp derivation in flake.nix with a real
              // buildNpmPackage build of Brainicism/bgutil-ytdlp-pot-provider
              // (server/) that produces build/main.js.
              console.error("placeholder potoken-server; wire artifact-source");
              process.exit(1);
              EOF
            '';

        potokenApp = mkPlaceholderApp {
          name = "bgutil-pot-provider";
          provenance =
            "Brainicism/bgutil-ytdlp-pot-provider fork, branch master "
            + "(Node.js POT provider, LuanRT BgUtils/Botguard)";
        };

        # Node.js runtime, built/packaged by Nix. No Debian/Ubuntu layers.
        nodejs = pkgs.nodejs_22;

        # ------------------------------------------------------------------
        # OCI image. buildLayeredImage keeps the Node runtime and the app in
        # separate layers so an app bump does not re-push the Node layer.
        #
        # Any shared secret used to protect the endpoint is deliberately absent
        # from the image: it is injected at runtime from AWS Secrets Manager
        # (R6.1). Only the non-secret port default is baked in.
        # ------------------------------------------------------------------
        image = pkgs.dockerTools.buildLayeredImage {
          name = "hellodj-potoken-server";
          tag = "nix";

          # Only Nix-built closures land in the image. No FROM ubuntu/debian.
          contents = [ nodejs pkgs.cacert ];

          extraCommands = ''
            mkdir -p opt/potoken-server/build
            cp ${potokenApp}/build/main.js opt/potoken-server/build/main.js
          '';

          config = {
            WorkingDir = "/opt/potoken-server";
            ExposedPorts = { "4416/tcp" = { }; };
            Env = [
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              # Non-secret runtime default. Any shared secret protecting the
              # endpoint is injected at runtime from Secrets Manager and is
              # intentionally NOT set here (R6.1).
              "PORT=4416"
            ];
            # Mirrors the upstream ENTRYPOINT (`node build/main.js`).
            Entrypoint = [
              "${nodejs}/bin/node"
              "/opt/potoken-server/build/main.js"
            ];
          };
        };
      in
      {
        packages = {
          default = image;
          image = image;
          # Expose the app derivation for inspection/testing.
          potokenApp = potokenApp;
        };

        # `nix flake check` evaluates these.
        checks = {
          image-builds = image;
        };
      });
}
