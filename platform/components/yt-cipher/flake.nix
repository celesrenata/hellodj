{
  description = ''
    HelloDJ yt-cipher component — YouTube player signature/n-param deciphering
    HTTP service (upstream kikkia/yt-cipher, a Deno wrapper around yt-dlp/ejs)
    rebuilt as a Nix-built OCI image on a Nix-built Deno base. NO Ubuntu/Debian
    base layers (Requirements 5.1/5.2/5.3).

    The shared secret (the yt-cipher API_TOKEN) is NOT baked into the image; it
    is injected at runtime from AWS Secrets Manager as the API_TOKEN environment
    variable (Requirement 6.1). This is the SAME shared secret used by the
    rendered Lavalink config as `remoteCipher.password` — they must match.

    Listens on port 8001 (upstream default, Requirement 6.1).
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
        # Artifact provenance (see README.md).
        #
        # Upstream: kikkia/yt-cipher — an HTTP API wrapper around yt-dlp/ejs
        # that performs YouTube player-script signature and n-parameter
        # deciphering. It is a Deno application; `server.ts` is the entrypoint
        # and it `deno compile`s a standalone binary that bundles `worker.ts`.
        # The build fetches a pinned `yt-dlp/ejs` checkout and patches it via
        # `scripts/patch-ejs.ts` (see the upstream Dockerfile).
        #
        # Pinned: kikkia/yt-cipher rev 1e1fd8e… and yt-dlp/ejs rev cd4e87f…
        # (the EJS_COMMIT the upstream Dockerfile pins).
        #
        # Deno resolves remote `https://deno.land/std@…` imports over the
        # network at compile time. Nix's build sandbox has no network, so we
        # populate `DENO_DIR` in a fixed-output derivation (`denoCache`, network
        # allowed because its content hash is pinned) and reuse it in the
        # hermetic `deno compile` step (DENO_DIR set, no network needed).
        # ------------------------------------------------------------------

        deno = pkgs.deno;

        # `deno compile` needs the target-arch `denort` runtime binary, which it
        # otherwise downloads from dl.deno.land at build time (breaking the
        # hermetic sandbox). Fetch it as a fixed-output derivation pinned to the
        # nixpkgs `deno` version, for the AWS Graviton (aarch64) deployment
        # target, and hand it to the compile step via `DENORT_BIN`. Bump the
        # version + hash together whenever nixpkgs' deno bumps.
        denortVersion = deno.version; # 2.9.4 at pin time
        denortZip = pkgs.fetchurl {
          url =
            "https://dl.deno.land/release/v${denortVersion}/denort-aarch64-unknown-linux-gnu.zip";
          hash = "sha256-7vmXrDP1wLhtvc000UFCmZ87C3kRXADngYqpnvr6l/8=";
        };
        denortBin = pkgs.runCommand "denort-${denortVersion}-aarch64" {
          nativeBuildInputs = [ pkgs.unzip ];
        } ''
          mkdir -p "$out/bin"
          unzip ${denortZip} -d "$out/bin"
          chmod +x "$out/bin/denort"
        '';

        src = pkgs.fetchFromGitHub {
          owner = "kikkia";
          repo = "yt-cipher";
          rev = "1e1fd8e2f34ca90cf23545be72e46307bd3d3d2a";
          hash = "sha256-WiOlBI2Z21Mr81gZIOMGXu0c3C4h0LM4CafXh6FkEtQ=";
        };

        # The pinned yt-dlp/ejs checkout the patch step consumes (EJS_COMMIT in
        # the upstream Dockerfile).
        ejsSrc = pkgs.fetchFromGitHub {
          owner = "yt-dlp";
          repo = "ejs";
          rev = "cd4e87f52e87ab6d8b318fd3a817adda6fafa8dc";
          hash = "sha256-6S6O2wXfD38iMbtqMB3WA25cJJoWQRZ7gx9cpKQVYpU=";
        };

        # Assemble the exact build tree the upstream Dockerfile produces (minus
        # the patch step, which needs network and therefore runs inside the
        # fixed-output `denoCache` below): the app sources + the raw ejs
        # checkout at ./ejs.
        #
        # PIN THE ts_prometheus IMPORT (see below). Upstream `src/metrics.ts`
        # imports the UNVERSIONED `https://deno.land/x/ts_prometheus/mod.ts`.
        # deno.land serves that as a 302 redirect to the current "latest"
        # (`/x/ts_prometheus@v0.3.0/mod.ts`). `deno compile` does NOT follow
        # deno.land/x redirects when it bundles the module graph
        # (denoland/deno#13704): it embeds the redirect STUB (which exports
        # nothing) under the unversioned URL, so at RUNTIME the standalone
        # binary throws `The requested module '.../ts_prometheus/mod.ts' does
        # not provide an export named 'Counter'` — even though the versioned
        # module (and all its transitive files) are correctly cached. This does
        # NOT surface at build time because we `deno compile --no-check`.
        #
        # Fix: rewrite the single unversioned import to the pinned versioned URL
        # BEFORE both cache-priming and compile (both use preparedSrc), so the
        # bundled graph references the real module directly with no redirect to
        # follow. `ts_prometheus` is the ONLY unversioned deno.land/x import in
        # the source (verified); the std imports are already `@0.224.0`-pinned.
        # Bump the version here together with the pinned yt-cipher rev if
        # upstream changes the ts_prometheus dependency.
        preparedSrc = pkgs.runCommand "yt-cipher-prepared-src" { } ''
          mkdir -p "$out"
          cp -r ${src}/. "$out/"
          chmod -R u+w "$out"
          cp -r ${ejsSrc} "$out/ejs"
          chmod -R u+w "$out/ejs"
          rm -rf "$out/ejs/.git" "$out/ejs/node_modules" || true
          # Pin the unversioned ts_prometheus import to the resolved version so
          # `deno compile` bundles the real module, not the redirect stub.
          substituteInPlace "$out/src/metrics.ts" \
            --replace-fail \
              'https://deno.land/x/ts_prometheus/mod.ts' \
              'https://deno.land/x/ts_prometheus@v0.3.0/mod.ts'
        '';

        # Canonicalizes the primed DENO_DIR so the `denoCache` fixed-output
        # derivation's hash is stable run-to-run and builder-to-builder. See the
        # long comment on `denoCache.installPhase` for why this is necessary.
        denoCacheCanon = pkgs.writeText "yt-cipher-deno-cache-canon.py" ''
          import glob, json, os, sys
          root = sys.argv[1]
          mark = b"// denoCacheMetadata="
          # Deno appends `// denoCacheMetadata={json}` to every cached remote
          # module. That JSON's `headers` map is serialized in non-deterministic
          # order and carries volatile CDN fields (date/age/cf-ray/
          # x-deno-trace-id/server-timing) plus a per-fetch `time` epoch — so
          # copying it verbatim gives a different hash on EVERY build. Keep only
          # the stable `url` mapping (+ content-type) that `deno compile
          # --cached-only` needs; drop all volatile transport metadata.
          for f in glob.glob(os.path.join(root, "remote", "**"), recursive=True):
              if not os.path.isfile(f):
                  continue
              with open(f, "rb") as fh:
                  data = fh.read()
              idx = data.rfind(mark)
              if idx == -1:
                  continue
              meta = json.loads(data[idx + len(mark):].decode("utf-8"))
              hdrs = meta.get("headers", {}) or {}
              canon = {"headers": {}, "url": meta.get("url", "")}
              for k in ("content-type",):
                  if k in hdrs:
                      canon["headers"][k] = hdrs[k]
              trailer = mark + json.dumps(
                  canon, sort_keys=True, separators=(",", ":")
              ).encode("utf-8")
              with open(f, "wb") as fh:
                  fh.write(data[:idx] + trailer)
          # npm packument `registry.json` carries a volatile `_deno.etag` (weak
          # CDN etag, differs per edge) and unordered `versions`/`time` maps.
          for f in glob.glob(os.path.join(root, "**", "registry.json"), recursive=True):
              if not os.path.isfile(f):
                  continue
              d = json.load(open(f))
              if "_deno.etag" in d:
                  d["_deno.etag"] = ""
              json.dump(d, open(f, "w"), sort_keys=True, separators=(",", ":"))
        '';

        # Fixed-output derivation that primes DENO_DIR with the remote std/deps
        # `patch-ejs.ts`, `server.ts`, and `worker.ts` import, so the app build
        # below runs fully offline. Network is allowed here because the output
        # is content-addressed by `outputHash`. Update the hash whenever the
        # pinned rev changes the set of imported remote deps.
        denoCache = pkgs.stdenv.mkDerivation {
          name = "yt-cipher-deno-cache";
          src = preparedSrc;
          nativeBuildInputs = [ deno pkgs.python3 ];
          buildPhase = ''
            export DENO_DIR="$TMPDIR/deno-dir"
            export HOME="$TMPDIR"
            # The ejs sources must be PATCHED before caching: patch-ejs.ts
            # rewrites their imports to `npm:meriyah` / `npm:astring`, and those
            # npm deps (plus the std deps patch-ejs.ts itself uses) are exactly
            # what the compile step needs cached. Cache in dependency order:
            #   1. the patch script's own std deps, then run it;
            #   2. server.ts + worker.ts, which now pull the patched ejs's
            #      npm:meriyah / npm:astring.
            deno cache scripts/patch-ejs.ts
            deno run --allow-read --allow-write ./scripts/patch-ejs.ts
            deno cache server.ts worker.ts
          '';
          # Emit ONLY the content-stable parts of DENO_DIR. Deno's cache also
          # contains SQLite analysis DBs (`*_cache_v2`), a `gen/` V8 code cache,
          # and other mutable state whose bytes/ordering vary run-to-run, which
          # would make this fixed-output derivation's hash non-deterministic
          # (the first two builds produced two different hashes). The fetched
          # dependency payloads — `remote/` (HTTPS imports) and `npm/` + `deps/`
          # (npm/JSR packages) — ARE the parts `--cached-only` needs.
          #
          # HOWEVER, copying those trees verbatim STILL produced a different
          # hash on every build, on every builder. Two sources of run-to-run
          # variance had to be neutralized (see `denoCacheCanon`):
          #   1. Each cached remote module carries a trailing
          #      `// denoCacheMetadata={json}` line whose `headers` map is
          #      serialized in non-deterministic order and holds volatile CDN
          #      fields (date/age/cf-ray/x-deno-trace-id/server-timing) + a
          #      per-fetch `time` epoch.
          #   2. Each npm packument `registry.json` holds a weak `_deno.etag`
          #      (differs per CDN edge) and unordered `versions`/`time` maps.
          # The canonicalizer rewrites both deterministically, keeping only the
          # stable `url` mapping (+ content-type) the compile step consults.
          # `deno compile --cached-only` does NOT need the volatile transport
          # metadata (verified: the compile still succeeds and the trees are
          # then byte-identical across independent fetches), so this makes the
          # output depend only on the stable dependency payload bytes.
          installPhase = ''
            mkdir -p "$out"
            for d in remote npm deps registries; do
              if [ -e "$TMPDIR/deno-dir/$d" ]; then
                cp -r "$TMPDIR/deno-dir/$d" "$out/$d"
              fi
            done
            # Drop any leftover SQLite/WAL/journal files that may sit inside the
            # copied dep trees so the hash depends only on payload bytes.
            find "$out" -type f \
              \( -name '*.db' -o -name '*_cache_v2' -o -name '*-wal' \
                 -o -name '*-shm' -o -name '*-journal' \) -delete
            # Canonicalize the volatile Deno cache metadata (remote-module
            # `denoCacheMetadata` trailers + npm `registry.json`) so the
            # fixed-output hash is stable run-to-run and builder-to-builder.
            python3 ${denoCacheCanon} "$out"
          '';
          dontFixup = true;
          outputHashMode = "recursive";
          outputHashAlgo = "sha256";
          # Content hash of the CANONICALIZED primed DENO_DIR (std +
          # npm:meriyah/astring for the patched ejs). Thanks to `denoCacheCanon`
          # this is now deterministic run-to-run and builder-to-builder — the
          # value only changes when the pinned rev changes the imported remote
          # deps. Recompute then: build `.#denoCache`, read the "got:" hash,
          # paste here.
          outputHash = "sha256-Myb8pxQnuPIuNvigF8vRKE174UwATfZizgfP9OpzeSM=";
        };

        ytCipherApp = pkgs.stdenv.mkDerivation {
          pname = "yt-cipher";
          version = "0-unstable-2026-08-24";
          src = preparedSrc;
          # `deno compile` emits a DYNAMICALLY-LINKED binary whose ELF
          # interpreter is the build host's `/lib/ld-linux-aarch64.so.1` (the
          # GNU/glibc target). The minimal OCI image (contents = [ cacert ]) has
          # no such loader, so the kernel fails the exec with the misleading
          # "no such file or directory" (it's the MISSING INTERPRETER, not the
          # binary). We patchelf the interpreter + RPATH to the Nix glibc/gcc
          # libs (explicit patchelf, NOT autoPatchelfHook — the latter needs
          # pyelftools and is flaky under cross-arch emulation). The referenced
          # store paths enter the binary's closure, so `buildLayeredImage`
          # includes the loader + libs in the image automatically (no FROM
          # debian needed).
          nativeBuildInputs = [ deno pkgs.patchelf ];
          buildInputs = [ pkgs.stdenv.cc.cc.lib pkgs.glibc ];
          # patchelf is applied explicitly in postInstall below; skip the
          # default fixup's autoPatchelf/strip which could re-touch the binary.
          dontStrip = true;
          buildPhase = ''
            # Point Deno at the pre-primed cache (writable copy — deno needs to
            # write its analysis caches) and run fully offline.
            cp -r "${denoCache}" "$TMPDIR/deno-dir"
            chmod -R u+w "$TMPDIR/deno-dir"
            export DENO_DIR="$TMPDIR/deno-dir"
            export HOME="$TMPDIR"
            # Supply the pre-fetched aarch64 `denort` runtime so `deno compile`
            # does not reach dl.deno.land, and cross-target aarch64 (Graviton)
            # regardless of the builder's own arch.
            export DENORT_BIN="${denortBin}/bin/denort"
            # Apply the ejs patch offline (its deps are cached), then compile.
            deno run --cached-only --allow-read --allow-write ./scripts/patch-ejs.ts
            deno compile \
              --no-check \
              --cached-only \
              --target aarch64-unknown-linux-gnu \
              --output server \
              --allow-net --allow-read --allow-write --allow-env \
              --include worker.ts \
              server.ts
          '';
          installPhase = ''
            mkdir -p "$out/opt/yt-cipher"
            cp server "$out/opt/yt-cipher/server"
            cp -r docs "$out/opt/yt-cipher/docs" 2>/dev/null || true
            mkdir -p "$out/opt/yt-cipher/player_cache"

            # Point the deno-compiled binary's ELF interpreter at the Nix glibc
            # dynamic loader and set an RPATH covering glibc + libgcc/libstdc++,
            # so it runs in the minimal image (which otherwise has no
            # /lib/ld-linux-aarch64.so.1). The store paths pulled in here become
            # part of the binary's closure and are included in the OCI image.
            # aarch64 glibc ships the loader as `ld-linux-aarch64.so.1`; fail
            # loudly if it is not where we expect (rather than silently leaving
            # the un-runnable /lib interpreter).
            LOADER="${pkgs.glibc}/lib/ld-linux-aarch64.so.1"
            if [ ! -e "$LOADER" ]; then
              echo "ERROR: expected glibc loader not found at $LOADER" >&2
              ls -1 "${pkgs.glibc}/lib" | grep -i "ld-linux" >&2 || true
              exit 1
            fi
            patchelf \
              --set-interpreter "$LOADER" \
              --set-rpath "${pkgs.lib.makeLibraryPath [ pkgs.glibc pkgs.stdenv.cc.cc.lib ]}" \
              "$out/opt/yt-cipher/server"
            # Verify the interpreter actually changed off the default /lib path.
            if patchelf --print-interpreter "$out/opt/yt-cipher/server" | grep -q "^/lib/"; then
              echo "ERROR: interpreter still points at /lib after patchelf" >&2
              exit 1
            fi
          '';
        };

        # ------------------------------------------------------------------
        # OCI image. buildLayeredImage keeps the Deno runtime and the app in
        # separate layers so an app bump does not re-push the Deno layer.
        #
        # API_TOKEN is deliberately absent from the image: it is the shared
        # secret injected at runtime from AWS Secrets Manager (R6.1). Only the
        # non-secret defaults (PORT/HOST/OVERRIDE_PLAYER_VARIANT) are baked in.
        # ------------------------------------------------------------------
        image = pkgs.dockerTools.buildLayeredImage {
          name = "hellodj-yt-cipher";
          tag = "nix";

          # Only Nix-built closures land in the image. No FROM ubuntu/debian.
          # The `deno compile` output is a self-contained binary, so the Deno
          # runtime need not be a separate image layer; cacert stays for TLS.
          contents = [ pkgs.cacert ];

          extraCommands = ''
            mkdir -p opt/yt-cipher
            cp -r ${ytCipherApp}/opt/yt-cipher/. opt/yt-cipher/
          '';

          config = {
            WorkingDir = "/opt/yt-cipher";
            ExposedPorts = { "8001/tcp" = { }; };
            Env = [
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              # Non-secret runtime defaults. The shared secret API_TOKEN is
              # injected at runtime from Secrets Manager and is intentionally
              # NOT set here (R6.1).
              "PORT=8001"
              "HOST=0.0.0.0"
              # Upstream recommends the IAS variant for reliability; the legacy
              # deployment set OVERRIDE_PLAYER_VARIANT=IAS.
              "OVERRIDE_PLAYER_VARIANT=IAS"
            ];
            # The `deno compile` standalone binary is the entrypoint.
            Entrypoint = [ "/opt/yt-cipher/server" ];
          };
        };
      in
      {
        packages = {
          default = image;
          image = image;
          # Expose the app + cache derivations for inspection/testing.
          ytCipherApp = ytCipherApp;
          denoCache = denoCache;
        };

        # `nix flake check` evaluates these.
        checks = {
          image-builds = image;
        };
      });
}
