{
  description = ''
    HelloDJ discord-bot-core component — Discord gateway, cog/command
    registration, guild policy, and background watchdogs. Packaged into a
    Nix-built OCI image. NO Ubuntu/Debian base (R5.1/5.2/5.3).

    Bot token injected at runtime from AWS Secrets Manager (R6.1).
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
          discordpy
          boto3
          botocore
          aiohttp
        ];

        pythonEnv = python.withPackages pythonDeps;

        appSrc = pkgs.runCommand "hellodj-discord-bot-core-src" { src = ./.; } ''
          mkdir -p $out/app
          cp -r $src/discord_bot_core $out/app/discord_bot_core 2>/dev/null || true
          # Copy any top-level .py files
          find $src -maxdepth 1 -name '*.py' -exec cp {} $out/app/ \;
          # Shared platform logic package (copied into source tree by pipeline)
          if [ -d "$src/hellodj_platform_logic" ]; then
            cp -r "$src/hellodj_platform_logic" $out/app/hellodj_platform_logic
          fi
        '';

        image = pkgs.dockerTools.buildLayeredImage {
          name = "hellodj-discord-bot-core";
          tag = "nix";

          contents = [ pythonEnv pkgs.cacert pkgs.coreutils ];

          extraCommands = ''
            cp -r ${appSrc}/app opt-app
          '';

          config = {
            WorkingDir = "/opt-app";
            Env = [
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              "PYTHONUNBUFFERED=1"
            ];
            Entrypoint = [
              "${pythonEnv}/bin/python"
              "-m" "discord_bot_core"
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
