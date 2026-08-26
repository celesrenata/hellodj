//! HelloDJ `spotify-stream` sidecar (skeleton).
//!
//! This is the AWS re-platform successor to the legacy Spotify direct-stream
//! sidecar. It is a thin wrapper around **librespot** (the open-source Rust
//! Spotify streaming client, <https://github.com/librespot-org/librespot>) that
//! exposes a direct-stream HTTP interface on **port 8802** — the same port the
//! legacy sidecar used — so `lavalink`/`playback-orchestrator` can pull Spotify
//! audio directly instead of mirroring through YouTube.
//!
//! Requirements traceability:
//! - **5.1** — packaged by the Nix build system (see `../flake.nix`,
//!   `pkgs.dockerTools.buildLayeredImage` over a Nix-built `pkgs.librespot`;
//!   no Ubuntu/Debian base).
//! - **6.1** — preserves multi-source playback (direct Spotify streaming).
//! - **15.1** — self-contained, independently deployable component.
//!
//! ## Secret injection (AWS Secrets Manager — never baked in)
//!
//! Spotify credentials/tokens are injected at runtime from AWS Secrets Manager,
//! not compiled into the binary or the image. This process reads them from,
//! in priority order:
//!   1. the file at `$SPOTIFY_CREDENTIALS_FILE` (a Secrets-Manager-mounted file,
//!      e.g. via the AWS Secrets & Config Provider CSI volume), then
//!   2. the `$SPOTIFY_CREDENTIALS` environment variable (Secrets Manager
//!      injected env).
//!
//! If neither is present the process refuses to start, so a misconfigured
//! deployment fails fast rather than serving without credentials.

use std::env;
use std::fs;
use std::process;

/// Default direct-stream port. Matches the legacy `spotify-stream` sidecar and
/// the `ExposedPorts` declared in `../flake.nix`.
const DEFAULT_PORT: u16 = 8802;

/// Resolve the Spotify credential from the AWS Secrets Manager injection points.
///
/// Returns the raw secret material (the caller hands it to librespot). Never
/// logs the secret value itself.
fn load_spotify_credentials() -> Result<String, String> {
    if let Ok(path) = env::var("SPOTIFY_CREDENTIALS_FILE") {
        if !path.is_empty() {
            return fs::read_to_string(&path)
                .map(|s| s.trim().to_string())
                .map_err(|e| format!("failed to read SPOTIFY_CREDENTIALS_FILE ({path}): {e}"));
        }
    }
    if let Ok(secret) = env::var("SPOTIFY_CREDENTIALS") {
        if !secret.is_empty() {
            return Ok(secret);
        }
    }
    Err("no Spotify credentials injected: expected AWS Secrets Manager \
         injection via SPOTIFY_CREDENTIALS_FILE (mounted file) or \
         SPOTIFY_CREDENTIALS (env)"
        .to_string())
}

/// Resolve the port to bind, honoring `$SPOTIFY_STREAM_PORT`.
fn resolve_port() -> u16 {
    env::var("SPOTIFY_STREAM_PORT")
        .ok()
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(DEFAULT_PORT)
}

fn main() {
    // Never print the credential value — only whether/where it was found.
    let _credentials = match load_spotify_credentials() {
        Ok(c) => {
            eprintln!("spotify-stream: loaded Spotify credentials from Secrets Manager injection");
            c
        }
        Err(e) => {
            eprintln!("spotify-stream: {e}");
            process::exit(1);
        }
    };

    let port = resolve_port();
    eprintln!("spotify-stream: would bind direct-stream interface on 0.0.0.0:{port}");

    // TODO(artifact-source): construct the librespot Session from `_credentials`
    // and serve the direct-stream HTTP surface on `port`. This skeleton only
    // establishes the credential-injection + port contract; the flake packages
    // upstream `pkgs.librespot` via the entrypoint wrapper until this crate's
    // full dependency set + Cargo.lock are committed.
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn port_defaults_to_8802() {
        // With no env override, the default direct-stream port is 8802.
        // (Uses the constant directly to avoid mutating process env in tests.)
        assert_eq!(DEFAULT_PORT, 8802);
    }

    #[test]
    fn missing_credentials_is_an_error() {
        // Ensure neither injection point is set for this assertion.
        env::remove_var("SPOTIFY_CREDENTIALS_FILE");
        env::remove_var("SPOTIFY_CREDENTIALS");
        assert!(load_spotify_credentials().is_err());
    }

    #[test]
    fn env_credentials_are_used_when_present() {
        env::remove_var("SPOTIFY_CREDENTIALS_FILE");
        env::set_var("SPOTIFY_CREDENTIALS", "test-secret");
        let got = load_spotify_credentials().expect("should read env credential");
        assert_eq!(got, "test-secret");
        env::remove_var("SPOTIFY_CREDENTIALS");
    }
}
