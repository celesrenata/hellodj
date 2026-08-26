"""HelloDJ ``activity-backend`` component.

This component is the Discord Activity server. It serves the Activity HTTP
endpoints under ``/activity/`` and runs a WebSocket hub for real-time state
synchronization (video play/pause/seek, whiteboard strokes, visualizer control,
synced lyrics) over ALB/CloudFront. It preserves the existing Activity feature
set (video streaming control, whiteboard, audio visualizer, synced lyrics)
through the AWS re-platform (Requirement 6.2).

Rather than transcoding media itself, the activity-backend emits typed
transcode requests to the ``hls-transcode`` component and reads/serves the
resulting HLS from Amazon S3 via CloudFront (Requirements 18.2, 18.4). It is an
independently deployable, independently versioned component (Requirement 15.1):
its own Nix-built image, its own semantic version, and its own CI/CD path.

Public surface:
    * :class:`~activity_backend.config.ActivityConfig` — runtime settings.
    * :class:`~activity_backend.ws_hub.WebSocketHub` — real-time sync hub.
    * :class:`~activity_backend.whiteboard.StrokeRegistry` — whiteboard state.
    * :class:`~activity_backend.visualizer.VisualizerRegistry` — visualizer state.
    * :class:`~activity_backend.lyrics.LyricsStore` — synced-lyrics state.
    * :class:`~activity_backend.transcode_client.TranscodeClient` — hls-transcode client.
    * :class:`~activity_backend.hls.HlsCatalog` — S3/CloudFront HLS URL derivation.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Independent semantic version for the activity-backend component (R15.1).
__version__ = "0.1.0"
