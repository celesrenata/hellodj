/**
 * HelloDJ Video Activity Frontend
 *
 * Discord Activity player with hls.js, hover/tap controls,
 * scrubber with drag-to-seek, WebSocket sync, and auto-hiding UI.
 */
import { DiscordSDK } from './discord-sdk.js';

(async () => {
  // --- Remote debug logger ---
  const _logQueue = [];
  const _rlog = (msg) => {
    _logQueue.push(msg);
    if (_logQueue.length >= 5 || !_rlog._timer) {
      clearTimeout(_rlog._timer);
      _rlog._timer = setTimeout(() => {
        const msgs = _logQueue.splice(0);
        if (msgs.length > 0) {
          fetch('clientlog', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({messages: msgs}) }).catch(() => {});
        }
      }, 100);
    }
  };
  _rlog._timer = null;
  _rlog('app.js loaded');
  _rlog('WhiteboardBundle exists: ' + (typeof window.WhiteboardBundle !== 'undefined'));
  if (window.WhiteboardBundle) {
    _rlog('WhiteboardBundle keys: ' + Object.keys(window.WhiteboardBundle).join(','));
  }

  // DOM
  const videoEl = document.getElementById('player');
  const controlsOverlay = document.getElementById('controls-overlay');
  const titleBar = document.getElementById('title-bar');
  const errorOverlay = document.getElementById('error-overlay');
  const errorMessage = document.getElementById('error-message');
  const countdownOverlay = document.getElementById('countdown-overlay');
  const countdownTitle = document.getElementById('countdown-title');
  const countdownNumber = document.getElementById('countdown-number');
  const dvdContainer = document.getElementById('dvd-container');
  const visualizerLoading = document.getElementById('visualizer-loading');
  const scrubber = document.getElementById('scrubber');
  const scrubberFill = document.getElementById('scrubber-fill');
  const scrubberBuffered = document.getElementById('scrubber-buffered');
  const scrubberThumb = document.getElementById('scrubber-thumb');
  const scrubberTooltip = document.getElementById('scrubber-tooltip');
  const timeDisplay = document.getElementById('time-display');
  const btnPlayPause = document.getElementById('btn-playpause');
  const btnBack = document.getElementById('btn-back');
  const btnForward = document.getElementById('btn-forward');
  const subtitleSelector = document.getElementById('subtitle-selector');
  const subtitleSelect = document.getElementById('subtitle-select');
  const subtitleForEveryone = document.getElementById('subtitle-for-everyone');
  const btnMute = document.getElementById('btn-mute');
  const volumeSlider = document.getElementById('volume-slider');

  // --- Utility ---
  const formatTitle = (title, uploader) => {
    if (uploader) return `${title} — Uploaded by ${uploader}`;
    return title || '';
  };

  const fmt = (sec) => {
    const s = Math.floor(Math.max(0, sec));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const ss = s % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h > 0 ? `${h}:${pad(m)}:${pad(ss)}` : `${m}:${pad(ss)}`;
  };

  const showError = (msg) => {
    errorMessage.textContent = msg;
    errorOverlay.classList.add('visible');
  };

  // --- Frontend State Machine ---
  // Modes: IDLE, COUNTDOWN, VIDEO_PLAYING, VISUALIZER_DVD, VISUALIZER_HLS
  let mode = 'IDLE';
  let _dvdScreensaver = null;
  let _vizPreMutedState = false; // Saved muted state before entering VISUALIZER_HLS

  // --- DVD Screensaver (Task 3.3) ---
  class DVDScreensaver {
    constructor(container, avatarUrl, trackInfo) {
      this.container = container;
      this.logo = document.createElement('img');
      this.logo.src = avatarUrl;
      this.logo.className = 'dvd-logo';
      this.x = Math.random() * (container.clientWidth - 128);
      this.y = Math.random() * (container.clientHeight - 128);
      this.dx = 2;  // pixels per frame (constant velocity)
      this.dy = 2;
      this.hue = 0;
      this.animFrame = null;
      this.trackInfo = trackInfo || null;
      this._trackLabel = null;
    }

    start() {
      this.container.appendChild(this.logo);
      if (this.trackInfo && (this.trackInfo.title || this.trackInfo.artist)) {
        this._createTrackLabel();
      }
      this._animate();
    }

    stop() {
      cancelAnimationFrame(this.animFrame);
      this.animFrame = null;
      this.logo.remove();
      if (this._trackLabel) {
        this._trackLabel.remove();
        this._trackLabel = null;
      }
    }

    updateTrack(trackInfo) {
      this.trackInfo = trackInfo;
      if (this._trackLabel) {
        this._trackLabel.textContent = this._formatTrack();
      } else if (trackInfo && (trackInfo.title || trackInfo.artist)) {
        this._createTrackLabel();
      }
    }

    _formatTrack() {
      if (!this.trackInfo) return '';
      const { title, artist } = this.trackInfo;
      if (title && artist) return `${title} — ${artist}`;
      return title || artist || '';
    }

    _createTrackLabel() {
      this._trackLabel = document.createElement('div');
      this._trackLabel.className = 'dvd-track-label';
      this._trackLabel.textContent = this._formatTrack();
      this.container.appendChild(this._trackLabel);
    }

    _animate() {
      const w = this.container.clientWidth - 128;
      const h = this.container.clientHeight - 128;

      this.x += this.dx;
      this.y += this.dy;

      let hitEdge = false;
      if (this.x <= 0 || this.x >= w) { this.dx = -this.dx; hitEdge = true; }
      if (this.y <= 0 || this.y >= h) { this.dy = -this.dy; hitEdge = true; }

      if (hitEdge) {
        this.hue = (this.hue + 60) % 360;
        this.logo.style.filter = `hue-rotate(${this.hue}deg)`;
      }

      this.logo.style.transform = `translate(${this.x}px, ${this.y}px)`;
      this.animFrame = requestAnimationFrame(() => this._animate());
    }
  }

  const setMode = (newMode) => {
    if (mode === newMode) return;
    const oldMode = mode;
    _rlog(`[Mode] ${oldMode} → ${newMode}`);
    mode = newMode;

    // Clean up previous mode
    if (oldMode === 'COUNTDOWN') {
      countdownOverlayCtrl.cancel();
      countdownOverlay.style.display = 'none';
    }
    if (oldMode === 'VISUALIZER_DVD') {
      dvdContainer.style.display = 'none';
      if (_dvdScreensaver) {
        _dvdScreensaver.stop();
        _dvdScreensaver = null;
      }
    }
    if (oldMode === 'VISUALIZER_HLS') {
      visualizerLoading.style.display = 'none';
      // Restore muted state and destroy viz HLS instance
      videoEl.muted = _vizPreMutedState;
      if (hls) { hls.destroy(); hls = null; }
      videoEl.removeAttribute('src');
      videoEl.load();
      // Restore controls visibility
      controlsOverlay.classList.remove('viz-live');
    }

    // Activate new mode
    switch (newMode) {
      case 'IDLE':
        videoEl.style.display = 'none';
        countdownOverlay.style.display = 'none';
        dvdContainer.style.display = 'none';
        visualizerLoading.style.display = 'none';
        break;
      case 'COUNTDOWN':
        videoEl.style.display = 'none';
        countdownOverlay.style.display = '';
        dvdContainer.style.display = 'none';
        visualizerLoading.style.display = 'none';
        break;
      case 'VIDEO_PLAYING':
        videoEl.style.display = '';
        countdownOverlay.style.display = 'none';
        dvdContainer.style.display = 'none';
        visualizerLoading.style.display = 'none';
        break;
      case 'VISUALIZER_DVD':
        videoEl.style.display = 'none';
        countdownOverlay.style.display = 'none';
        dvdContainer.style.display = '';
        visualizerLoading.style.display = 'none';
        break;
      case 'VISUALIZER_HLS':
        // Save muted state and mute (viz stream has no audio track)
        _vizPreMutedState = videoEl.muted;
        videoEl.muted = true;
        videoEl.style.display = '';
        countdownOverlay.style.display = 'none';
        dvdContainer.style.display = 'none';
        visualizerLoading.style.display = 'none';
        // Hide scrubber/time (live stream — no seeking) and show "LIVE" indicator
        controlsOverlay.classList.add('viz-live');
        break;
    }
  };

  /**
   * CountdownOverlay — manages the 3-2-1 countdown animation with CSS pop effects.
   * Replaces the old setTimeout-based placeholder with proper animation re-triggering.
   */
  class CountdownOverlay {
    constructor({ container, titleEl, numberEl, onComplete }) {
      this._container = container;
      this._titleEl = titleEl;
      this._numberEl = numberEl;
      this._onComplete = onComplete;
      this._timer = null;
      this._active = false;
    }

    /**
     * Start the countdown from the given number of seconds.
     * @param {number} seconds — may be a float for late-joiners (rounded to nearest int)
     * @param {string} videoTitle — displayed above the countdown number
     */
    start(seconds, videoTitle) {
      this.cancel(); // Clear any previous countdown

      // Round to nearest integer for late-joiners receiving fractional remaining time
      let remaining = Math.round(seconds);
      if (remaining < 1) remaining = 1;

      this._active = true;
      this._titleEl.textContent = videoTitle || '';
      this._showNumber(remaining);

      this._timer = setInterval(() => {
        remaining--;
        if (remaining > 0) {
          this._showNumber(remaining);
        } else {
          // Countdown complete — hide overlay and notify
          this._finish();
        }
      }, 1000);
    }

    /**
     * Cancel an in-progress countdown (used when mode changes away).
     */
    cancel() {
      if (this._timer) {
        clearInterval(this._timer);
        this._timer = null;
      }
      this._active = false;
    }

    /**
     * Display a number with CSS animation re-trigger.
     * Removes and re-applies the animation to get the countdown-pop effect on each tick.
     */
    _showNumber(num) {
      this._numberEl.textContent = num;
      // Force animation restart by resetting and triggering reflow
      this._numberEl.style.animation = 'none';
      this._numberEl.offsetHeight; // trigger reflow
      this._numberEl.style.animation = '';
    }

    /**
     * Complete the countdown — hide overlay and invoke the onComplete callback.
     */
    _finish() {
      this.cancel();
      this._container.style.display = 'none';
      if (this._onComplete) {
        this._onComplete();
      }
    }
  }

  // Instantiate the CountdownOverlay
  const countdownOverlayCtrl = new CountdownOverlay({
    container: countdownOverlay,
    titleEl: countdownTitle,
    numberEl: countdownNumber,
    onComplete: () => wsSend({ type: 'ready' }),
  });

  /** Start countdown (convenience wrapper used by WS message handler) */
  const startCountdown = (seconds, videoTitle) => {
    countdownOverlayCtrl.start(seconds, videoTitle);
  };

  // --- Lyrics Overlay (Task 1.12) ---
  /**
   * LyricsOverlay — Client-side synchronized lyrics renderer.
   *
   * Renders a 3-line display (previous, current, next) with smooth transitions.
   * Uses binary search to efficiently find the active line on each timeupdate.
   * Handles enable/disable from local toggle and broadcast override.
   */
  class LyricsOverlay {
    constructor(container) {
      this.container = container;
      this.el = null;           // .lyrics-overlay DOM element
      this.lines = [];          // [{time_ms, text, words}]
      this.syncType = null;     // 'lrc_synced' | 'lrc_word' | 'beat_estimated'
      this.currentIndex = -1;
      this.enabled = false;
      this.forcedState = null;  // null | true | false (broadcast override)
      this._unavailableTimer = null;
      this._build();
      // Restore saved preference from localStorage
      const savedPref = localStorage.getItem('hellodj_lyrics_enabled');
      if (savedPref === 'true') this.enabled = true;
    }

    _build() {
      this.el = document.createElement('div');
      this.el.className = 'lyrics-overlay';
      this.el.innerHTML = `
        <div class="lyrics-line prev"></div>
        <div class="lyrics-line current"></div>
        <div class="lyrics-line next"></div>
      `;
      this.container.appendChild(this.el);
      this.el.style.display = 'none';
    }

    enable() {
      this.enabled = true;
      if (this.lines.length > 0) {
        this.el.style.display = '';
      }
      localStorage.setItem('hellodj_lyrics_enabled', 'true');
    }

    disable() {
      this.enabled = false;
      this.el.style.display = 'none';
      localStorage.setItem('hellodj_lyrics_enabled', 'false');
    }

    forceEnable() {
      this.forcedState = true;
      this.el.style.display = this.lines.length > 0 ? '' : 'none';
    }

    forceDisable() {
      this.forcedState = false;
      this.el.style.display = 'none';
    }

    clearForce() {
      this.forcedState = null;
      // Revert to local preference
      if (this.enabled && this.lines.length > 0) {
        this.el.style.display = '';
      } else {
        this.el.style.display = 'none';
      }
    }

    get isVisible() {
      if (this.forcedState !== null) return this.forcedState;
      return this.enabled;
    }

    setLyricsData(payload) {
      this.lines = payload.lines || [];
      this.syncType = payload.sync_type;
      this.currentIndex = -1;
      if (this.isVisible && this.lines.length > 0) {
        this.el.style.display = '';
      }
    }

    clearLyrics() {
      this.lines = [];
      this.syncType = null;
      this.currentIndex = -1;
      this._clearDisplay();
    }

    updatePosition(currentTimeMs) {
      if (!this.isVisible || this.lines.length === 0) return;

      // Binary search for current line
      const idx = this._findLineIndex(currentTimeMs);
      if (idx === this.currentIndex) return;

      this.currentIndex = idx;
      this._renderLines(idx);

      // Word-level karaoke
      if (this.syncType === 'lrc_word' && this.lines[idx]?.words) {
        this._renderKaraoke(this.lines[idx].words, currentTimeMs);
      }
    }

    /**
     * Binary search returning the largest index where lines[i].time_ms <= timeMs.
     * Returns -1 if no line is at or before timeMs.
     */
    _findLineIndex(timeMs) {
      let lo = 0, hi = this.lines.length - 1, result = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (this.lines[mid].time_ms <= timeMs) {
          result = mid;
          lo = mid + 1;
        } else {
          hi = mid - 1;
        }
      }
      return result;
    }

    _renderLines(idx) {
      const prev = this.el.querySelector('.lyrics-line.prev');
      const curr = this.el.querySelector('.lyrics-line.current');
      const next = this.el.querySelector('.lyrics-line.next');

      prev.textContent = idx > 0 ? this.lines[idx - 1].text : '';
      curr.textContent = idx >= 0 ? this.lines[idx].text : '';
      next.textContent = idx < this.lines.length - 1 ? this.lines[idx + 1].text : '';

      // Trigger transition animation via requestAnimationFrame
      curr.classList.add('animate');
      requestAnimationFrame(() => {
        requestAnimationFrame(() => curr.classList.remove('animate'));
      });
    }

    _renderKaraoke(words, currentTimeMs) {
      const curr = this.el.querySelector('.lyrics-line.current');
      curr.innerHTML = words.map(w => {
        const active = currentTimeMs >= w.time_ms;
        return `<span class="lyrics-word ${active ? 'active' : ''}">${w.text}</span>`;
      }).join(' ');
    }

    _clearDisplay() {
      this.el.querySelectorAll('.lyrics-line').forEach(el => {
        el.textContent = '';
        el.innerHTML = '';
      });
      this.el.style.display = 'none';
    }

    showUnavailable() {
      if (!this.isVisible) return;
      const curr = this.el.querySelector('.lyrics-line.current');
      curr.textContent = 'No lyrics available';
      this.el.style.display = '';
      // Clear any existing dismissal timer
      if (this._unavailableTimer) {
        clearTimeout(this._unavailableTimer);
      }
      this._unavailableTimer = setTimeout(() => {
        if (this.lines.length === 0) {
          this.el.style.display = 'none';
          curr.textContent = '';
        }
        this._unavailableTimer = null;
      }, 3000);
    }
  }

  // Instantiate LyricsOverlay — use the video element's parent as container
  const lyricsOverlay = new LyricsOverlay(videoEl.parentElement);

  // --- Lyrics toggle button ---
  const btnLyrics = document.getElementById('btn-lyrics');
  if (btnLyrics) {
    // Restore active state from localStorage on load
    if (localStorage.getItem('hellodj_lyrics_enabled') === 'true') {
      btnLyrics.dataset.active = 'true';
    }
    btnLyrics.addEventListener('click', () => {
      if (lyricsOverlay.enabled) {
        lyricsOverlay.disable();
        btnLyrics.dataset.active = 'false';
      } else {
        lyricsOverlay.enable();
        btnLyrics.dataset.active = 'true';
      }
    });
  }

  // --- Controls visibility (auto-hide after 4s, show on hover/tap) ---
  let hideTimer = null;
  const showControls = () => {
    controlsOverlay.classList.add('visible');
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => controlsOverlay.classList.remove('visible'), 4000);
  };

  // Show controls initially for 20s
  controlsOverlay.classList.add('visible');
  hideTimer = setTimeout(() => controlsOverlay.classList.remove('visible'), 20000);

  document.addEventListener('mousemove', showControls);
  document.addEventListener('touchstart', showControls);
  document.addEventListener('click', showControls);

  // --- Discord SDK Init ---
  let guildId = null;
  let instanceId = null;

  try {
    const clientId = document.querySelector('meta[name="discord-client-id"]')?.content || '';
    const sdk = new DiscordSDK(clientId);
    await sdk.ready();
    guildId = sdk.guildId;
    instanceId = sdk.instanceId;
    if (!guildId || !instanceId) throw new Error('Missing guild/instance context');
  } catch (err) {
    showError(`SDK init failed: ${err.message}`);
    return;
  }

  // --- Backend API ---
  const authHeaders = { Authorization: `Bearer ${instanceId}` };

  const fetchStatus = async (suppressErrors = false) => {
    try {
      const resp = await fetch(`status/${guildId}`, { headers: authHeaders });
      if (!resp.ok) {
        if (!suppressErrors) {
          if (resp.status === 404) showError('No active video session.');
          else if (resp.status === 401) showError('Auth failed.');
          else showError(`Status error: HTTP ${resp.status}`);
        }
        return null;
      }
      return await resp.json();
    } catch (e) {
      if (!suppressErrors) showError(`Fetch error: ${e.message}`);
      return null;
    }
  };

  // --- WebSocket Sync ---
  let ws = null;
  let _remoteAction = false;  // Flag to prevent echo loops
  let _wsReconnectTimer = null;
  let _wsIntentionalClose = false;
  let _whiteboardSync = null; // Whiteboard WebSocket sync handler

  const wsSend = (msg) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  };

  const connectWebSocket = () => {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${proto}//${location.host}/ws/${guildId}?token=${encodeURIComponent(instanceId)}`;

    _wsIntentionalClose = false;
    ws = new WebSocket(wsUrl);

    ws.addEventListener('open', () => {
      console.log('[HelloDJ] WebSocket connected');
      clearTimeout(_wsReconnectTimer);
    });

    ws.addEventListener('message', (event) => {
      let data;
      try { data = JSON.parse(event.data); } catch { return; }
      handleWsMessage(data);
    });

    ws.addEventListener('close', () => {
      console.log('[HelloDJ] WebSocket closed');
      if (!_wsIntentionalClose) {
        // Reconnect after 3s
        _wsReconnectTimer = setTimeout(connectWebSocket, 3000);
      }
    });

    ws.addEventListener('error', () => {
      // Error will trigger close event, which handles reconnection
    });
  };

  const disconnectWebSocket = () => {
    _wsIntentionalClose = true;
    clearTimeout(_wsReconnectTimer);
    if (ws) {
      ws.close();
      ws = null;
    }
  };

  const handleWsMessage = (data) => {
    const { type } = data;

    // Forward whiteboard-related messages to whiteboard sync handler
    if (_whiteboardSync && _whiteboardSync.handleMessage(data)) {
      return;
    }

    switch (type) {
      case 'play':
        _remoteAction = true;
        {
          const wasMuted = videoEl.muted;
          videoEl.play().catch(() => {});
          videoEl.muted = wasMuted;
        }
        if (data.position != null) videoEl.currentTime = data.position;
        _remoteAction = false;
        break;

      case 'pause':
        _remoteAction = true;
        videoEl.pause();
        if (data.position != null) videoEl.currentTime = data.position;
        _remoteAction = false;
        break;

      case 'seek':
        _remoteAction = true;
        if (data.position != null) videoEl.currentTime = data.position;
        _remoteAction = false;
        break;

      case 'state':
        // Late-joiner sync: apply full state (preserve muted state)
        _remoteAction = true;
        if (mode !== 'VIDEO_PLAYING') setMode('VIDEO_PLAYING');
        {
          const wasMuted = videoEl.muted;
          // Only seek if position differs by more than 5s to avoid keyframe corruption
          if (data.position != null) {
            const drift = Math.abs(videoEl.currentTime - data.position);
            if (drift > 5) videoEl.currentTime = data.position;
          }
          if (data.playing) {
            videoEl.play().catch(() => {});
          } else {
            videoEl.pause();
          }
          // Restore muted state — don't let play() unmute
          videoEl.muted = wasMuted;
        }
        // Apply subtitle if set for everyone
        if (data.subtitle_lang) {
          activateSubtitleTrack(data.subtitle_lang);
        }
        // Apply audio track if set for everyone
        if (data.audio_lang) {
          switchAudioTrack(data.audio_lang);
        }
        _remoteAction = false;
        break;

      case 'countdown':
        // Received countdown signal — transition to COUNTDOWN mode
        setMode('COUNTDOWN');
        startCountdown(data.seconds || 3, data.video_title || '');
        break;

      case 'start':
        // Countdown complete on server side — begin HLS playback at position 0.
        // Skip if HLS is already initialized (this client triggered the ready
        // and already called initHls from its own countdown onComplete).
        if (!hls) {
          setMode('VIDEO_PLAYING');
          const playlistUrl = `stream/${guildId}/playlist.m3u8?token=${encodeURIComponent(instanceId)}`;
          initHls(playlistUrl, true);
        }
        break;

      case 'visualizer':
        // Visualizer activation/update message
        if (data.engine === 'dvd') {
          // Stop existing DVD screensaver if re-activated in same mode
          if (_dvdScreensaver) {
            _dvdScreensaver.stop();
            _dvdScreensaver = null;
          }
          setMode('VISUALIZER_DVD');
          const avatarUrl = data.config?.avatar_url || '';
          const trackInfo = data.config?.track || null;
          _dvdScreensaver = new DVDScreensaver(dvdContainer, avatarUrl, trackInfo);
          _dvdScreensaver.start();
          if (trackInfo && trackInfo.title) {
            titleBar.textContent = formatTitle(trackInfo.title, trackInfo.artist);
          }
        } else if (data.hls_ready) {
          // Server-rendered engine ready — switch to HLS playback
          setMode('VISUALIZER_HLS');
          visualizerLoading.style.display = 'none';
          if (data.playlist_url) {
            initHls(data.playlist_url, false, true);
          }
        } else if (data.state === 'starting') {
          // Engine is starting up, show loading state
          // Switch mode to VISUALIZER_HLS (shows videoEl) but overlay loading on top
          if (mode === 'VISUALIZER_HLS' && hls) {
            // Already in viz mode with active stream — destroy it for restart
            hls.destroy();
            hls = null;
          }
          setMode('VISUALIZER_HLS');
          visualizerLoading.style.display = '';
        }
        break;

      case 'session_end':
        // Session ended — clean up and go idle
        setMode('IDLE');
        if (hls) { hls.destroy(); hls = null; }
        videoEl.pause();
        videoEl.removeAttribute('src');
        videoEl.load();
        titleBar.textContent = '';
        break;

      case 'track_change':
        // Track metadata updated while in visualizer mode
        if (mode === 'VISUALIZER_DVD' || mode === 'VISUALIZER_HLS') {
          if (data.title) {
            titleBar.textContent = formatTitle(data.title, data.artist);
          }
          // Update DVDScreensaver track info if active
          if (mode === 'VISUALIZER_DVD' && _dvdScreensaver) {
            _dvdScreensaver.updateTrack({ title: data.title || '', artist: data.artist || '' });
          }
        }
        break;

      case 'subtitle_change':
        _remoteAction = true;
        if (data.lang) {
          activateSubtitleTrack(data.lang);
        } else {
          // Turning off subtitles
          activateSubtitleTrack('');
        }
        _remoteAction = false;
        break;

      case 'audio_change':
        _remoteAction = true;
        if (data.lang) {
          switchAudioTrack(data.lang);
        }
        _remoteAction = false;
        break;

      case 'lyrics_data':
        if (lyricsOverlay) lyricsOverlay.setLyricsData(data);
        break;

      case 'lyrics_unavailable':
        if (lyricsOverlay) lyricsOverlay.showUnavailable();
        break;

      case 'lyrics_overlay_enable':
        if (lyricsOverlay) lyricsOverlay.forceEnable();
        break;

      case 'lyrics_overlay_disable':
        if (lyricsOverlay) lyricsOverlay.forceDisable();
        break;
    }
  };

  /** Activate a subtitle track by language code — adds a <track> element for the VTT sidecar */
  const activateSubtitleTrack = (lang) => {
    // Remove existing tracks
    videoEl.querySelectorAll('track').forEach(t => t.remove());
    if (!lang) return;
    const track = document.createElement('track');
    track.kind = 'subtitles';
    track.src = `stream/${guildId}/subtitles/${lang}.vtt?token=${encodeURIComponent(instanceId)}`;
    track.srclang = lang;
    track.default = true;
    videoEl.appendChild(track);
    track.track.mode = 'showing';
    // Update subtitle select UI to match
    if (subtitleSelect) subtitleSelect.value = lang;
  };

  /** Populate subtitle selector from status API response */
  const populateSubtitles = (subtitles) => {
    if (!subtitles || subtitles.length === 0) {
      subtitleSelector.style.display = 'none';
      return;
    }
    // Clear existing options, keep "Off"
    subtitleSelect.innerHTML = '<option value="">Off</option>';
    subtitles.forEach((s) => {
      const opt = document.createElement('option');
      opt.value = s.lang;
      opt.textContent = s.label || s.lang;
      subtitleSelect.appendChild(opt);
    });
    subtitleSelector.style.display = 'flex';
  };

  subtitleSelect.addEventListener('change', () => {
    const lang = subtitleSelect.value;
    activateSubtitleTrack(lang);
    // If "for everyone" is checked, broadcast via WebSocket
    if (subtitleForEveryone.checked) {
      wsSend({ type: 'subtitle_change', lang: lang || null, for_everyone: true });
    }
  });

  /** Switch hls.js audio track by language code */
  const switchAudioTrack = (lang) => {
    if (!hls) return;
    const audioTracks = hls.audioTracks || [];
    for (let i = 0; i < audioTracks.length; i++) {
      if (audioTracks[i].lang === lang) {
        hls.audioTrack = i;
        break;
      }
    }
  };

  // --- HLS Setup ---
  let hls = null;

  const initHls = (playlistUrl, seekToStart = true, isLive = false) => {
    if (hls) { hls.destroy(); hls = null; }

    if (!Hls.isSupported()) {
      videoEl.src = playlistUrl;
      videoEl.play().catch(() => {});
      connectWebSocket();
      return;
    }

    const hlsConfig = {
      enableWorker: true,
      lowLatencyMode: isLive,
      maxBufferLength: isLive ? 10 : 30,
      maxMaxBufferLength: isLive ? 20 : 60,
      startLevel: 0,
      startPosition: isLive ? -1 : 0,
    };
    if (isLive) {
      hlsConfig.liveDurationInfinity = true;
      hlsConfig.liveBackBufferLength = 0;
    }

    hls = new Hls(hlsConfig);
    hls._seekToStart = seekToStart;
    hls._isLive = isLive;
    hls.loadSource(playlistUrl);
    hls.attachMedia(videoEl);

    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (hls._seekToStart) {
        videoEl.currentTime = 0;
      }
      videoEl.play().catch(() => {});
      // Hide visualizer loading overlay once manifest is parsed and playback starts
      if (hls._isLive && mode === 'VISUALIZER_HLS') {
        visualizerLoading.style.display = 'none';
      }
      // Connect WebSocket after HLS is initialized
      connectWebSocket();
    });

    hls.on(Hls.Events.ERROR, (_ev, data) => {
      if (data.details === 'bufferStalledError') {
        // Buffer stall — try to recover by nudging forward
        _rlog('HLS buffer stalled, attempting recovery');
        hls.recoverMediaError();
        return;
      }
      if (!data.fatal) return;
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR) { hls.recoverMediaError(); return; }
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
        // Network error during playback — attempt recovery
        _rlog('HLS network error, retrying: ' + data.details);
        hls.startLoad();
        return;
      }
      showError(`Stream error: ${data.details}`);
    });

    // Handle end of VOD — poll immediately for next video
    videoEl.addEventListener('ended', () => {
      _rlog('Video ended, checking for next session');
      videoEl.pause();
      // Immediately poll for session change instead of waiting 10s
      _checkForNextSession();
    });
  };

  // --- Player Controls ---
  // Volume slider
  let _savedVolume = 0.8; // remember volume before mute
  volumeSlider.addEventListener('input', () => {
    const val = volumeSlider.value / 100;
    videoEl.volume = val;
    if (val > 0 && videoEl.muted) {
      videoEl.muted = false;
    }
    _updateMuteIcon();
  });

  // Mute toggle on speaker icon
  btnMute.addEventListener('click', (e) => {
    e.stopPropagation(); // don't trigger global unmute
    if (videoEl.muted || videoEl.volume === 0) {
      videoEl.muted = false;
      videoEl.volume = _savedVolume || 0.8;
      volumeSlider.value = Math.round(videoEl.volume * 100);
    } else {
      _savedVolume = videoEl.volume;
      videoEl.muted = true;
    }
    _updateMuteIcon();
  });

  const _updateMuteIcon = () => {
    if (videoEl.muted || videoEl.volume === 0) {
      btnMute.textContent = '🔇';
    } else if (videoEl.volume < 0.5) {
      btnMute.textContent = '🔉';
    } else {
      btnMute.textContent = '🔊';
    }
  };

  // Initialize volume from slider default
  videoEl.volume = volumeSlider.value / 100;

  btnPlayPause.addEventListener('click', () => {
    if (videoEl.paused) {
      videoEl.play();
      btnPlayPause.textContent = '⏸️';
      if (!_remoteAction) wsSend({ type: 'play', position: videoEl.currentTime });
    } else {
      videoEl.pause();
      btnPlayPause.textContent = '▶️';
      if (!_remoteAction) wsSend({ type: 'pause', position: videoEl.currentTime });
    }
  });

  btnBack.addEventListener('click', () => {
    wsSend({ type: 'previous' });
  });

  btnForward.addEventListener('click', () => {
    wsSend({ type: 'skip' });
  });

  // --- Keyboard shortcuts ---
  document.addEventListener('keydown', (e) => {
    // Don't handle keys when typing in an input
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

    switch (e.key) {
      case ' ':
      case 'k':
        e.preventDefault();
        btnPlayPause.click();
        break;
      case 'ArrowLeft':
      case 'j':
        e.preventDefault();
        btnBack.click();
        break;
      case 'ArrowRight':
      case 'l':
        e.preventDefault();
        btnForward.click();
        break;
      case 'ArrowUp':
        e.preventDefault();
        videoEl.volume = Math.min(1, videoEl.volume + 0.1);
        volumeSlider.value = Math.round(videoEl.volume * 100);
        _updateMuteIcon();
        break;
      case 'ArrowDown':
        e.preventDefault();
        videoEl.volume = Math.max(0, videoEl.volume - 0.1);
        volumeSlider.value = Math.round(videoEl.volume * 100);
        _updateMuteIcon();
        break;
      case 'm':
        e.preventDefault();
        btnMute.click();
        break;
      case 'f':
        e.preventDefault();
        if (document.fullscreenElement) {
          document.exitFullscreen();
        } else {
          document.documentElement.requestFullscreen().catch(() => {});
        }
        break;
    }
  });

  // Time display update
  const updateTime = () => {
    const cur = videoEl.currentTime || 0;
    const dur = videoEl.duration || 0;
    timeDisplay.textContent = `${fmt(cur)} / ${fmt(dur)}`;

    // Update played position
    const pct = dur > 0 ? (cur / dur) * 100 : 0;
    scrubberFill.style.width = `${pct}%`;
    scrubberThumb.style.left = `${pct}%`;

    // Update buffered range
    if (videoEl.buffered.length > 0) {
      const bufferedEnd = videoEl.buffered.end(videoEl.buffered.length - 1);
      const bufPct = dur > 0 ? (bufferedEnd / dur) * 100 : 0;
      scrubberBuffered.style.width = `${bufPct}%`;
    }

    // Update lyrics overlay position
    if (lyricsOverlay) lyricsOverlay.updatePosition(cur * 1000);
  };
  videoEl.addEventListener('timeupdate', updateTime);
  videoEl.addEventListener('progress', updateTime);

  // Update play/pause icon on state change
  videoEl.addEventListener('play', () => { btnPlayPause.textContent = '⏸️'; });
  videoEl.addEventListener('pause', () => { btnPlayPause.textContent = '▶️'; });

  // Hide visualizer loading overlay once video actually starts playing
  videoEl.addEventListener('playing', () => {
    if (mode === 'VISUALIZER_HLS') {
      visualizerLoading.style.display = 'none';
    }
  });

  // --- Scrubber drag-to-seek ---
  let dragging = false;

  const getPercent = (e) => {
    const rect = scrubber.getBoundingClientRect();
    const x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
    return Math.max(0, Math.min(1, x / rect.width));
  };

  const updateTooltip = (pct, e) => {
    const dur = videoEl.duration || 0;
    scrubberTooltip.textContent = fmt(pct * dur);
    const rect = scrubber.getBoundingClientRect();
    const x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
    scrubberTooltip.style.left = `${x}px`;
  };

  scrubber.addEventListener('mousedown', (e) => { dragging = true; scrubber.classList.add('dragging'); seekTo(e); });
  scrubber.addEventListener('touchstart', (e) => { dragging = true; scrubber.classList.add('dragging'); seekTo(e); });

  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    seekTo(e);
  });
  document.addEventListener('touchmove', (e) => {
    if (!dragging) return;
    seekTo(e);
  });

  // On drag-end: send seek message with final position
  document.addEventListener('mouseup', () => {
    if (dragging) {
      dragging = false;
      scrubber.classList.remove('dragging');
      if (!_remoteAction) wsSend({ type: 'seek', position: videoEl.currentTime });
    }
  });
  document.addEventListener('touchend', () => {
    if (dragging) {
      dragging = false;
      scrubber.classList.remove('dragging');
      if (!_remoteAction) wsSend({ type: 'seek', position: videoEl.currentTime });
    }
  });

  // Hover tooltip on scrubber
  scrubber.addEventListener('mousemove', (e) => {
    if (dragging) return;
    const pct = getPercent(e);
    updateTooltip(pct, e);
  });

  const seekTo = (e) => {
    const pct = getPercent(e);
    const dur = videoEl.duration || 0;
    videoEl.currentTime = pct * dur;
    scrubberFill.style.width = `${pct * 100}%`;
    scrubberThumb.style.left = `${pct * 100}%`;
    updateTooltip(pct, e);
    // Note: WebSocket seek message is sent on drag-end only (debounced)
  };

  // --- Init ---
  const status = await fetchStatus();
  if (!status) return;

  if (status.video_title) titleBar.textContent = formatTitle(status.video_title, status.uploader);

  // Populate subtitle tracks from status API
  if (status.subtitles) {
    populateSubtitles(status.subtitles);
  }

  if (status.state === 'streaming' && status.playlist_url) {
    const playlistUrl = `stream/${guildId}/playlist.m3u8?token=${encodeURIComponent(instanceId)}`;

    // Late joiner: if stream already has elapsed time, skip countdown and go directly to HLS
    if (status.elapsed_seconds > 5) {
      setMode('VIDEO_PLAYING');
      initHls(playlistUrl, false);
    } else {
      // First viewer or stream just started — show CSS countdown, then switch to HLS
      setMode('COUNTDOWN');
      countdownOverlayCtrl.start(3, status.video_title || '');
      // Override onComplete to init HLS after countdown
      countdownOverlayCtrl._onComplete = () => {
        wsSend({ type: 'ready' });
        setMode('VIDEO_PLAYING');
        initHls(playlistUrl, true);
      };
    }
  } else if (status.state === 'buffering') {
    // Still transcoding — show countdown overlay as "preparing" state, poll until streaming
    setMode('COUNTDOWN');
    countdownTitle.textContent = status.video_title || 'Loading...';
    countdownNumber.textContent = '⏳';

    // Poll until stream is ready, then run 3..2..1 countdown
    const waitForStream = setInterval(async () => {
      const s = await fetchStatus(true);
      if (s && s.state === 'streaming' && s.playlist_url) {
        clearInterval(waitForStream);
        const playlistUrl = `stream/${guildId}/playlist.m3u8?token=${encodeURIComponent(instanceId)}`;
        countdownOverlayCtrl.start(3, s.video_title || '');
        countdownOverlayCtrl._onComplete = () => {
          wsSend({ type: 'ready' });
          setMode('VIDEO_PLAYING');
          initHls(playlistUrl, true);
        };
      }
    }, 2000);
  } else {
    showError('No active video session. Start one with /video play');
  }

  // --- Poll for changes ---
  let currentSessionId = status.session_id;

  const _checkForNextSession = async () => {
    const updated = await fetchStatus(true); // suppress errors — 404 is expected during transitions
    if (!updated) return; // Server unavailable or no session yet — retry next interval
    const formattedTitle = formatTitle(updated.video_title, updated.uploader);
    if (updated.video_title && formattedTitle !== titleBar.textContent) {
      titleBar.textContent = formattedTitle;
    }
    if (updated.session_id && updated.session_id !== currentSessionId) {
      currentSessionId = updated.session_id;
      // New session — reconnect WebSocket and reinit HLS
      disconnectWebSocket();
      if (updated.playlist_url) {
        errorOverlay.classList.remove('visible');
        setMode('VIDEO_PLAYING');
        const playlistUrl = `stream/${guildId}/playlist.m3u8?token=${encodeURIComponent(instanceId)}`;
        initHls(playlistUrl);
      }
    } else if (updated.state === 'streaming' && updated.session_id === currentSessionId) {
      // Still streaming same session — clear any stale error overlay
      errorOverlay.classList.remove('visible');
    } else if (updated.state === 'idle' || !updated.session_id) {
      // Session ended with no next video — show message but keep polling
      showError('Playback complete — queue is empty.');
    }
  };

  setInterval(_checkForNextSession, 10000);

  // --- Whiteboard Initialization ---
  _rlog('Whiteboard init starting');
  _rlog('WhiteboardBundle available: ' + !!window.WhiteboardBundle);
  const whiteboardCanvas = document.getElementById('whiteboard-canvas');
  const whiteboardHud = document.getElementById('whiteboard-hud');
  const btnWhiteboard = document.getElementById('btn-whiteboard');
  _rlog('Whiteboard DOM: canvas=' + !!whiteboardCanvas + ' hud=' + !!whiteboardHud + ' btn=' + !!btnWhiteboard);

  if (whiteboardCanvas && whiteboardHud && btnWhiteboard) {
    const {
      WhiteboardOverlay, ToolManager, PenTool, LineTool, ShapeTool,
      TextTool, EraserTool, StickerTool, StickerPicker, ColorPicker,
      initUndo, initReset, initTextBgToggle, getTextBg,
      initCanvasResize, ControlsPassthrough, initWhiteboardSync,
    } = window.WhiteboardBundle;
    // Use instanceId as a unique viewer identifier (it's per-user per-session)
    const localAuthorId = instanceId || crypto.randomUUID();

    // Create WhiteboardOverlay
    const overlay = new WhiteboardOverlay({
      canvas: whiteboardCanvas,
      hud: whiteboardHud,
      toggleButton: btnWhiteboard,
      localAuthorId,
    });

    // Initialize WebSocket sync for whiteboard
    _whiteboardSync = initWhiteboardSync(wsSend, overlay);

    // Initialize text background toggle
    initTextBgToggle(document.getElementById('text-bg-toggle'));

    // Initialize ColorPicker
    const swatches = document.querySelectorAll('.color-swatch');
    const customColorInput = document.getElementById('color-custom');
    const colorPicker = new ColorPicker({ swatches, customInput: customColorInput });

    // Initialize StickerPicker
    const stickerPickerContainer = document.getElementById('sticker-picker');
    const stickerPicker = new StickerPicker({
      container: stickerPickerContainer,
      onSelect: () => {}, // Will be overridden by StickerTool
    });

    // Initialize ToolManager
    const toolManager = new ToolManager(whiteboardCanvas);

    // Config helpers for tools
    const getCanvas = () => whiteboardCanvas;
    const getColor = () => colorPicker.getColor();

    // Register tools
    const penTool = new PenTool({ getCanvas, getColor, getWidth: () => overlay.currentWidth, getOpacity: () => overlay.currentOpacity });
    const lineTool = new LineTool({ getCanvas, getColor, getWidth: () => overlay.currentWidth, getOpacity: () => overlay.currentOpacity });
    const shapeTool = new ShapeTool();
    const textTool = new TextTool({
      getCanvasSize: () => ({ width: whiteboardCanvas.width, height: whiteboardCanvas.height }),
      getColor,
      getTextBg,
      getContainer: () => document.getElementById('app'),
      requestRedraw: () => overlay.redraw(),
      onStrokeFinalized: (stroke) => {
        stroke.author = localAuthorId;
        overlay.addStroke(stroke);
        _whiteboardSync.sendStrokeAdd(stroke);
        undoHandler.updateButtonState();
      },
    });
    const eraserTool = new EraserTool({
      getStrokes: () => Array.from(overlay.strokes.values()),
      getCanvas,
      onErase: (strokeId) => {
        overlay.removeStroke(strokeId);
        _whiteboardSync.sendStrokeRemove(strokeId);
        undoHandler.updateButtonState();
      },
    });
    const stickerTool = new StickerTool({
      getCanvas,
      getColor,
      stickerPicker,
    });

    toolManager.registerTool(penTool);
    toolManager.registerTool(lineTool);
    toolManager.registerTool(shapeTool);
    toolManager.registerTool(textTool);
    toolManager.registerTool(eraserTool);
    toolManager.registerTool(stickerTool);

    // Default tool: pen
    toolManager.selectTool('pen');

    // Wire HUD tool buttons
    document.querySelectorAll('.hud-tools .hud-btn[data-tool]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const toolName = btn.dataset.tool;
        toolManager.selectTool(toolName);
        // Update active state on buttons
        document.querySelectorAll('.hud-tools .hud-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        // Update shape tool color when selected
        if (toolName === 'shape') {
          shapeTool.setColor(colorPicker.getColor());
        }
      });
    });
    // Set initial active state on pen button
    document.querySelector('.hud-btn[data-tool="pen"]')?.classList.add('active');

    // Wire size and opacity sliders
    document.getElementById('stroke-size')?.addEventListener('input', (e) => {
      overlay.currentWidth = parseInt(e.target.value, 10);
    });
    document.getElementById('stroke-opacity')?.addEventListener('input', (e) => {
      overlay.currentOpacity = parseInt(e.target.value, 10) / 100;
    });

    // Wire canvas pointer events to active tool
    whiteboardCanvas.addEventListener('pointerdown', (e) => {
      if (overlay.mode !== 'active') return;
      const tool = toolManager.getActiveTool();
      if (tool) tool.onPointerDown(e);
    });

    whiteboardCanvas.addEventListener('pointermove', (e) => {
      if (overlay.mode !== 'active') return;
      const tool = toolManager.getActiveTool();
      if (tool) {
        tool.onPointerMove(e);
        // Re-render preview on top of existing strokes
        overlay.redraw();
        tool.renderPreview(overlay.ctx);
      }
    });

    whiteboardCanvas.addEventListener('pointerup', (e) => {
      if (overlay.mode !== 'active') return;
      const tool = toolManager.getActiveTool();
      if (tool) {
        const stroke = tool.onPointerUp(e);
        if (stroke) {
          stroke.author = localAuthorId;
          overlay.addStroke(stroke);
          _whiteboardSync.sendStrokeAdd(stroke);
          undoHandler.updateButtonState();
        }
        overlay.redraw(); // Clear preview
      }
    });

    whiteboardCanvas.addEventListener('pointerleave', (e) => {
      if (overlay.mode !== 'active') return;
      const tool = toolManager.getActiveTool();
      // PenTool and LineTool support onPointerLeave for finalizing at canvas boundary
      if (tool && typeof tool.onPointerLeave === 'function') {
        const stroke = tool.onPointerLeave(e);
        if (stroke) {
          stroke.author = localAuthorId;
          overlay.addStroke(stroke);
          _whiteboardSync.sendStrokeAdd(stroke);
          undoHandler.updateButtonState();
        }
        overlay.redraw();
      }
    });

    // Initialize undo button
    const undoHandler = initUndo(
      document.getElementById('btn-undo'),
      overlay,
      (strokeId) => _whiteboardSync.sendStrokeRemove(strokeId)
    );

    // Initialize reset button
    initReset(
      document.getElementById('btn-reset'),
      overlay,
      () => _whiteboardSync.sendWhiteboardReset()
    );

    // Initialize canvas resize handling
    initCanvasResize(whiteboardCanvas, overlay);

    // Initialize controls passthrough
    const controlsPassthrough = new ControlsPassthrough({
      canvas: whiteboardCanvas,
      controlsOverlay,
      bottomControls: document.querySelector('.bottom-controls'),
      showControls,
    });

    // Sync whiteboard active state with controls passthrough
    const origActivate = overlay.activate.bind(overlay);
    const origDeactivate = overlay.deactivate.bind(overlay);

    overlay.activate = () => {
      origActivate();
      controlsPassthrough.setWhiteboardActive(true);
    };
    overlay.deactivate = () => {
      origDeactivate();
      controlsPassthrough.setWhiteboardActive(false);
    };

    // Update shape tool color when color changes
    swatches.forEach(s => s.addEventListener('click', () => shapeTool.setColor(colorPicker.getColor())));
    if (customColorInput) customColorInput.addEventListener('input', () => shapeTool.setColor(colorPicker.getColor()));
  }
})();
