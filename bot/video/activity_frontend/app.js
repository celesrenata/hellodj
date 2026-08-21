/**
 * HelloDJ Video Activity Frontend
 *
 * Discord Activity player with hls.js, hover/tap controls,
 * scrubber with drag-to-seek, WebSocket sync, and auto-hiding UI.
 */
import { DiscordSDK } from './discord-sdk.js';
import { WhiteboardOverlay } from './whiteboard.js';
import { ToolManager } from './tools.js';
import { PenTool } from './pen_tool.js';
import { LineTool } from './line_tool.js';
import { ShapeTool } from './shape_tool.js';
import { TextTool } from './text_tool.js';
import { EraserTool } from './eraser_tool.js';
import { StickerTool } from './sticker_tool.js';
import { StickerPicker } from './sticker_picker.js';
import { ColorPicker } from './color_picker.js';
import { initUndo } from './undo.js';
import { initReset } from './reset.js';
import { initTextBgToggle, getTextBg } from './text_bg_toggle.js';
import { initCanvasResize } from './canvas_resize.js';
import { ControlsPassthrough } from './controls_passthrough.js';
import { initWhiteboardSync } from './ws_whiteboard.js';

(async () => {
  // DOM
  const videoEl = document.getElementById('player');
  const controlsOverlay = document.getElementById('controls-overlay');
  const titleBar = document.getElementById('title-bar');
  const errorOverlay = document.getElementById('error-overlay');
  const errorMessage = document.getElementById('error-message');
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

  const fetchStatus = async () => {
    try {
      const resp = await fetch(`status/${guildId}`, { headers: authHeaders });
      if (!resp.ok) {
        if (resp.status === 404) showError('No active video session.');
        else if (resp.status === 401) showError('Auth failed.');
        else showError(`Status error: HTTP ${resp.status}`);
        return null;
      }
      return await resp.json();
    } catch (e) {
      showError(`Fetch error: ${e.message}`);
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
        videoEl.play().catch(() => {});
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
        // Late-joiner sync: apply full state
        _remoteAction = true;
        if (data.position != null) videoEl.currentTime = data.position;
        if (data.playing) {
          videoEl.play().catch(() => {});
        } else {
          videoEl.pause();
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

  const initHls = (playlistUrl) => {
    if (hls) { hls.destroy(); hls = null; }

    if (!Hls.isSupported()) {
      videoEl.src = playlistUrl;
      videoEl.play().catch(() => {});
      connectWebSocket();
      return;
    }

    hls = new Hls({
      enableWorker: true,
      lowLatencyMode: false,
      maxBufferLength: 30,
      maxMaxBufferLength: 60,
      startLevel: 0,
      startPosition: 0,
    });
    hls.loadSource(playlistUrl);
    hls.attachMedia(videoEl);

    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      videoEl.play().catch(() => {});
      // Connect WebSocket after HLS is initialized
      connectWebSocket();
    });

    hls.on(Hls.Events.ERROR, (_ev, data) => {
      if (!data.fatal) return;
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR) { hls.recoverMediaError(); return; }
      showError(`Stream error: ${data.details}`);
    });
  };

  // --- Player Controls ---
  // Unmute on first interaction with the video area (not UI buttons or canvas)
  const unmute = (e) => {
    // Don't unmute from button/control/canvas clicks
    if (e.target.closest('.controls-row') || e.target.closest('.whiteboard-hud') || 
        e.target.closest('.hud-btn') || e.target.closest('.ctrl-btn') ||
        e.target.id === 'whiteboard-canvas' || e.target.closest('.sticker-picker')) return;
    if (videoEl.muted) {
      videoEl.muted = false;
      document.removeEventListener('click', unmute);
      document.removeEventListener('touchstart', unmute);
    }
  };
  document.addEventListener('click', unmute);
  document.addEventListener('touchstart', unmute);

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
    const newTime = Math.max(0, videoEl.currentTime - 10);
    videoEl.currentTime = newTime;
    if (!_remoteAction) wsSend({ type: 'seek', position: newTime });
  });

  btnForward.addEventListener('click', () => {
    const newTime = Math.min(videoEl.duration || 0, videoEl.currentTime + 10);
    videoEl.currentTime = newTime;
    if (!_remoteAction) wsSend({ type: 'seek', position: newTime });
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
  };
  videoEl.addEventListener('timeupdate', updateTime);
  videoEl.addEventListener('progress', updateTime);

  // Update play/pause icon on state change
  videoEl.addEventListener('play', () => { btnPlayPause.textContent = '⏸️'; });
  videoEl.addEventListener('pause', () => { btnPlayPause.textContent = '▶️'; });

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
    // Stream is ready — play countdown first, then switch to HLS
    videoEl.src = 'static/countdown.mp4'; videoEl.muted = true;
    videoEl.play().catch(() => {});

    videoEl.addEventListener('ended', () => {
      // Countdown finished — switch to HLS stream
      // Use absolute URL to bypass Discord's proxy for segment delivery
      videoEl.removeAttribute('src'); videoEl.muted = false;
      const playlistUrl = `stream/${guildId}/playlist.m3u8?token=${encodeURIComponent(instanceId)}`;
      initHls(playlistUrl);
    }, { once: true });
  } else if (status.state === 'buffering') {
    // Still transcoding — show countdown, then poll until streaming
    titleBar.textContent = `${status.video_title || 'Loading...'} — Preparing stream...`;
    videoEl.src = 'static/countdown.mp4'; videoEl.muted = true;
    videoEl.play().catch(() => {});

    // Poll until stream is ready
    const waitForStream = setInterval(async () => {
      const s = await fetchStatus();
      if (s && s.state === 'streaming' && s.playlist_url) {
        clearInterval(waitForStream);
        titleBar.textContent = formatTitle(s.video_title, s.uploader);
        // Wait for countdown to finish if still playing
        if (!videoEl.ended && videoEl.currentTime < videoEl.duration) {
          videoEl.addEventListener('ended', () => {
            videoEl.removeAttribute('src'); videoEl.muted = false;
            const playlistUrl = `stream/${guildId}/playlist.m3u8?token=${encodeURIComponent(instanceId)}`;
            initHls(playlistUrl);
          }, { once: true });
        } else {
          videoEl.removeAttribute('src'); videoEl.muted = false;
          const playlistUrl = `stream/${guildId}/playlist.m3u8?token=${encodeURIComponent(instanceId)}`;
          initHls(playlistUrl);
        }
      }
    }, 2000);
  } else {
    showError('No active video session. Start one with /video play');
  }

  // --- Poll for changes ---
  let currentSessionId = status.session_id;
  setInterval(async () => {
    const updated = await fetchStatus();
    if (!updated) return;
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
        const playlistUrl = `stream/${guildId}/playlist.m3u8?token=${encodeURIComponent(instanceId)}`;
        initHls(playlistUrl);
      }
    }
  }, 10000);

  // --- Whiteboard Initialization ---
  const whiteboardCanvas = document.getElementById('whiteboard-canvas');
  const whiteboardHud = document.getElementById('whiteboard-hud');
  const btnWhiteboard = document.getElementById('btn-whiteboard');

  if (whiteboardCanvas && whiteboardHud && btnWhiteboard) {
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
    const penTool = new PenTool({ getCanvas, getColor });
    const lineTool = new LineTool({ getCanvas, getColor });
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

    // Sync whiteboard active state with controls passthrough + sidebar layout
    const appEl = document.getElementById('app');
    const useSidebar = () => window.innerWidth >= 960;
    const origActivate = overlay.activate.bind(overlay);
    const origDeactivate = overlay.deactivate.bind(overlay);

    overlay.activate = () => {
      origActivate();
      controlsPassthrough.setWhiteboardActive(true);
      if (useSidebar()) appEl.classList.add('whiteboard-sidebar-active');
    };
    overlay.deactivate = () => {
      origDeactivate();
      controlsPassthrough.setWhiteboardActive(false);
      appEl.classList.remove('whiteboard-sidebar-active');
    };

    // Handle viewport resize while whiteboard is active — toggle sidebar class
    window.addEventListener('resize', () => {
      if (overlay.mode === 'active') {
        if (useSidebar()) {
          appEl.classList.add('whiteboard-sidebar-active');
        } else {
          appEl.classList.remove('whiteboard-sidebar-active');
        }
      }
    });

    // Update shape tool color when color changes
    swatches.forEach(s => s.addEventListener('click', () => shapeTool.setColor(colorPicker.getColor())));
    if (customColorInput) customColorInput.addEventListener('input', () => shapeTool.setColor(colorPicker.getColor()));
  }
})();
