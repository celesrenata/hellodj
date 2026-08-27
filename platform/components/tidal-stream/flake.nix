{
  description = ''
    HelloDJ tidal-stream component — direct Tidal audio streaming sidecar.
    First-party single-app-id OAuth token refresh. Nix-built OCI image,
    NO Ubuntu/Debian base (R5.1/5.2/5.3).

    Port 8801. Tidal credentials injected at runtime from AWS Secrets Manager
    (R6.1).
  '';

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachSystem [ "aarch64-linux" "x86_64-linux" ] (system:
      let
        pkgs = import nixpkgs { inherit system; };

        python = pkgs.python314;

        pythonDeps = ps: with ps; [
          aiohttp
          boto3
          botocore
        ];

        pythonEnv = python.withPackages pythonDeps;

        appSrc = pkgs.runCommand "hellodj-tidal-stream-src" { src = ./.; } ''
          mkdir -p $out/app
          cp -r $src/tidal_stream $out/app/tidal_stream 2>/dev/null || true
          find $src -maxdepth 1 -name '*.py' -exec cp {} $out/app/ \;
          # Shared platform logic package (copied into source tree by pipeline)
          if [ -d "$src/hellodj_platform_logic" ]; then
            cp -r "$src/hellodj_platform_logic" $out/app/hellodj_platform_logic
          fi
        '';

        image = pkgs.dockerTools.buildLayeredImage {
          name = "hellodj-tidal-stream";
          tag = "nix";

          contents = [ pythonEnv pkgs.cacert pkgs.coreutils ];

          extraCommands = ''
            cp -r ${appSrc}/app opt-app
          '';

          config = {
            WorkingDir = "/opt-app";
            ExposedPorts = { "8801/tcp" = { }; };
            Env = [
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              "PYTHONUNBUFFERED=1"
            ];
            Entrypoint = [
              "${pythonEnv}/bin/python"
              "-m" "tidal_stream"
            ];
          };
        };
      in
      {
        packages = {
          default = image;
          image = image;
        };
        checks = { image-builds = image; };
      });
}
