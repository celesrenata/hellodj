{
  description = ''
    HelloDJ voice-pipeline component — local ONNX wake word detection (the only
    on-box AI) with STT/intent/TTS delegated to Amazon Bedrock + Transcribe +
    Polly over an IAM task role. Nix-built OCI image, NO Ubuntu/Debian base
    (R5.1/5.2/5.3).

    AI access via IAM task role (no static keys, R6.1).
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
          onnxruntime
          boto3
          botocore
          numpy
        ];

        pythonEnv = python.withPackages pythonDeps;

        appSrc = pkgs.runCommand "hellodj-voice-pipeline-src" { src = ./.; } ''
          mkdir -p $out/app
          cp -r $src/voice_pipeline $out/app/voice_pipeline 2>/dev/null || true
          find $src -maxdepth 1 -name '*.py' -exec cp {} $out/app/ \;
        '';

        image = pkgs.dockerTools.buildLayeredImage {
          name = "hellodj-voice-pipeline";
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
              "-m" "voice_pipeline"
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
