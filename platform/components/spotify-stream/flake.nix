{
  description = ''
    HelloDJ spotify-stream component — a direct Spotify audio streaming sidecar
    built on librespot (the Python `librespot` reimplementation of the Spotify
    streaming client) + aiohttp, packaged into a Nix-built OCI image on a
    Nix-built Python base. NO Ubuntu/Debian base layers (Requirements
    5.1/5.2/5.3).

    This is the AWS port of the WORKING on-prem `spotify-stream/app.py` service
    (the earlier Rust `crate/` was a never-finished skeleton). It exposes the
    direct-stream HTTP interface on port 8802 (matches the legacy sidecar):
      GET  /stream/<track_id>   -> raw audio (OGG Vorbis, transcoded to MP3)
      GET  /preload/<track_id>  -> warm the track cache
      GET  /health              -> service + session health
      GET  /auth/status         -> OAuth state (pending URL or authenticated)
      POST /auth/reset          -> force re-auth

    ## Authentication (librespot's own OAuth — NOT the app client id/secret)

    librespot has its OWN built-in Spotify client; it authenticates a USER
    session via an OAuth URL (surfaced on `/auth/status` and in the logs) and
    then caches a persistent `spotify-credentials.json` in `DATA_DIR`, which it
    restores on restart. It does NOT consume the `hellodj/<stage>/spotify` app
    `{client_id, client_secret}` secret for streaming (that secret is for the
    web-ui's per-guild Spotify connect OAuth, a different flow). So NOTHING
    secret is baked into the image; the librespot session cache lives on a
    persistent `DATA_DIR` volume (R6.1 / R15.1). Requires a Spotify Premium
    account (librespot cannot stream free accounts).
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
        # Python runtime: the SAME dependency set the on-prem service uses —
        # `librespot` (the Python Spotify streaming client) + `aiohttp`. Both
        # are in nixpkgs, so this is a pure Nix build (no pip, no Debian).
        # ------------------------------------------------------------------
        pythonEnv = pkgs.python3.withPackages (ps: [
          ps.librespot
          ps.aiohttp
        ]);

        # The application source (ported verbatim from `spotify-stream/app.py`).
        app = ./app.py;

        # ffmpeg is invoked as a subprocess to transcode Spotify's non-standard
        # OGG Vorbis to MP3 (lavaplayer's native decoder chokes on the raw
        # headers). A headless build is enough (no X/SDL); keep the closure lean.
        ffmpeg = pkgs.ffmpeg-headless;

        # ------------------------------------------------------------------
        # OCI image. buildLayeredImage keeps the Python runtime, ffmpeg, and the
        # app in separate Nix-closure layers — no FROM ubuntu/debian.
        #
        # No credentials are baked in: librespot runs its OAuth flow at runtime
        # and caches the session under DATA_DIR (a persistent volume).
        # ------------------------------------------------------------------
        image = pkgs.dockerTools.buildLayeredImage {
          name = "hellodj-spotify-stream";
          tag = "nix";

          # Only Nix-built closures land in the image. `ffmpeg` is on PATH for
          # the transcode subprocess; `cacert` for TLS to Spotify.
          contents = [ pythonEnv ffmpeg pkgs.cacert ];

          extraCommands = ''
            mkdir -p app
            cp ${app} app/app.py
            mkdir -p app/data
          '';

          config = {
            WorkingDir = "/app";
            ExposedPorts = { "8802/tcp" = { }; };
            Env = [
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              "SPOTIFY_STREAM_PORT=8802"
              # librespot caches its session credentials here; back it with a
              # persistent volume so a restart restores the session instead of
              # forcing a fresh OAuth (R6.1 / R15.1).
              "DATA_DIR=/app/data"
              # Ensure the ffmpeg on PATH is the Nix one (the app calls it by
              # bare name via subprocess).
              "PATH=${pythonEnv}/bin:${ffmpeg}/bin"
            ];
            Entrypoint = [ "${pythonEnv}/bin/python" "/app/app.py" ];
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
