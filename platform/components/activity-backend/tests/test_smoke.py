"""Smoke tests for activity-backend.

These exercise the pure, dependency-free surfaces (config, whiteboard stroke
validation + registry, HLS S3/CloudFront URL derivation, transcode request
building + client delegation, WebSocket-hub state transitions, visualizer/lyrics
stores, LRC parsing) without requiring aiohttp or boto3 to be installed.
"""

from __future__ import annotations

import pytest
from activity_backend.app import ActivityHandlers
from activity_backend.config import ActivityConfig
from activity_backend.hls import HlsCatalog
from activity_backend.lyrics import LyricsStore, parse_lrc
from activity_backend.models import PlaybackState
from activity_backend.transcode_client import (
    TranscodeClient,
    TranscodeError,
    TranscodeKind,
    TranscodeRequest,
)
from activity_backend.visualizer import VisualizerRegistry
from activity_backend.whiteboard import StrokeRegistry, validate_stroke_payload
from activity_backend.ws_hub import WebSocketHub

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_config_defaults() -> None:
    cfg = ActivityConfig.from_env({})
    assert cfg.route_prefix == "/activity"
    assert cfg.transcode_base_url == "http://hls-transcode:8080"
    assert cfg.port == 8090
    assert cfg.hls_s3_prefix == "hls"


def test_config_reads_env_and_normalizes_prefix() -> None:
    cfg = ActivityConfig.from_env(
        {
            "HELLODJ_TRANSCODE_URL": "http://transcode:9000",
            "HELLODJ_CLOUDFRONT_DOMAIN": "cdn.hellodj.bot",
            "HELLODJ_HLS_S3_BUCKET": "hellodj-hls",
            "HELLODJ_ACTIVITY_ROUTE_PREFIX": "activity/",
            "HELLODJ_MAX_STROKES": "42",
        }
    )
    assert cfg.transcode_base_url == "http://transcode:9000"
    assert cfg.cloudfront_domain == "cdn.hellodj.bot"
    assert cfg.hls_s3_bucket == "hellodj-hls"
    assert cfg.route_prefix == "/activity"
    assert cfg.max_strokes_per_guild == 42


# --------------------------------------------------------------------------- #
# PlaybackState anchor model
# --------------------------------------------------------------------------- #


def test_playback_state_seek_and_pause() -> None:
    state = PlaybackState(playing=False)
    state.seek_to(30.0)
    assert state.anchor_position == 30.0
    state.set_playing(True)
    assert state.playing is True
    state.set_playing(False)
    # Frozen position is >= the seek anchor
    assert state.position >= 30.0
    msg = state.to_message("video")
    assert msg["type"] == "state"
    assert msg["media_type"] == "video"


# --------------------------------------------------------------------------- #
# Whiteboard validation + registry
# --------------------------------------------------------------------------- #


def _stroke_payload(**overrides: object) -> dict:
    base = {
        "id": "s1",
        "stroke_type": "freehand",
        "points": [[0.0, 0.0], [1.0, 1.0]],
        "color": "#fff",
        "width": 2.0,
        "author": "42",
    }
    base.update(overrides)
    return base


def test_validate_stroke_ok() -> None:
    stroke, error = validate_stroke_payload(_stroke_payload())
    assert error is None
    assert stroke is not None
    assert stroke.type == "freehand"


def test_validate_stroke_bad_type() -> None:
    stroke, error = validate_stroke_payload(_stroke_payload(stroke_type="blob"))
    assert stroke is None
    assert error and "invalid type" in error


def test_validate_stroke_sticker_requires_fields() -> None:
    stroke, error = validate_stroke_payload(
        _stroke_payload(stroke_type="sticker")
    )
    assert stroke is None
    assert error and "sticker" in error


def test_stroke_registry_capacity() -> None:
    reg = StrokeRegistry(max_strokes=2)
    s1, _ = validate_stroke_payload(_stroke_payload(id="a"))
    s2, _ = validate_stroke_payload(_stroke_payload(id="b"))
    s3, _ = validate_stroke_payload(_stroke_payload(id="c"))
    assert reg.add(s1) is True
    assert reg.add(s2) is True
    assert reg.add(s3) is False  # at capacity
    assert len(reg) == 2
    assert reg.remove("a") is True
    assert reg.add(s3) is True


# --------------------------------------------------------------------------- #
# HLS catalog (S3 key + CloudFront URL derivation)
# --------------------------------------------------------------------------- #


def test_hls_catalog_derives_key_and_url() -> None:
    cfg = ActivityConfig.from_env(
        {
            "HELLODJ_HLS_S3_BUCKET": "hellodj-hls",
            "HELLODJ_CLOUDFRONT_DOMAIN": "cdn.hellodj.bot",
        }
    )
    catalog = HlsCatalog(cfg)
    loc = catalog.locate(123, "video", "abc")
    assert loc.bucket == "hellodj-hls"
    assert loc.key_prefix == "hls/guild=123/video/abc"
    assert loc.playlist_key == "hls/guild=123/video/abc/index.m3u8"
    assert loc.playlist_url == (
        "https://cdn.hellodj.bot/hls/guild=123/video/abc/index.m3u8"
    )


def test_hls_catalog_no_cdn_returns_empty_url() -> None:
    cfg = ActivityConfig.from_env({"HELLODJ_HLS_S3_BUCKET": "b"})
    catalog = HlsCatalog(cfg)
    loc = catalog.locate(1, "visualizer", "x")
    assert loc.playlist_url == ""


# --------------------------------------------------------------------------- #
# Transcode request building + client delegation (fake transport)
# --------------------------------------------------------------------------- #


def test_transcode_request_payload() -> None:
    req = TranscodeRequest(
        guild_id=7,
        kind=TranscodeKind.VIDEO,
        stream_id="sid",
        s3_bucket="b",
        s3_key_prefix="hls/guild=7/video/sid",
        source_uri="https://example/media",
    )
    payload = req.to_payload()
    assert payload["guildId"] == "7"
    assert payload["kind"] == "video"
    assert payload["sourceUri"] == "https://example/media"


class _FakeTransport:
    def __init__(self, response: dict) -> None:
        self._response = response
        self.last_url: str | None = None
        self.last_payload: dict | None = None

    async def post_json(self, url: str, payload: dict) -> dict:
        self.last_url = url
        self.last_payload = payload
        return self._response


@pytest.mark.asyncio
async def test_transcode_client_request() -> None:
    transport = _FakeTransport({"accepted": True, "playlistKey": "k"})
    client = TranscodeClient("http://transcode:8080/", transport)
    req = TranscodeRequest(
        guild_id=1,
        kind=TranscodeKind.VISUALIZER,
        stream_id="s",
        s3_bucket="b",
        s3_key_prefix="p",
        engine="drift",
    )
    result = await client.request_transcode(req)
    assert result.accepted is True
    assert result.playlist_key == "k"
    assert transport.last_url == "http://transcode:8080/v1/transcode"


class _RaisingTransport:
    async def post_json(self, url: str, payload: dict) -> dict:
        raise ConnectionError("boom")


@pytest.mark.asyncio
async def test_transcode_client_wraps_error() -> None:
    client = TranscodeClient("http://t", _RaisingTransport())
    req = TranscodeRequest(
        guild_id=1,
        kind=TranscodeKind.VIDEO,
        stream_id="s",
        s3_bucket="b",
        s3_key_prefix="p",
        source_uri="u",
    )
    with pytest.raises(TranscodeError):
        await client.request_transcode(req)


# --------------------------------------------------------------------------- #
# WebSocket hub state transitions (no aiohttp)
# --------------------------------------------------------------------------- #


def test_hub_apply_playback_broadcast() -> None:
    hub = WebSocketHub(lambda t: 1)
    out = hub.apply_playback(9, {"type": "seek", "position": 12.5})
    assert out is not None
    assert out["anchor_position"] == 12.5
    assert hub.playback_state(9).anchor_position == 12.5


def test_hub_stroke_add_remove_reset() -> None:
    hub = WebSocketHub(lambda t: 1)
    broadcast, error = hub.apply_stroke_add(3, _stroke_payload(id="k"))
    assert error is None and broadcast is not None
    assert len(hub.stroke_registry(3)) == 1
    assert hub.apply_stroke_remove(3, {"id": "k"}) is not None
    assert len(hub.stroke_registry(3)) == 0
    hub.apply_stroke_add(3, _stroke_payload(id="k2"))
    hub.apply_whiteboard_reset(3, {"type": "whiteboard_reset"})
    assert len(hub.stroke_registry(3)) == 0


def test_hub_late_join_messages_include_video_and_lyrics() -> None:
    lyrics = LyricsStore()
    lyrics.set_lyrics(5, "track-1", [(0.0, "hello")])
    hub = WebSocketHub(lambda t: 5, lyrics=lyrics)
    hub.mark_video_active(5, True)
    hub.apply_playback(5, {"type": "play"})
    messages = hub.build_late_join_messages(5)
    types = {m["type"] for m in messages}
    assert "state" in types
    assert "lyrics" in types
    state_msg = next(m for m in messages if m["type"] == "state")
    assert state_msg["media_type"] == "video"


# --------------------------------------------------------------------------- #
# Visualizer registry
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_visualizer_set_engine_and_hls_ready() -> None:
    calls: list[tuple[int, str]] = []

    async def switcher(guild_id: int, engine: str) -> None:
        calls.append((guild_id, engine))

    reg = VisualizerRegistry(switcher)
    await reg.set_engine(2, "drift")
    assert calls == [(2, "drift")]
    # Engine active but HLS not ready yet: late joiners still learn the engine
    # is active (they wait for HLS), so a message is emitted with hls_ready=False.
    pending = reg.state_message(2)
    assert pending is not None
    assert pending["engine"] == "drift"
    assert pending["hls_ready"] is False
    reg.set_hls_ready(2, "https://cdn/x.m3u8")
    msg = reg.state_message(2)
    assert msg is not None
    assert msg["engine"] == "drift"
    assert msg["hls_ready"] is True
    assert msg["playlist_url"] == "https://cdn/x.m3u8"
    # Selecting "off" clears the active state, so no late-join message.
    await reg.set_engine(2, "off")
    assert reg.state_message(2) is None


# --------------------------------------------------------------------------- #
# Lyrics store + LRC parsing
# --------------------------------------------------------------------------- #


def test_parse_lrc_sorted() -> None:
    lines = parse_lrc("[00:10.00]second\n[00:01.50]first\nno-timestamp")
    assert lines[0] == (1.5, "first")
    assert lines[1] == (10.0, "second")


def test_lyrics_store_toggle() -> None:
    store = LyricsStore()
    store.set_lyrics(1, "t", [(0.0, "a")])
    assert store.state_message(1) is not None
    store.set_enabled(1, False)
    assert store.state_message(1) is None


# --------------------------------------------------------------------------- #
# Handlers (pure logic, fake transport)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_handlers_start_video_emits_transcode() -> None:
    cfg = ActivityConfig.from_env(
        {
            "HELLODJ_HLS_S3_BUCKET": "hellodj-hls",
            "HELLODJ_CLOUDFRONT_DOMAIN": "cdn.hellodj.bot",
        }
    )
    transport = _FakeTransport({"accepted": True})
    hub = WebSocketHub(lambda t: 1)
    visualizer = VisualizerRegistry()
    lyrics = LyricsStore()
    handlers = ActivityHandlers(
        cfg,
        hub,
        HlsCatalog(cfg),
        TranscodeClient(cfg.transcode_base_url, transport),
        visualizer,
        lyrics,
    )
    status, body = await handlers.start_video(
        55, {"sourceUri": "https://example/media", "streamId": "sid"}
    )
    assert status == 202
    assert body["playlistUrl"].startswith("https://cdn.hellodj.bot/")
    assert hub.media_type(55) == "video"
    assert transport.last_payload is not None
    assert transport.last_payload["kind"] == "video"


@pytest.mark.asyncio
async def test_handlers_set_lyrics_from_lrc() -> None:
    cfg = ActivityConfig.from_env({})
    handlers = ActivityHandlers(
        cfg,
        WebSocketHub(lambda t: 1),
        HlsCatalog(cfg),
        TranscodeClient(cfg.transcode_base_url, _FakeTransport({})),
        VisualizerRegistry(),
        LyricsStore(),
    )
    status, body = handlers.set_lyrics(
        9, {"trackKey": "t", "lrc": "[00:01.00]hi"}
    )
    assert status == 200
    assert body["enabled"] is True
    assert body["lineCount"] == 1
