{
  description = ''
    HelloDJ mcp-searxng-enhanced component — the OvertliDS/mcp-searxng-enhanced
    MCP server (category-aware web search, website scraping, date/time), rebuilt
    as a Nix-built OCI image from the upstream Python source. NO Ubuntu/Debian
    base layers (Requirements 5.1/5.2/5.3).

    Run in FastMCP HTTP mode (`python mcp_server.py --http`): it exposes the MCP
    protocol over HTTP at `/mcp` on MCP_HTTP_PORT. The voice-pipeline's Bedrock
    `web_search` tool calls this server's `search_web` tool over MCP-HTTP, and
    this server queries the in-cluster `searxng` metasearch component
    (SEARXNG_ENGINE_API_BASE_URL) for results (spec bedrock-voice-web-search).

    No secrets: the SearXNG endpoint and timezone are non-secret runtime env
    injected by the WorkloadsStack (SEARXNG_ENGINE_API_BASE_URL, DESIRED_TIMEZONE,
    MCP_HTTP_HOST/PORT). The server makes no AWS API calls, so it holds no IAM
    grant.

    Listens on port 8000 (HTTP mode default).
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
        # Upstream source: OvertliDS/mcp-searxng-enhanced (a single-file Python
        # MCP server, `mcp_server.py`, + requirements.txt). Pinned to the master
        # HEAD merge commit that added HTTP Server Mode (FastMCP). Bump the rev +
        # hash together.
        # ------------------------------------------------------------------
        src = pkgs.fetchFromGitHub {
          owner = "OvertliDS";
          repo = "mcp-searxng-enhanced";
          rev = "31291d968a5c4f4c8c7986877b0c1342daff9db7";
          hash = "sha256-a+pV5+VMiN9QNx2t24OeEvIRGt0rGGvyK8EisswGBQY=";
        };

        # The upstream requirements. All are packaged in nixpkgs. Version
        # CONSTRAINTS in requirements.txt are looser than nixpkgs' current
        # versions for a few (`trafilatura<2`, `cachetools<6`, `pydantic<3`),
        # but the app has no lockfile and imports these by public API; we use
        # the nixpkgs versions. `trafilatura`/`pymupdf` power the `get_website`
        # scraping tool; the voice-pipeline only uses `search_web`, so a scraping
        # dep drift does not affect the web-search path. See README for the risk.
        pythonEnv = pkgs.python3.withPackages (ps: with ps; [
          fastmcp
          httpx
          beautifulsoup4
          pydantic
          tzdata
          trafilatura
          python-dateutil
          cachetools
          filetype
          pymupdf
          pymupdf4llm
          lxml-html-clean
        ]);

        # ------------------------------------------------------------------
        # OCI image. buildLayeredImage keeps the Python env and the app source
        # in separate layers so an app bump does not re-push the interpreter
        # layer. Only Nix-built closures land in the image — no FROM
        # ubuntu/debian (R5.1/5.2/5.3).
        # ------------------------------------------------------------------
        image = pkgs.dockerTools.buildLayeredImage {
          name = "hellodj-mcp-searxng-enhanced";
          tag = "nix";

          contents = [ pythonEnv pkgs.cacert ];

          extraCommands = ''
            mkdir -p opt/mcp-searxng-enhanced config
            cp ${src}/mcp_server.py opt/mcp-searxng-enhanced/mcp_server.py
          '';

          config = {
            WorkingDir = "/opt/mcp-searxng-enhanced";
            ExposedPorts = { "8000/tcp" = { }; };
            Env = [
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              # Non-secret HTTP-mode bind defaults. SEARXNG_ENGINE_API_BASE_URL
              # + DESIRED_TIMEZONE are injected at runtime by the WorkloadsStack.
              "MCP_HTTP_HOST=0.0.0.0"
              "MCP_HTTP_PORT=8000"
              # Persistent config path the server writes on first start.
              "ODS_CONFIG_PATH=/config/ods_config.json"
            ];
            # Mirrors the upstream HTTP-mode invocation: `python mcp_server.py --http`.
            Entrypoint = [
              "${pythonEnv}/bin/python"
              "/opt/mcp-searxng-enhanced/mcp_server.py"
              "--http"
            ];
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
