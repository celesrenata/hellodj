/**
 * HelloDJ Video Activity Frontend
 *
 * Discord Activity player with hls.js, hover/tap controls,
 * scrubber with drag-to-seek, WebSocket sync, and auto-hiding UI.
 */
import { DiscordSDK } from './discord-sdk.js';
import { VisualizerMenu } from './menu_panel.js';

/**
 * ClockSync — Manages the monotonic clock sync handshake with the server.
 *
 * Computes the offset between local performance.now() and the server's
 * time.monotonic(), enabling accurate drift detection without wall-clock
 * dependency. Retries up to 3 times on timeout, then signals failure.
 */
class ClockSync {
  constructor(wsSend) {
    this._wsSend = wsSend;
    this.serverOffset = 0;
    this.rtt = 0;
    this.synced = false;
    this._pendingT1 = null;
    this._retryCount = 0;
    this._maxRetries = 3;
    this._timeout = null;
    this._onFailed = null; // Callback set by caller to close WS on exhausted retries
  }

  initiate() {
    this._pendingT1 = performance.now();
    this._wsSend({ type: 'clock_sync', client_t1: this._pendingT1 });
    this._timeout = setTimeout(() => this._onTimeout(), 2000);
  }

  handleReply(data) {
    if (data.client_t1 !== this._pendingT1) return false;
    clearTimeout(this._timeout);
    this._timeout = null;
    const now = performance.now();
    this.rtt = now - this._pendingT1;
    this.serverOffset = data.server_mono - (this._pendingT1 + this.rtt / 2);
    this.synced = true;
    this._retryCount = 0;
    return true;
  }

  serverNow() {
    return performance.now() + this.serverOffset;
  }

  get driftTolerance() {
    const rttSeconds = this.rtt / 1000;
    return Math.min(10.0, Math.max(3.0, rttSeconds * 2));
  }

  _onTimeout() {
    this._timeout = null;
    this._retryCount++;
    if (this._retryCount >= this._maxRetries) {
      // Exhausted retries — close WebSocket to trigger reconnection
      if (this._onFailed) this._onFailed();
      return;
    }
    // Retry after 500ms delay
    setTimeout(() => this.initiate(), 500);
  }

  destroy() {
    if (this._timeout) {
      clearTimeout(this._timeout);
      this._timeout = null;
    }
  }
}

// --- Drift Checker State (module scope) ---
let driftCheckInterval = null;
let isBuffering = false;

/**
 * Select the correct anchor_time field from a state message.
 * Prefers anchor_time_mono (monotonic) if present and > 0.
 */
function selectAnchorTime(state) {
  if (state.anchor_time_mono && state.anchor_time_mono > 0) {
    return state.anchor_time_mono;
  }
  return state.anchor_time;
}

/**
 * Compute expected playback position from server state + clock sync.
 * Uses monotonic anchor math: anchor_position + (serverNow - anchorTime).
 * Returns clamped to >= 0.0.
 */
function computeExpectedPosition(state, clockSync) {
  const anchorTime = selectAnchorTime(state);
  if (!state.playing) return Math.max(0.0, state.anchor_position);
  const serverNow = clockSync.serverNow();
  const expected = state.anchor_position + (serverNow - anchorTime);
  return Math.max(0.0, expected);
}

/**
 * Start the drift checker interval. Every 2 seconds, compares the video
 * element's currentTime to the expected server position and seeks if drift
 * exceeds the RTT-adaptive tolerance.
 *
 * @param {HTMLVideoElement} videoEl - The video element to monitor
 * @param {Function} getState - Returns the current playback state object
 * @param {ClockSync} clockSync - The clock sync instance for offset/tolerance
 */
function startDriftChecker(videoEl, getState, clockSync) {
  stopDriftChecker();
  driftCheckInterval = setInterval(() => {
    if (isBuffering) return;
    const state = getState();
    if (!state || !state.playing) return;
    if (!clockSync.synced) return;

    const expected = computeExpectedPosition(state, clockSync);
    const actual = videoEl.currentTime;
    const drift = Math.abs(actual - expected);

    if (drift > clockSync.driftTolerance) {
      console.log(`[DriftChecker] drift=${drift.toFixed(2)}s > tolerance=${clockSync.driftTolerance.toFixed(2)}s — seeking`);
      videoEl.currentTime = expected;
    }
  }, 2000);
}

/**
 * Stop the drift checker interval.
 */
function stopDriftChecker() {
  if (driftCheckInterval) {
    clearInterval(driftCheckInterval);
    driftCheckInterval = null;
  }
}

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
      this.hue = 0;
      this.animFrame = null;
      this.trackInfo = trackInfo || null;
      this._trackLabel = null;

      // Use proportional sizing: 15% of the smaller container dimension
      const dim = Math.min(container.clientWidth, container.clientHeight);
      this._size = Math.max(48, Math.round(dim * 0.15));
      this.logo.style.width = `${this._size}px`;
      this.logo.style.height = `${this._size}px`;

      // Initialize position within safe bounds
      const maxX = Math.max(0, container.clientWidth - this._size);
      const maxY = Math.max(0, container.clientHeight - this._size);
      this.x = 10 + Math.random() * Math.max(0, maxX - 20);
      this.y = 10 + Math.random() * Math.max(0, maxY - 20);
      // Speed proportional to viewport: ~0.3% of smaller dimension per frame
      const baseSpeed = Math.max(0.5, dim * 0.003);
      this.dx = baseSpeed;
      this.dy = baseSpeed;
      if (Math.random() > 0.5) this.dx = -this.dx;
      if (Math.random() > 0.5) this.dy = -this.dy;
    }

    start() {
      this.container.appendChild(this.logo);
      if (this.trackInfo && (this.trackInfo.title || this.trackInfo.artist)) {
        this._createTrackLabel();
      }
      // Listen for container resize to update size proportionally
      this._resizeObserver = new ResizeObserver(() => this._onResize());
      this._resizeObserver.observe(this.container);
      this._animate();
    }

    stop() {
      cancelAnimationFrame(this.animFrame);
      this.animFrame = null;
      if (this._resizeObserver) {
        this._resizeObserver.disconnect();
        this._resizeObserver = null;
      }
      this.logo.remove();
      if (this._trackLabel) {
        this._trackLabel.remove();
        this._trackLabel = null;
      }
    }

    _onResize() {
      const dim = Math.min(this.container.clientWidth, this.container.clientHeight);
      const newSize = Math.max(48, Math.round(dim * 0.15));
      if (newSize !== this._size) {
        this._size = newSize;
        this.logo.style.width = `${this._size}px`;
        this.logo.style.height = `${this._size}px`;
        // Clamp position to new bounds
        const maxX = Math.max(0, this.container.clientWidth - this._size);
        const maxY = Math.max(0, this.container.clientHeight - this._size);
        this.x = Math.max(0, Math.min(this.x, maxX));
        this.y = Math.max(0, Math.min(this.y, maxY));
        // Update speed proportionally
        const newSpeed = Math.max(0.5, dim * 0.003);
        this.dx = Math.sign(this.dx) * newSpeed;
        this.dy = Math.sign(this.dy) * newSpeed;
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
      const w = this.container.clientWidth;
      const h = this.container.clientHeight;
      if (w === 0 || h === 0) {
        this.animFrame = requestAnimationFrame(() => this._animate());
        return;
      }

      const maxX = w - this._size;
      const maxY = h - this._size;

      this.x += this.dx;
      this.y += this.dy;

      // Bounce off edges and clamp to bounds
      let hitEdge = false;
      if (this.x <= 0) { this.x = 0; this.dx = Math.abs(this.dx); hitEdge = true; }
      else if (this.x >= maxX) { this.x = maxX; this.dx = -Math.abs(this.dx); hitEdge = true; }
      if (this.y <= 0) { this.y = 0; this.dy = Math.abs(this.dy); hitEdge = true; }
      else if (this.y >= maxY) { this.y = maxY; this.dy = -Math.abs(this.dy); hitEdge = true; }

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
  let _searchPanel = null; // Search panel instance
  let _visualizerMenu = null; // Visualizer menu panel instance

  // --- Clock Sync State (module-scope within IIFE) ---
  let clockSync = null;          // Current ClockSync instance
  let _syncQueue = [];           // Messages queued while awaiting clock sync
  let _syncTimeout = null;       // 5s timeout for sync completion on reconnect
  let _hasConnectedBefore = false; // Track if this is a reconnection
  let _lastState = null;         // Last received state for drift checking

  const wsSend = (msg) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  };

  /**
   * Process queued messages after clock sync completes (or times out).
   * Replays state messages with the (possibly new) clock offset.
   */
  const _processSyncQueue = () => {
    if (_syncTimeout) {
      clearTimeout(_syncTimeout);
      _syncTimeout = null;
    }
    const queue = _syncQueue.splice(0);
    for (const msg of queue) {
      handleWsMessage(msg);
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
      console.log('[HelloDJ] WebSocket connected' + (_hasConnectedBefore ? ' (reconnect)' : ''));
      clearTimeout(_wsReconnectTimer);

      // Destroy old clock sync (preserve serverOffset/rtt for fallback)
      const prevOffset = clockSync ? clockSync.serverOffset : 0;
      const prevRtt = clockSync ? clockSync.rtt : 0;
      if (clockSync) clockSync.destroy();

      // Create new ClockSync instance
      clockSync = new ClockSync(wsSend);
      // Carry forward previous offset/rtt as fallback
      clockSync.serverOffset = prevOffset;
      clockSync.rtt = prevRtt;
      clockSync._onFailed = () => {
        // Exhausted retries — on reconnect, use stored offset and process queue
        if (_hasConnectedBefore && (prevOffset !== 0 || prevRtt !== 0)) {
          console.log('[HelloDJ] Clock sync failed on reconnect, using stored offset');
          clockSync.serverOffset = prevOffset;
          clockSync.rtt = prevRtt;
          _processSyncQueue();
        } else {
          // First connection with no previous offset — close WS to trigger reconnect
          if (ws) ws.close();
        }
      };

      // Reset sync queue
      _syncQueue = [];

      // Initiate clock sync handshake immediately
      clockSync.initiate();

      // Set up 5s sync timeout — if clock sync doesn't complete in 5s,
      // proceed with stored offset and process queued messages
      if (_syncTimeout) clearTimeout(_syncTimeout);
      _syncTimeout = setTimeout(() => {
        _syncTimeout = null;
        if (!clockSync.synced) {
          console.log('[HelloDJ] Clock sync timeout (5s), using stored offset');
          _processSyncQueue();
        }
      }, 5000);

      _hasConnectedBefore = true;

      // Re-enable menu on reconnect and re-sync if menu is open
      if (_visualizerMenu) {
        _visualizerMenu.setDisconnected(false);
        if (_visualizerMenu.isOpen) {
          wsSend({ type: 'menu_init' });
        }
      }
    });

    ws.addEventListener('message', (event) => {
      let data;
      try { data = JSON.parse(event.data); } catch { return; }

      // Handle clock_sync_reply directly
      if (data.type === 'clock_sync_reply') {
        if (clockSync && clockSync.handleReply(data)) {
          console.log(`[HelloDJ] Clock synced: offset=${clockSync.serverOffset.toFixed(2)}ms, rtt=${clockSync.rtt.toFixed(2)}ms`);
          // Sync complete — process queued messages with new offset
          _processSyncQueue();
          // Start drift checker if we have a PLAYING state
          if (_lastState && _lastState.playing && mode === 'VIDEO_PLAYING') {
            startDriftChecker(videoEl, () => _lastState, clockSync);
          }
        }
        return;
      }

      // While clock sync is pending on reconnect, queue state messages
      // but let time-critical messages (countdown, start) through immediately
      if (clockSync && !clockSync.synced && _syncTimeout) {
        const timeCritical = ['countdown', 'start', 'session_end', 'session_change',
                             'visualizer', 'track_change', 'lyrics_data', 'lyrics_unavailable',
                             'lyrics_overlay_enable', 'lyrics_overlay_disable',
                             'menu_init_response', 'presets_list_response', 'settings_schema_response',
                             'engine_switch_ack', 'preset_apply_ack', 'setting_change_ack',
                             'preset_save_ack', 'preset_delete_ack', 'visualizer_state',
                             'preset_added', 'preset_removed'];
        if (!timeCritical.includes(data.type)) {
          // Queue state/play/pause/seek messages until sync completes
          _syncQueue.push(data);
          return;
        }
      }

      handleWsMessage(data);
    });

    ws.addEventListener('close', () => {
      console.log('[HelloDJ] WebSocket closed');
      // Stop drift checker on disconnect (will restart after reconnect + sync)
      stopDriftChecker();
      // Notify menu of disconnection
      if (_visualizerMenu) _visualizerMenu.setDisconnected(true);
      if (!_wsIntentionalClose) {
        // Reconnect after 3s — preserve video element and HLS session
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
    if (_syncTimeout) {
      clearTimeout(_syncTimeout);
      _syncTimeout = null;
    }
    _syncQueue = [];
    stopDriftChecker();
    if (clockSync) {
      clockSync.destroy();
    }
    if (ws) {
      ws.close();
      ws = null;
    }
  };

  const handleWsMessage = (data) => {
    const { type } = data;
    if (type !== 'state' && type !== 'pong') {
      _rlog('[WS msg] type=' + type);
    }

    // Forward whiteboard-related messages to whiteboard sync handler
    if (_whiteboardSync && _whiteboardSync.handleMessage(data)) {
      return;
    }

    // Forward search-related messages to search panel
    if (_searchPanel && _searchPanel.handleMessage(data)) {
      return;
    }

    // Forward menu-related messages to visualizer menu panel
    if (_visualizerMenu && _visualizerMenu.handleMessage(data)) {
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
        // Update _lastState for drift checker
        if (data.anchor_position != null) {
          _lastState = {
            playing: true,
            anchor_position: data.anchor_position,
            anchor_time_mono: data.anchor_time_mono || 0,
            anchor_time: data.anchor_time || 0,
          };
        }
        // Use monotonic clock math if clock sync is available
        if (clockSync && clockSync.synced && data.anchor_position != null && data.anchor_time_mono > 0) {
          const expected = computeExpectedPosition(_lastState, clockSync);
          const drift = Math.abs(videoEl.currentTime - expected);
          if (drift > clockSync.driftTolerance) videoEl.currentTime = expected;
        } else if (data.anchor_position != null && data.anchor_time != null) {
          // Fallback to wall-clock math
          const expected = data.anchor_position + (Date.now() / 1000 - data.anchor_time);
          if (Math.abs(videoEl.currentTime - expected) > 3) videoEl.currentTime = expected;
        } else if (data.position != null) {
          videoEl.currentTime = data.position;
        }
        // Start drift checker on play
        if (clockSync && clockSync.synced && _lastState && mode === 'VIDEO_PLAYING') {
          startDriftChecker(videoEl, () => _lastState, clockSync);
        }
        _remoteAction = false;
        break;

      case 'pause':
        _remoteAction = true;
        videoEl.pause();
        // Update _lastState for drift checker
        if (data.anchor_position != null) {
          _lastState = {
            playing: false,
            anchor_position: data.anchor_position,
            anchor_time_mono: data.anchor_time_mono || 0,
            anchor_time: data.anchor_time || 0,
          };
        }
        // Use monotonic clock math if clock sync is available
        if (clockSync && clockSync.synced && data.anchor_position != null && data.anchor_time_mono > 0) {
          // When paused, expected is just anchor_position
          const expected = data.anchor_position;
          const drift = Math.abs(videoEl.currentTime - expected);
          if (drift > clockSync.driftTolerance) videoEl.currentTime = expected;
        } else if (data.anchor_position != null) {
          // Fallback: only seek if server position differs significantly
          if (Math.abs(videoEl.currentTime - data.anchor_position) > 3) {
            videoEl.currentTime = data.anchor_position;
          }
        } else if (data.position != null) {
          if (Math.abs(videoEl.currentTime - data.position) > 3) {
            videoEl.currentTime = data.position;
          }
        }
        // Stop drift checker on pause
        stopDriftChecker();
        _remoteAction = false;
        break;

      case 'seek':
        _remoteAction = true;
        if (data.position != null) videoEl.currentTime = data.position;
        _remoteAction = false;
        break;

      case 'audio_state':
        // Audio track metadata from the bot — update scrubber for audio playback
        // when no video is playing (Activity acts as universal remote)
        _remoteAction = true;
        if (data.duration != null && data.duration > 0) {
          window._audioDuration = data.duration;
          window._audioPlaying = data.playing !== false;
          window._audioPosition = data.position || 0;
          window._audioAnchorTime = Date.now() / 1000;
          window._audioTitle = data.title || '';
          window._audioAuthor = data.author || '';
          window._audioArtwork = data.artwork_url || '';
          // Update title bar with audio track info
          if (data.title) {
            titleBar.textContent = formatTitle(data.title, data.author);
          }
          // Update play/pause button state
          btnPlayPause.textContent = data.playing ? '⏸️' : '▶️';
        }
        _remoteAction = false;
        break;

      case 'state':
        // Late-joiner / reconnect sync: use monotonic anchor-based position computation.
        // Uses RTT-adaptive drift tolerance when clock sync is available.
        _remoteAction = true;
        // Only switch to VIDEO_PLAYING for actual video streams
        if (data.media_type === 'video') {
          if (mode !== 'VIDEO_PLAYING') setMode('VIDEO_PLAYING');
        } else if (data.media_type === 'audio') {
          // Audio state — update audio tracking variables for scrubber
          if (data.anchor_position != null) {
            window._audioPosition = data.anchor_position;
            window._audioAnchorTime = data.anchor_time || (Date.now() / 1000);
            window._audioPlaying = data.playing !== false;
          }
          _remoteAction = false;
          break;
        } else {
          // Legacy state message without media_type — assume video
          if (mode !== 'VIDEO_PLAYING') setMode('VIDEO_PLAYING');
        }
        {
          const wasMuted = videoEl.muted;

          // Update _lastState for drift checker
          _lastState = {
            playing: !!data.playing,
            anchor_position: data.anchor_position || 0,
            anchor_time_mono: data.anchor_time_mono || 0,
            anchor_time: data.anchor_time || 0,
          };

          let expectedPos = 0;
          let tolerance = 3; // default fixed tolerance

          // Use monotonic clock math if clock sync is available and message has anchor_time_mono
          if (clockSync && clockSync.synced && data.anchor_time_mono > 0) {
            expectedPos = computeExpectedPosition(_lastState, clockSync);
            tolerance = clockSync.driftTolerance;
          } else if (data.anchor_position != null && data.anchor_time != null) {
            // Fallback to wall-clock math
            if (data.playing) {
              expectedPos = data.anchor_position + (Date.now() / 1000 - data.anchor_time);
            } else {
              expectedPos = data.anchor_position;
            }
          } else if (data.position != null) {
            expectedPos = data.position;
          }

          const drift = Math.abs(videoEl.currentTime - expectedPos);
          if (drift > tolerance) videoEl.currentTime = expectedPos;

          if (data.playing) {
            videoEl.play().catch(() => {});
            // Start drift checker for playing state
            if (clockSync && clockSync.synced) {
              startDriftChecker(videoEl, () => _lastState, clockSync);
            }
          } else {
            videoEl.pause();
            // Stop drift checker for non-playing states
            stopDriftChecker();
          }
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
        // Received countdown signal — transition to COUNTDOWN mode.
        // Guard: if the countdown is already active (started by status-check
        // path), don't restart it — that causes "3...3...2...1" instead of "3...2...1".
        if (!countdownOverlayCtrl._active) {
          setMode('COUNTDOWN');
          startCountdown(data.seconds || 3, data.video_title || '');
        }
        break;

      case 'start':
        // Countdown complete on server side — begin HLS playback at position 0.
        // Skip if HLS is already initialized (this client triggered the ready
        // and already called initHls from its own countdown onComplete).
        // Store state for drift checker
        _lastState = {
          playing: true,
          anchor_position: data.position || 0,
          anchor_time_mono: data.anchor_time_mono || 0,
          anchor_time: data.timestamp || 0,
        };
        if (!hls) {
          setMode('VIDEO_PLAYING');
          const playlistUrl = `stream/${guildId}/playlist.m3u8?token=${encodeURIComponent(instanceId)}`;
          initHls(playlistUrl, true);
        }
        // Start drift checker after playback begins
        if (clockSync && clockSync.synced && _lastState) {
          startDriftChecker(videoEl, () => _lastState, clockSync);
        }
        break;

      case 'visualizer':
        // Visualizer activation/update message
        if (data.engine === 'dvd') {
          // If already in DVD mode, just update config (don't reset position)
          if (mode === 'VISUALIZER_DVD' && _dvdScreensaver) {
            // Update avatar if different
            const newAvatar = data.config?.avatar_url || '';
            if (newAvatar && _dvdScreensaver.logo.src !== newAvatar) {
              _dvdScreensaver.logo.src = newAvatar;
            }
            // Update track info if provided
            if (data.config?.track) {
              _dvdScreensaver.updateTrack(data.config.track);
              if (data.config.track.title) {
                titleBar.textContent = formatTitle(data.config.track.title, data.config.track.artist);
              }
            }
            break;
          }
          // First activation — create the screensaver
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
            // Strip /activity/ prefix (HLS.js uses relative URLs from page base)
            // and append auth token (same pattern as video HLS)
            let vizUrl = data.playlist_url;
            if (vizUrl.startsWith('/activity/')) {
              vizUrl = vizUrl.slice('/activity/'.length);
            } else if (vizUrl.startsWith('/')) {
              vizUrl = vizUrl.slice(1);
            }
            vizUrl += '?token=' + encodeURIComponent(instanceId);
            initHls(vizUrl, false, true);
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
        // Session ended — clean up video state, wait for visualizer message from server
        _rlog('[session_end] received — cleaning up video, currentSessionId=' + currentSessionId);
        // Stop drift checker and clear state
        stopDriftChecker();
        _lastState = null;
        if (hls) { hls.destroy(); hls = null; }
        videoEl.pause();
        videoEl.removeAttribute('src');
        videoEl.load();
        videoEl.playbackRate = 1.0;
        titleBar.textContent = '';
        _updateMuteIcon();
        // Go to IDLE temporarily — the server will send a 'visualizer' message
        // immediately after session_end if an engine is configured, which will
        // transition to the correct visualizer mode with proper settings.
        setMode('IDLE');
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

      case 'session_change':
        // Server notifies that skip/previous happened — immediately check for new session
        _rlog('[session_change] received via WS, calling _checkForNextSession');
        _checkForNextSession();
        break;

      case 'filter_sync':
        // Lavalink timescale changed (e.g. nightcore 1.25x, vaporwave 0.85x).
        // Adjust video playbackRate to keep video in sync with filtered audio.
        if (data.timescale != null) {
          _rlog('[filter_sync] timescale=' + data.timescale);
          videoEl.playbackRate = data.timescale;
        }
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

  const initHls = (playlistUrl, seekToStart = true, isLive = false, preloadOnly = false) => {
    if (hls) { hls.destroy(); hls = null; }

    if (!Hls.isSupported()) {
      videoEl.src = playlistUrl;
      if (!preloadOnly) videoEl.play().catch(() => {});
      connectWebSocket();
      return;
    }

    // Determine startPosition: use anchor_position from _lastState if > 0
    // (fallback from segment-zero timeout), otherwise default to 0
    let startPos = isLive ? -1 : 0;
    if (!isLive && _lastState && _lastState.anchor_position > 0 && seekToStart) {
      startPos = _lastState.anchor_position;
    }

    const hlsConfig = {
      enableWorker: true,
      lowLatencyMode: isLive,
      maxBufferLength: isLive ? 10 : 30,
      maxMaxBufferLength: isLive ? 20 : 60,
      startLevel: 0,
      startPosition: startPos,
      // Start rendering immediately — don't wait for large buffer before first frame
      maxBufferHole: 0.5,
      nudgeMaxRetry: 5,
      startFragPrefetch: true,
    };
    if (isLive) {
      hlsConfig.liveDurationInfinity = true;
      hlsConfig.liveBackBufferLength = 0;
    }

    hls = new Hls(hlsConfig);
    hls._seekToStart = seekToStart;
    hls._isLive = isLive;
    hls._preloadOnly = preloadOnly;
    hls.loadSource(playlistUrl);

    // Only attach to video element if not preloading — attaching triggers buffering
    // into the MediaSource which can auto-play. For preload, we attach later.
    if (!preloadOnly) {
      hls.attachMedia(videoEl);
    }

    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (hls._seekToStart) {
        videoEl.currentTime = 0;
      }
      // Only auto-play if not in countdown (preloading keeps video paused)
      if (mode !== 'COUNTDOWN') {
        videoEl.style.display = '';
        videoEl.muted = false;
        videoEl.play().catch((err) => {
          _rlog('play() rejected: ' + err.message);
        });
      }
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
    if (window._audioDuration > 0 && mode !== 'VIDEO_PLAYING') {
      // Audio mode — toggle via WS (no local video element involved)
      if (window._audioPlaying) {
        window._audioPlaying = false;
        // Freeze position at current computed value
        window._audioPosition = window._audioPosition + (Date.now() / 1000 - window._audioAnchorTime);
        window._audioAnchorTime = Date.now() / 1000;
        btnPlayPause.textContent = '▶️';
        wsSend({ type: 'pause', position: window._audioPosition });
      } else {
        window._audioPlaying = true;
        window._audioAnchorTime = Date.now() / 1000;
        btnPlayPause.textContent = '⏸️';
        wsSend({ type: 'play', position: window._audioPosition });
      }
    } else if (videoEl.paused) {
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

    // When menu is open, let the menu handle its own keys (Escape, Tab, arrows)
    // and suppress app-level shortcuts that could conflict with focus trap
    if (_visualizerMenu && _visualizerMenu.isOpen) {
      // Escape closes menu and returns focus to toggle button
      if (e.key === 'Escape') {
        e.preventDefault();
        _visualizerMenu.close();
        const menuToggle = document.getElementById('menu-toggle');
        if (menuToggle) menuToggle.focus();
      }
      return;
    }

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
  // knownDuration: authoritative video duration from server metadata.
  // videoEl.duration grows as HLS transcode progresses — use server value when available.
  let knownDuration = 0;

  const getDuration = () => knownDuration > 0 ? knownDuration : (videoEl.duration || 0);

  const updateTime = () => {
    let cur, dur;

    // If we're in audio mode (no video playing, but audio state received from bot)
    if (window._audioDuration > 0 && mode !== 'VIDEO_PLAYING') {
      dur = window._audioDuration;
      if (window._audioPlaying && window._audioAnchorTime) {
        cur = window._audioPosition + (Date.now() / 1000 - window._audioAnchorTime);
      } else {
        cur = window._audioPosition || 0;
      }
      // Clamp to duration
      if (cur > dur) cur = dur;
      if (cur < 0) cur = 0;
    } else {
      cur = videoEl.currentTime || 0;
      dur = getDuration();
    }

    timeDisplay.textContent = `${fmt(cur)} / ${fmt(dur)}`;

    // Update played position
    const pct = dur > 0 ? (cur / dur) * 100 : 0;
    scrubberFill.style.width = `${pct}%`;
    scrubberThumb.style.left = `${pct}%`;

    // Update buffered range (video only)
    if (mode === 'VIDEO_PLAYING' && videoEl.buffered.length > 0) {
      const bufferedEnd = videoEl.buffered.end(videoEl.buffered.length - 1);
      const bufPct = dur > 0 ? (bufferedEnd / dur) * 100 : 0;
      scrubberBuffered.style.width = `${bufPct}%`;
    }

    // Update lyrics overlay position
    if (lyricsOverlay) lyricsOverlay.updatePosition(cur * 1000);
  };
  videoEl.addEventListener('timeupdate', updateTime);
  videoEl.addEventListener('progress', updateTime);
  // Tick for audio mode (no video timeupdate events to drive the loop)
  setInterval(() => { if (window._audioDuration > 0 && mode !== 'VIDEO_PLAYING') updateTime(); }, 500);

  // Update play/pause icon on state change
  videoEl.addEventListener('play', () => { btnPlayPause.textContent = '⏸️'; });
  videoEl.addEventListener('pause', () => { btnPlayPause.textContent = '▶️'; });

  // Hide visualizer loading overlay once video actually starts playing
  videoEl.addEventListener('playing', () => {
    if (mode === 'VISUALIZER_HLS') {
      visualizerLoading.style.display = 'none';
    }
  });

  // Buffering detection for drift checker — skip checks while rebuffering
  videoEl.addEventListener('waiting', () => { isBuffering = true; });
  videoEl.addEventListener('canplay', () => { isBuffering = false; });

  // --- Scrubber drag-to-seek ---
  let dragging = false;

  const getPercent = (e) => {
    const rect = scrubber.getBoundingClientRect();
    const x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
    return Math.max(0, Math.min(1, x / rect.width));
  };

  const updateTooltip = (pct, e) => {
    const dur = getDuration();
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
      // In audio mode, compute position from audio duration; in video mode use videoEl
      const seekPos = (window._audioDuration > 0 && mode !== 'VIDEO_PLAYING')
        ? window._audioPosition
        : videoEl.currentTime;
      if (!_remoteAction) wsSend({ type: 'seek', position: seekPos });
    }
  });
  document.addEventListener('touchend', () => {
    if (dragging) {
      dragging = false;
      scrubber.classList.remove('dragging');
      const seekPos = (window._audioDuration > 0 && mode !== 'VIDEO_PLAYING')
        ? window._audioPosition
        : videoEl.currentTime;
      if (!_remoteAction) wsSend({ type: 'seek', position: seekPos });
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
    const dur = (window._audioDuration > 0 && mode !== 'VIDEO_PLAYING')
      ? window._audioDuration
      : getDuration();
    const newPos = pct * dur;
    if (window._audioDuration > 0 && mode !== 'VIDEO_PLAYING') {
      // Audio mode — update the local audio position tracker
      window._audioPosition = newPos;
      window._audioAnchorTime = Date.now() / 1000;
    } else {
      videoEl.currentTime = newPos;
    }
    scrubberFill.style.width = `${pct * 100}%`;
    scrubberThumb.style.left = `${pct * 100}%`;
    updateTooltip(pct, e);
    // Note: WebSocket seek message is sent on drag-end only (debounced)
  };

  // --- Init ---
  const status = await fetchStatus();
  if (!status) return;

  // Set the authoritative duration from server metadata (seconds)
  if (status.video_duration && status.video_duration > 0) {
    knownDuration = status.video_duration;
  }

  if (status.video_title) titleBar.textContent = formatTitle(status.video_title, status.uploader);

  // Populate subtitle tracks from status API
  if (status.subtitles) {
    populateSubtitles(status.subtitles);
  }

  if (status.state === 'streaming' && status.playlist_url) {
    const playlistUrl = `stream/${guildId}/playlist.m3u8?token=${encodeURIComponent(instanceId)}`;

    if (status.playback_started) {
      // Late joiner: playback already started by a previous viewer.
      // Jump directly to HLS at current position (no countdown).
      setMode('VIDEO_PLAYING');
      initHls(playlistUrl, false);
    } else {
      // First viewer: show countdown, preload HLS in background, then play from 0.
      setMode('COUNTDOWN');
      initHls(playlistUrl, true, false, true);
      countdownOverlayCtrl.start(3, status.video_title || '');
      countdownOverlayCtrl._onComplete = () => {
        wsSend({ type: 'ready' });
        setMode('VIDEO_PLAYING');
        if (hls) {
          hls.attachMedia(videoEl);
          hls.once(Hls.Events.FRAG_BUFFERED, () => {
            videoEl.currentTime = 0;
            videoEl.muted = false;
            videoEl.play().catch((err) => { _rlog('play() after countdown: ' + err.message); });
          });
        }
        connectWebSocket();
      };
    }
  } else if (status.state === 'buffering') {
    // Still transcoding — show countdown overlay as "preparing" state, poll until streaming
    setMode('COUNTDOWN');
    countdownTitle.textContent = status.video_title || 'Loading...';
    countdownNumber.textContent = '⏳';

    // Poll until stream is ready, then preload HLS during countdown
    const waitForStream = setInterval(async () => {
      const s = await fetchStatus(true);
      if (s && s.state === 'streaming' && s.playlist_url) {
        clearInterval(waitForStream);
        const playlistUrl = `stream/${guildId}/playlist.m3u8?token=${encodeURIComponent(instanceId)}`;
        // Start HLS loading in preload mode during countdown
        initHls(playlistUrl, true, false, true);
        countdownOverlayCtrl.start(3, s.video_title || '');
        countdownOverlayCtrl._onComplete = () => {
          wsSend({ type: 'ready' });
          setMode('VIDEO_PLAYING');
          if (hls) {
            hls.attachMedia(videoEl);
            hls.once(Hls.Events.FRAG_BUFFERED, () => {
              videoEl.currentTime = 0;
              videoEl.muted = false;
              videoEl.play().catch((err) => { _rlog('play() after countdown: ' + err.message); });
            });
          }
          connectWebSocket();
        };
      }
    }, 2000);
  } else if (status.state === 'visualizer') {
    // Visualizer-only mode — no video session, but a visualizer engine is configured.
    // Connect WebSocket and let the VisualizerManager send the engine activation message.
    connectWebSocket();
    // For client-side engines (dvd) or any engine without server rendering,
    // start the DVD screensaver as the default visualization.
    // Server-rendered engines (audiovis, projectm, etc.) will override via WS message.
    setMode('VISUALIZER_DVD');
    const avatarUrl = status.bot_avatar_url || '';
    _dvdScreensaver = new DVDScreensaver(dvdContainer, avatarUrl, null);
    _dvdScreensaver.start();
  } else {
    // No active session — show DVD screensaver as idle state instead of blank screen
    connectWebSocket();
    setMode('VISUALIZER_DVD');
    const avatarUrl = status.bot_avatar_url || '';
    _dvdScreensaver = new DVDScreensaver(dvdContainer, avatarUrl, null);
    _dvdScreensaver.start();
  }

  // --- Poll for changes ---
  let currentSessionId = status.session_id;

  const _checkForNextSession = async () => {
    const updated = await fetchStatus(true); // suppress errors — 404 is expected during transitions
    if (!updated) {
      _rlog('[_checkForNextSession] fetchStatus returned null (404/error)');
      return;
    }
    _rlog('[_checkForNextSession] status: state=' + updated.state + ' session_id=' + (updated.session_id || 'null') + ' currentSessionId=' + currentSessionId + ' playlist_url=' + (updated.playlist_url ? 'yes' : 'no') + ' title=' + (updated.video_title || ''));
    const formattedTitle = formatTitle(updated.video_title, updated.uploader);
    if (updated.video_title && formattedTitle !== titleBar.textContent) {
      titleBar.textContent = formattedTitle;
    }
    if (updated.session_id && updated.session_id !== currentSessionId) {
      if (updated.playlist_url && updated.state === 'streaming') {
        _rlog('[_checkForNextSession] NEW SESSION ready: ' + updated.session_id + ' (was ' + currentSessionId + ')');
        currentSessionId = updated.session_id;
        // Update known duration for the new video
        if (updated.video_duration && updated.video_duration > 0) {
          knownDuration = updated.video_duration;
        }
        errorOverlay.classList.remove('visible');
        setMode('VIDEO_PLAYING');
        const playlistUrl = `stream/${guildId}/playlist.m3u8?token=${encodeURIComponent(instanceId)}`;
        initHls(playlistUrl);
      } else {
        _rlog('[_checkForNextSession] new session ' + updated.session_id + ' not ready yet (state=' + updated.state + ', playlist=' + (updated.playlist_url ? 'yes' : 'no') + ') — will retry');
        // Don't update currentSessionId — wait until it's streaming with playlist
      }
    } else if (updated.state === 'streaming' && updated.session_id === currentSessionId) {
      // Still streaming same session — clear any stale error overlay
      errorOverlay.classList.remove('visible');
    } else if (updated.state === 'visualizer') {
      // Visualizer-only mode — keep current visualizer running, don't interfere
      errorOverlay.classList.remove('visible');
    } else if (updated.state === 'idle' || !updated.session_id) {
      // Session ended with no next video — show DVD screensaver as idle
      if (mode !== 'VISUALIZER_DVD' && mode !== 'VISUALIZER_HLS') {
        _rlog('[_checkForNextSession] idle/no session — showing DVD screensaver');
        errorOverlay.classList.remove('visible');
        setMode('VISUALIZER_DVD');
        const avatarUrl = updated.bot_avatar_url || status.bot_avatar_url || '';
        _dvdScreensaver = new DVDScreensaver(dvdContainer, avatarUrl, null);
        _dvdScreensaver.start();
      }
    } else if (updated.state === 'visualizer') {
      // Visualizer-only mode — no video session, keep visualizer running
      errorOverlay.classList.remove('visible');
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

    // Wire HUD tool buttons with popup slider toggle
    const penPopup = document.getElementById('pen-popup');
    const eraserPopup = document.getElementById('eraser-popup');
    const shapePopup = document.getElementById('shape-popup');

    function closeAllPopups() {
      penPopup?.classList.remove('open');
      eraserPopup?.classList.remove('open');
      shapePopup?.classList.remove('open');
    }

    document.querySelectorAll('.hud-tools .hud-btn[data-tool]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        const toolName = btn.dataset.tool;
        const wasAlreadyActive = toolManager.getActiveTool()?.name === toolName;

        toolManager.selectTool(toolName);
        // Update active state on buttons
        document.querySelectorAll('.hud-tools .hud-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        // Update shape tool color when selected
        if (toolName === 'shape') {
          shapeTool.setColor(colorPicker.getColor());
        }
        // Toggle popup for pen/eraser/shape
        if (toolName === 'pen') {
          eraserPopup?.classList.remove('open');
          shapePopup?.classList.remove('open');
          penPopup?.classList.toggle('open');
        } else if (toolName === 'eraser') {
          penPopup?.classList.remove('open');
          shapePopup?.classList.remove('open');
          eraserPopup?.classList.toggle('open');
        } else if (toolName === 'shape') {
          penPopup?.classList.remove('open');
          eraserPopup?.classList.remove('open');
          shapePopup?.classList.toggle('open');
        } else if (toolName === 'sticker') {
          closeAllPopups();
          // Toggle sticker picker if already active
          if (wasAlreadyActive) {
            const isVisible = stickerPickerContainer.style.display !== 'none';
            if (isVisible) {
              stickerPicker.hide();
            } else {
              stickerPicker.show();
            }
          }
        } else {
          closeAllPopups();
        }
        e.stopPropagation();
      });
    });

    // Wire shape type buttons
    document.querySelectorAll('.shape-type-btn[data-shape]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        const shapeType = btn.dataset.shape;
        shapeTool.setShapeType(shapeType);
        // Update active state
        document.querySelectorAll('.shape-type-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        e.stopPropagation();
      });
    });

    // Wire animated toggle
    const shapeAnimatedToggle = document.getElementById('shape-animated-toggle');
    shapeAnimatedToggle?.addEventListener('change', (e) => {
      shapeTool.setAnimated(e.target.checked);
    });

    // Close popups when clicking outside
    document.addEventListener('click', (e) => {
      if (!e.target.closest('.hud-tool-wrapper')) {
        closeAllPopups();
      }
    });

    // Prevent popup clicks from closing themselves
    penPopup?.addEventListener('click', (e) => e.stopPropagation());
    eraserPopup?.addEventListener('click', (e) => e.stopPropagation());
    shapePopup?.addEventListener('click', (e) => e.stopPropagation());
    // Prevent pointer events on popups from reaching the canvas
    penPopup?.addEventListener('pointerdown', (e) => e.stopPropagation());
    eraserPopup?.addEventListener('pointerdown', (e) => e.stopPropagation());
    shapePopup?.addEventListener('pointerdown', (e) => e.stopPropagation());
    penPopup?.addEventListener('pointermove', (e) => e.stopPropagation());
    eraserPopup?.addEventListener('pointermove', (e) => e.stopPropagation());
    shapePopup?.addEventListener('pointermove', (e) => e.stopPropagation());
    penPopup?.addEventListener('pointerup', (e) => e.stopPropagation());
    eraserPopup?.addEventListener('pointerup', (e) => e.stopPropagation());
    shapePopup?.addEventListener('pointerup', (e) => e.stopPropagation());

    // Set initial active state on pen button
    document.querySelector('.hud-btn[data-tool="pen"]')?.classList.add('active');

    // Wire size and opacity sliders (pen)
    const strokeSizeSlider = document.getElementById('stroke-size');
    const strokeSizeVal = document.getElementById('stroke-size-val');
    const strokeOpacitySlider = document.getElementById('stroke-opacity');
    const strokeOpacityVal = document.getElementById('stroke-opacity-val');

    strokeSizeSlider?.addEventListener('input', (e) => {
      const v = parseInt(e.target.value, 10);
      overlay.currentWidth = v;
      if (strokeSizeVal) strokeSizeVal.textContent = v;
    });
    strokeOpacitySlider?.addEventListener('input', (e) => {
      const v = parseInt(e.target.value, 10);
      overlay.currentOpacity = v / 100;
      if (strokeOpacityVal) strokeOpacityVal.textContent = v + '%';
    });

    // Wire eraser size slider
    const eraserSizeSlider = document.getElementById('eraser-size');
    const eraserSizeVal = document.getElementById('eraser-size-val');
    eraserSizeSlider?.addEventListener('input', (e) => {
      const v = parseInt(e.target.value, 10);
      eraserTool.radius = v;
      if (eraserSizeVal) eraserSizeVal.textContent = v;
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

  // --- Search Panel Initialization ---
  const btnSearch = document.getElementById('btn-search');
  if (btnSearch && window.SearchPanel) {
    _searchPanel = new window.SearchPanel({
      container: document.getElementById('app'),
      wsSend: (msg) => wsSend(msg),
    });

    btnSearch.addEventListener('click', () => {
      _searchPanel.toggle();
      btnSearch.dataset.active = _searchPanel.active ? 'true' : 'false';
    });
  }

  // --- Visualizer Menu Initialization ---
  const menuContainer = document.getElementById('visualizer-menu-container');
  const menuToggle = document.getElementById('menu-toggle');

  if (menuContainer && menuToggle) {
    _visualizerMenu = new VisualizerMenu(
      menuContainer,
      (msg) => wsSend(msg),
      () => {
        // onClose callback — update toggle button state and return focus
        menuToggle.dataset.active = 'false';
        menuToggle.setAttribute('aria-label', 'Open visualizer menu');
        menuToggle.focus();
      }
    );

    menuToggle.addEventListener('click', () => {
      _visualizerMenu.toggle();
      const isOpen = _visualizerMenu.isOpen;
      menuToggle.dataset.active = isOpen ? 'true' : 'false';
      menuToggle.setAttribute('aria-label', isOpen ? 'Close visualizer menu' : 'Open visualizer menu');
    });
  }
})();
