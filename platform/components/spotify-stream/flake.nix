{
  description = ''
    HelloDJ spotify-stream component — direct Spotify audio streaming sidecar
    built on librespot (the Python `librespot` reimplementation of the Spotify
    streaming client) + aiohttp, packaged into a Nix-built OCI image on a
    Nix-built Python base. NO Ubuntu/Debian base layers (Requirements 9.1/9.2).

    ## Multi-tenant (multi-tenant-source-streaming section 2)

    The single global librespot session is GONE. This sidecar serves EVERY
    guild from that guild's owning user's Spotify account: each request carries
    the `guild_id` in its path, the sidecar resolves `guild→owner_sub`
    server-side, resolves that user's Spotify credential (with the one-time
    captured librespot reusable blob) from the unified per-user credential store
    (`hellodj-core` DynamoDB + the source-credentials KMS CMK, Decrypt-only),
    and builds/serves a per-user librespot session from a bounded, LRU-evicted
    pool. There is NO shared-account fallback (R3.6/R10.5).

    Endpoints (port 8802):
      GET  /stream/<guild_id>/<track_id>   -> raw audio (transcoded to MP3)
      GET  /preload/<guild_id>/<track_id>  -> warm the per-user track cache
      GET  /health                         -> liveness + per-sub pool state
      GET  /auth/status                    -> multi-session auth state (per sub)
      POST /auth/librespot/start           -> begin one-time capture (task 2.2)
      POST /auth/librespot/complete        -> finish capture, return reusable blob

    ## Authentication (librespot's own OAuth — captured ONCE per user by web-ui)

    librespot builds a per-user `Session` NON-INTERACTIVELY from a reusable
    `{username, credentials, type}` blob captured once at connect time (the
    web-ui orchestrates it via this sidecar's `/auth/librespot/*` endpoints and
    stores it INSIDE the envelope-encrypted credential blob). NOTHING secret is
    baked into the image. Requires a Spotify Premium account (librespot cannot
    stream free accounts; a non-Premium user surfaces as per-sub
    `failed(not_premium)`).

    New env: HELLODJ_CORE_TABLE, HELLODJ_SOURCE_CREDS_KMS_KEY_ID, AWS_REGION,
    SPOTIFY_MAX_SESSIONS, SPOTIFY_SESSION_IDLE_TIMEOUT, DATA_DIR.
  '';

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    # Default target is aarch64-linux (AWS Graviton). x86_64-linux is provided
    # only as a documented fallback per the dependency-compatibility gate.
    flake-utils.lib.eachSystem [ "aarch64-linux" "x86_64-linux" ] (system:
      let
        pkgs = import nixpkgs { inherit system; };

        # ------------------------------------------------------------------
        # Python runtime: `librespot` (the Python Spotify streaming client) +
        # `aiohttp` (HTTP server), plus `boto3` for the unified credential store
        # (DynamoDB read + KMS Decrypt) the multi-tenant resolver uses. All in
        # nixpkgs, so this is a pure Nix build (no pip, no Debian).
        # ------------------------------------------------------------------
        pythonEnv = pkgs.python3.withPackages (ps: [
          ps.librespot
          ps.aiohttp
          ps.boto3
          ps.botocore
        ]);

        # ffmpeg is invoked as a subprocess to transcode Spotify's non-standard
        # OGG Vorbis to MP3 (lavaplayer's native decoder chokes on the raw
        # headers). A headless build is enough (no X/SDL); keep the closure lean.
        ffmpeg = pkgs.ffmpeg-headless;

        # Reproducible source: strip transient files (__pycache__/*.pyc/*.pyo,
        # .git, caches) so the derivation input hash depends ONLY on real source
        # content (matches the tidal-stream flake — keeps the S3 cache hitting).
        filteredSrc = pkgs.lib.cleanSourceWith {
          src = ./.;
          filter = path: _type:
            let base = baseNameOf path;
            in base != "__pycache__"
              && base != ".git"
              && base != ".pytest_cache"
              && base != ".ruff_cache"
              && base != ".hypothesis"
              && !pkgs.lib.hasSuffix ".pyc" base
              && !pkgs.lib.hasSuffix ".pyo" base;
        };

        # Assemble the app source tree: the spotify_stream package + the shared
        # hellodj_platform_logic package (vendored into the source tree by the
        # pipeline before the Nix build, same as tidal-stream).
        appSrc = pkgs.runCommand "hellodj-spotify-stream-src" { src = filteredSrc; } ''
          mkdir -p $out/app
          cp -r $src/spotify_stream $out/app/spotify_stream
          # Shared platform logic package (copied into source tree by pipeline)
          if [ -d "$src/hellodj_platform_logic" ]; then
            cp -r "$src/hellodj_platform_logic" $out/app/hellodj_platform_logic
          fi
        '';

        # ------------------------------------------------------------------
        # OCI image. buildLayeredImage keeps the Python runtime, ffmpeg, and the
        # app in separate Nix-closure layers — no FROM ubuntu/debian.
        # ------------------------------------------------------------------
        image = pkgs.dockerTools.buildLayeredImage {
          name = "hellodj-spotify-stream";
          tag = "nix";

          # Only Nix-built closures land in the image. `ffmpeg` is on PATH for
          # the transcode subprocess; `cacert` for TLS to Spotify/AWS.
          contents = [ pythonEnv ffmpeg pkgs.cacert pkgs.coreutils ];

          extraCommands = ''
            cp -r ${appSrc}/app opt-app
            # `cp -r` from the Nix store copies read-only perms, so the copied
            # tree (and opt-app itself) is not writable — a bare `mkdir
            # opt-app/data` then fails with "Permission denied" in the layered-
            # image builder. Make the app dir writable, create the per-user
            # DATA_DIR root, and leave it world-writable so the sidecar (running
            # as an arbitrary UID) can create per-<sub> cache subdirs at runtime.
            chmod -R u+w opt-app
            mkdir -p opt-app/data
            chmod 0777 opt-app/data
          '';

          config = {
            WorkingDir = "/opt-app";
            ExposedPorts = { "8802/tcp" = { }; };
            Env = [
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              "PYTHONUNBUFFERED=1"
              "SPOTIFY_STREAM_PORT=8802"
              # Per-user librespot caches live under DATA_DIR/<sub>/; back it
              # with a persistent volume so a restart restores sessions (R9.3).
              "DATA_DIR=/opt-app/data"
              # Ensure the ffmpeg on PATH is the Nix one (called by bare name).
              "PATH=${pythonEnv}/bin:${ffmpeg}/bin"
            ];
            Entrypoint = [ "${pythonEnv}/bin/python" "-m" "spotify_stream" ];
          };
        };
      in
      {
        packages = {
          default = image;
          image = image;
          # Expose the individual derivations for inspection/testing.
          pythonEnv = pythonEnv;
        };

        # `nix flake check` evaluates these.
        checks = {
          image-builds = image;
        };
      });
}
