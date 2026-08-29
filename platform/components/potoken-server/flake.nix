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
        # Upstream: Brainicism/bgutil-ytdlp-pot-provider — a Node.js/TypeScript
        # server (the `server/` subproject) that generates YouTube
        # Proof-of-Origin tokens via LuanRT's BgUtils/Botguard interfacing
        # library. The published image's entrypoint is `node build/main.js` and
        # it exposes port 4416. It exposes `POST /get_pot` and `GET /ping`.
        #
        # Pinned to release tag 1.3.2 (rev 7511309…). The `server/` subdir holds
        # a `package.json` + `package-lock.json`; `npx tsc` compiles `src/` to
        # `build/main.js` (see the upstream server/Dockerfile `build_node`
        # stage). One runtime dependency, `canvas`, is a NATIVE module compiled
        # from source, so the build needs the C toolchain + cairo/pango/jpeg/
        # giflib/rsvg/pixman headers wired as `buildInputs` / `nativeBuildInputs`
        # (mirroring nixpkgs' own `node-canvas` packaging).
        # ------------------------------------------------------------------

        src = pkgs.fetchFromGitHub {
          owner = "Brainicism";
          repo = "bgutil-ytdlp-pot-provider";
          rev = "7511309af023b09788dc8f2efc96cc3671291e6c"; # tag 1.3.2
          hash = "sha256-vlhuw0Ci/xfPgLxjeW7E+Pz9Fo6yeME3cyVRf8NAAPU=";
        };

        nodejs = pkgs.nodejs_22;

        # Native libraries the `canvas` npm dependency links against. These
        # match nixpkgs' `node-canvas` buildInputs; without them `npm ci`'s
        # node-gyp step fails to find cairo/pango/etc. The SAME shared libraries
        # must be present at RUNTIME (the compiled `canvas.node` dlopen's them),
        # so they are also added to the image `contents` below — but only these
        # library outputs, never the `.dev` header outputs.
        canvasNativeDeps = with pkgs; [
          cairo
          pango
          libjpeg
          giflib
          librsvg
          pixman
        ];

        potokenApp = pkgs.buildNpmPackage {
          pname = "bgutil-pot-provider";
          version = "1.3.2";

          inherit src;
          # The Node server lives in the `server/` subdirectory of the repo.
          sourceRoot = "${src.name}/server";

          # Hash of the fetched npm dependency closure, computed from
          # server/package-lock.json via `prefetch-npm-deps`. Update alongside
          # the pinned rev whenever the lockfile changes.
          npmDepsHash = "sha256-hpXVvhJm66+ETJdGAbEa/QZ4rxOYBD8RJqSItlNpoOg=";

          nativeBuildInputs = [ pkgs.python3 pkgs.pkg-config ] ++ [ nodejs ];
          buildInputs = canvasNativeDeps;

          # `npm ci` runs canvas's node-gyp native build; allow its lifecycle
          # script (the package.json `allowScripts` lists canvas + @swc/core).
          npmFlags = [ "--legacy-peer-deps" ];

          # `npx tsc` (the package's build step) emits build/main.js.
          buildPhase = ''
            runHook preBuild
            npx tsc
            runHook postBuild
          '';

          # Install the compiled server + its production node_modules so the
          # runtime image can `node build/main.js` with all deps resolved.
          installPhase = ''
            runHook preInstall
            mkdir -p "$out/opt/potoken-server"
            cp -r build "$out/opt/potoken-server/build"
            cp -r node_modules "$out/opt/potoken-server/node_modules"
            cp package.json "$out/opt/potoken-server/package.json"
            runHook postInstall
          '';

          # Skip the default npm build (we run tsc explicitly) and the default
          # npm install (we assemble the layout by hand above).
          dontNpmBuild = true;
          dontNpmInstall = true;
        };

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
          contents = [ nodejs pkgs.cacert ] ++ canvasNativeDeps;

          extraCommands = ''
            mkdir -p opt/potoken-server
            cp -r ${potokenApp}/opt/potoken-server/. opt/potoken-server/
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
