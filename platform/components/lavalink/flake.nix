{
  description = ''
    HelloDJ lavalink component — a THIN CONSUMER of the authoritative Lavalink
    fork flake (`github:hellodj/Lavalink/dev`). It re-exports that fork's
    Nix-built OCI `#image` (and the jar outputs) rather than building its own.

    Design §4 makes the `Lavalink` Fork_Flake the authoritative image builder
    and this component a thin consumer of its `#image`/jar outputs — keeping ONE
    image definition and avoiding drift (Requirements 4.5).

    The fork flake builds the image on a Nix-built Temurin 25 JRE
    (`temurin-jre-bin-25`, replacing the previous Temurin 21 base — R3.5) via
    `pkgs.dockerTools.buildLayeredImage` (R5.1), bundling the three REAL fork
    jars (custom `Lavalink.jar`, `lavasrc-plugin.jar`, `youtube-plugin-sabr.jar`)
    — NO Ubuntu/Debian/Alpine base and NO `PLACEHOLDER ARTIFACT` marker (R4.6).

    The previous `mkPlaceholderJar` derivations (which emitted `PLACEHOLDER
    ARTIFACT` markers for `Lavalink.jar`, `youtube-plugin-sabr.jar`, and
    `lavasrc-plugin-4.8.3.jar`) are REMOVED — the artifacts are now the real
    Fork_Flake outputs (Requirements 4.5, 4.6).

    Config (application.yml) is NOT baked into the image. It is rendered and
    injected at runtime by the `config-renderer` component into
    /opt/Lavalink/application.yml (Requirement 4.8) — the fork flake's image
    honours this contract (WorkingDir = /opt/Lavalink, no application.yml baked).
  '';

  # github: inputs ONLY (no path: inputs). The NixOS steering forbids path:
  # inputs because they tie the build to a machine's filesystem layout (R11.3).
  # Concrete revisions are captured in flake.lock at pin time (R11.1).
  #
  # `lavalink-fork` is the authoritative Lavalink fork on branch `dev` (R1.3).
  # For LOCAL build/verification the input is overridden on the CLI to the
  # working fork checkout (the committed input stays the github: form — R1.5):
  #   nix build .#image \
  #     --override-input lavalink-fork path:/…/Lavalink
  # (the fork itself may need its own sibling overrides for
  #  lavaplayer/lavasrc/youtube-source — see the fork flake header.)
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";

    # The authoritative Lavalink fork flake — builds the custom Lavalink.jar
    # and the OCI `#image` (Temurin 25 JRE base, real plugin jars). github:
    # form only (R1.5 / R11.3); build branch `dev` (R1.3).
    lavalink-fork = {
      url = "github:hellodj/Lavalink/dev";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, flake-utils, lavalink-fork }:
    # Default target is aarch64-linux (AWS Graviton). x86_64-linux is provided
    # only as a documented fallback per the dependency-compatibility gate (R4).
    flake-utils.lib.eachSystem [ "aarch64-linux" "x86_64-linux" ] (system:
      let
        # ------------------------------------------------------------------
        # Thin-consumer wiring (Design §4).
        #
        # This component builds NOTHING itself: it re-exports the authoritative
        # Lavalink fork's outputs. The fork's `#image` is the single Nix-built
        # OCI image (dockerTools.buildLayeredImage on a Temurin 25 JRE base,
        # R3.5 / R5.1) that bundles the three REAL fork jars:
        #
        #   /opt/Lavalink/Lavalink.jar                    <- fork #lavalinkJar
        #   /opt/Lavalink/plugins/lavasrc-plugin.jar      <- LavaSrc fork
        #   /opt/Lavalink/plugins/youtube-plugin-sabr.jar <- youtube-source fork
        #
        # Because these are the real Fork_Flake outputs, no `PLACEHOLDER
        # ARTIFACT` marker appears in any bundled jar (R4.6), and there is no
        # `temurin-jre-bin-21` / distro base anywhere (R3.5 / R5.1).
        #
        # Referencing the fork's `#image` here makes the whole fork build chain
        # (custom Lavalink.jar + the two plugin jars) a hard dependency, so if
        # any artifact cannot be resolved the build fails fast and names the
        # missing artifact (R4.7) — the fork flake enforces this.
        # ------------------------------------------------------------------
        forkPackages = lavalink-fork.packages.${system};

        image = forkPackages.image;
      in
      {
        packages = {
          default = image;
          image = image;

          # Re-export the authoritative fork's jar outputs for inspection/
          # testing. These are the REAL artifacts (no placeholders) — the same
          # ones the fork's `#image` bundles.
          lavalinkJar = forkPackages.lavalinkJar;
          youtubeSabrPlugin = forkPackages.youtubeSabrPlugin;
          lavasrcPlugin = forkPackages.lavasrcPlugin;
        };

        # `nix flake check` evaluates these. Building the fork's `#image` forces
        # the full fork build chain (jar + plugins) to succeed first.
        checks = {
          image-builds = image;
        };
      });
}
