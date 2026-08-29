"""HelloDJ spotify-stream sidecar — per-user librespot session pool.

Multi-tenant Spotify data plane (multi-tenant-source-streaming section 2). The
single global librespot session is replaced by a bounded, per-user
:class:`~spotify_stream.session_pool.SpotifySessionPool` keyed by the guild's
owning Cognito ``sub``; each request resolves that user's stored Spotify
credential from the unified store (read-only, KMS Decrypt-only) and streams from
that user's session — no shared-account fallback (R3, R10.5).
"""
