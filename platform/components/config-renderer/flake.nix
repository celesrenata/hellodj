{
  description = ''
    HelloDJ config-renderer component — renders the complete Lavalink
    application.yml from AWS Secrets Manager + DynamoDB. Runs as an init
    container / one-shot Job. Nix-built OCI image, NO Ubuntu/Debian base
    (R5.1/5.2/5.3).

    Secrets read at runtime via IAM (IRSA). Nothing baked in (R6.1).
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
          boto3
          botocore
          pyyaml
        ];

        pythonEnv = python.withPackages pythonDeps;

        appSrc = pkgs.runCommand "hellodj-config-renderer-src" { src = ./.; } ''
          mkdir -p $out/app
          cp -r $src/config_renderer $out/app/config_renderer 2>/dev/null || true
          find $src -maxdepth 1 -name '*.py' -exec cp {} $out/app/ \;
        '';

        image = pkgs.dockerTools.buildLayeredImage {
          name = "hellodj-config-renderer";
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
              "-m" "config_renderer"
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
