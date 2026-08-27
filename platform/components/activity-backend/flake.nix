{
  description = ''
    HelloDJ activity-backend component — aiohttp Discord Activity server
    (video control, whiteboard, visualizer, synced lyrics) with a WebSocket
    hub. Nix-built OCI image, NO Ubuntu/Debian base (R5.1/5.2/5.3).

    Port 8090. Secrets injected at runtime from AWS Secrets Manager (R6.1).
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

        appSrc = pkgs.runCommand "hellodj-activity-backend-src" { src = ./.; } ''
          mkdir -p $out/app
          cp -r $src/activity_backend $out/app/activity_backend 2>/dev/null || true
          find $src -maxdepth 1 -name '*.py' -exec cp {} $out/app/ \;
        '';

        image = pkgs.dockerTools.buildLayeredImage {
          name = "hellodj-activity-backend";
          tag = "nix";

          contents = [ pythonEnv pkgs.cacert pkgs.coreutils ];

          extraCommands = ''
            cp -r ${appSrc}/app opt-app
          '';

          config = {
            WorkingDir = "/opt-app";
            ExposedPorts = { "8090/tcp" = { }; };
            Env = [
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              "PYTHONUNBUFFERED=1"
            ];
            Entrypoint = [
              "${pythonEnv}/bin/python"
              "-m" "activity_backend.server"
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
