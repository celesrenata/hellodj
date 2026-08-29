{
  description = ''
    HelloDJ searxng component — a privacy-respecting SearXNG metasearch engine,
    packaged as a Nix-built OCI image on nixpkgs' `searxng` package. NO
    Ubuntu/Debian base layers (Requirements 5.1/5.2/5.3).

    This is the metasearch backend for the voice-pipeline's Bedrock web-search
    tool: the `mcp-searxng-enhanced` component queries this service's
    `/search?format=json` endpoint, and the voice-pipeline surfaces brief
    results in spoken answers (bedrock-voice-web-search). SearXNG's default
    settings.yml only enables the HTML result format; a baked settings.yml here
    enables the `json` format so the MCP wrapper can consume results, binds
    0.0.0.0:8080, and disables the rate limiter (the only caller is the
    in-cluster MCP wrapper, not the public internet).

    The `secret_key` is a non-secret placeholder here (SearXNG requires the key
    to exist; this instance is internal-only, behind cluster networking, and
    serves no authenticated session). If a real per-stage key is ever wanted it
    is injected at runtime via `SEARXNG_SECRET` from AWS Secrets Manager (R6.1)
    — never baked as a real secret.

    Listens on port 8080.
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

        # SearXNG from nixpkgs (a Python/Flask app). Entry point is
        # `searxng-run`, which calls `searx.webapp.run()` and reads the bind
        # address/port + enabled result formats from SEARXNG_SETTINGS_PATH.
        searxng = pkgs.searxng;

        # Baked, non-secret settings. The critical bit is `search.formats`
        # including `json` (SearXNG's default is html-only, which would make the
        # MCP wrapper's `format=json` request 403). The limiter is off because
        # the only caller is the in-cluster MCP wrapper. `secret_key` is a
        # placeholder (internal-only instance); a real key, if ever wanted, is
        # injected at runtime as SEARXNG_SECRET (R6.1), not baked here.
        settings = pkgs.writeText "searxng-settings.yml" ''
          use_default_settings: true
          general:
            debug: false
            instance_name: "HelloDJ Search"
          server:
            bind_address: "0.0.0.0"
            port: 8080
            secret_key: "hellodj-internal-metasearch-not-a-secret"
            limiter: false
            image_proxy: false
          search:
            safe_search: 0
            formats:
              - html
              - json
          ui:
            static_use_hash: true
        '';

        # ------------------------------------------------------------------
        # OCI image. buildLayeredImage keeps the SearXNG closure and the
        # settings in separate layers. Only Nix-built closures land in the
        # image — no FROM ubuntu/debian (R5.1/5.2/5.3).
        # ------------------------------------------------------------------
        image = pkgs.dockerTools.buildLayeredImage {
          name = "hellodj-searxng";
          tag = "nix";

          contents = [ searxng pkgs.cacert ];

          extraCommands = ''
            mkdir -p etc/searxng
            cp ${settings} etc/searxng/settings.yml
          '';

          config = {
            ExposedPorts = { "8080/tcp" = { }; };
            Env = [
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              "SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml"
            ];
            Entrypoint = [ "${searxng}/bin/searxng-run" ];
          };
        };
      in
      {
        packages = {
          default = image;
          image = image;
        };

        checks = {
          image-builds = image;
        };
      });
}
