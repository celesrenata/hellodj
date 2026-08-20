'use strict';

/**
 * HelloDJ Video Activity Frontend
 *
 * Initializes the Discord Embedded App SDK, authenticates with the Activity
 * backend, fetches session status, and sets up hls.js for synchronized video
 * playback within the Discord Activity iframe.
 */
(async () => {
  const STATUS_POLL_INTERVAL_MS = 10_000;

  // DOM elements
  const videoEl = document.getElementById('player');
  const titleEl = document.getElementById('video-title');
  const durationEl = document.getElementById('video-duration');
  const errorOverlay = document.getElementById('error-overlay');
  const errorMessage = document.getElementById('error-message');

  // --- Utility ---

  /**
   * Format seconds into HH:MM:SS or MM:SS.
   */
  const formatDuration = (totalSeconds) => {
    const seconds = Math.floor(totalSeconds);
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  };

  /**
   * Show the error overlay with a message.
   */
  const showError = (msg) => {
    errorMessage.textContent = msg;
    errorOverlay.hidden = false;
  };

  // --- Discord Embedded App SDK Initialization ---

  let guildId = null;
  let channelId = null;
  let instanceId = null;

  try {
    // The CDN exposes the SDK on window.DiscordSDK (or window.Discord?.DiscordSDK)
    const DiscordSDKConstructor =
      window.DiscordSDK ?? window.Discord?.DiscordSDK;

    if (!DiscordSDKConstructor) {
      throw new Error('Discord Embedded App SDK not available');
    }

    // Client ID can be empty string — the Activity iframe provides context regardless
    const sdk = new DiscordSDKConstructor('');
    await sdk.ready();

    guildId = sdk.guildId;
    channelId = sdk.channelId;
    instanceId = sdk.instanceId;

    if (!guildId || !instanceId) {
      throw new Error('Missing guild or instance context from Discord SDK');
    }
  } catch (err) {
    showError(`SDK initialization failed: ${err.message}`);
    return;
  }

  // --- Backend Communication ---

  const authHeaders = {
    Authorization: `Bearer ${instanceId}`,
  };

  /**
   * Fetch the current session status from the Activity backend.
   * Returns the parsed JSON or null on failure.
   */
  const fetchStatus = async () => {
    try {
      const resp = await fetch(`/activity/status/${guildId}`, {
        headers: authHeaders,
      });
      if (!resp.ok) {
        if (resp.status === 401) {
          showError('Authentication failed. Please rejoin the Activity.');
          return null;
        }
        if (resp.status === 404) {
          showError('No active video session for this server.');
          return null;
        }
        return null;
      }
      return await resp.json();
    } catch {
      return null;
    }
  };

  // --- HLS Playback Setup ---

  let hls = null;
  let currentSessionId = null;

  /**
   * Initialize (or reinitialize) hls.js with the given playlist URL and seek offset.
   */
  const initHls = (playlistUrl, elapsedSeconds) => {
    // Tear down existing instance if any
    if (hls) {
      hls.destroy();
      hls = null;
    }

    if (!Hls.isSupported()) {
      // Fallback for browsers with native HLS (Safari)
      videoEl.src = playlistUrl;
      videoEl.currentTime = elapsedSeconds;
      videoEl.play().catch(() => {});
      return;
    }

    hls = new Hls({
      enableWorker: true,
      lowLatencyMode: false,
    });

    hls.loadSource(playlistUrl);
    hls.attachMedia(videoEl);

    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      // Seek to elapsed seconds for late-joiner sync
      videoEl.currentTime = elapsedSeconds;
      videoEl.play().catch(() => {});
    });

    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (!data.fatal) return;

      switch (data.type) {
        case Hls.ErrorTypes.MEDIA_ERROR:
          // Attempt recovery for media errors
          hls.recoverMediaError();
          break;
        case Hls.ErrorTypes.NETWORK_ERROR:
          showError('Network error — unable to load video stream.');
          break;
        default:
          showError('Playback error — unable to play video.');
          break;
      }
    });
  };

  // --- Initial Status Fetch & Playback Start ---

  const status = await fetchStatus();
  if (!status) return;

  if (status.video_title) {
    titleEl.textContent = status.video_title;
  }
  if (status.video_duration > 0) {
    durationEl.textContent = formatDuration(status.video_duration);
  }

  if (status.playlist_url) {
    // Use the full stream path for the guild's playlist
    const playlistUrl = `/activity/stream/${guildId}/playlist.m3u8`;
    const elapsed = status.elapsed_seconds ?? 0;
    currentSessionId = status.session_id;
    initHls(playlistUrl, elapsed);
  } else {
    showError('Waiting for video to begin transcoding...');
  }

  // --- Periodic Status Polling ---

  setInterval(async () => {
    const updated = await fetchStatus();
    if (!updated) return;

    // Update title/duration if video changed (queue advanced)
    if (updated.video_title && updated.video_title !== titleEl.textContent) {
      titleEl.textContent = updated.video_title;
    }
    if (updated.video_duration > 0) {
      durationEl.textContent = formatDuration(updated.video_duration);
    }

    // If session changed (new video started), reinitialize the player
    if (updated.session_id && updated.session_id !== currentSessionId) {
      currentSessionId = updated.session_id;
      if (updated.playlist_url) {
        const playlistUrl = `/activity/stream/${guildId}/playlist.m3u8`;
        const elapsed = updated.elapsed_seconds ?? 0;
        errorOverlay.hidden = true;
        initHls(playlistUrl, elapsed);
      }
    }
  }, STATUS_POLL_INTERVAL_MS);
})();
