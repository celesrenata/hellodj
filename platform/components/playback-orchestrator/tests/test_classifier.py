"""Unit tests for the audio/video/radio content classifier."""

from __future__ import annotations

import pytest

from playback_orchestrator.classifier import ContentType, classify


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("audio", ContentType.AUDIO),
        ("video", ContentType.VIDEO),
        ("radio", ContentType.RADIO),
    ],
)
def test_mode_override_wins(mode: str, expected: ContentType) -> None:
    result = classify("anything at all", mode=mode)  # type: ignore[arg-type]
    assert result.content_type is expected
    assert result.confidence == "definite"
    assert result.source_hint == "mode_override"


def test_video_attachment_is_video() -> None:
    result = classify("clip", attachment_content_type="video/mp4")
    assert result.content_type is ContentType.VIDEO
    assert result.source_hint == "attachment"


def test_audio_attachment_is_audio() -> None:
    result = classify("song", attachment_content_type="audio/mpeg")
    assert result.content_type is ContentType.AUDIO


def test_radio_prefix() -> None:
    result = classify("radio:lofi hip hop")
    assert result.content_type is ContentType.RADIO
    assert result.confidence == "definite"


def test_spsearch_prefix_is_audio() -> None:
    assert classify("spsearch:daft punk").source_hint == "spotify"


def test_empty_query_defaults_audio_search() -> None:
    result = classify("   ")
    assert result.content_type is ContentType.AUDIO
    assert result.source_hint == "search"


def test_plain_text_defaults_audio_search() -> None:
    result = classify("never gonna give you up")
    assert result.content_type is ContentType.AUDIO
    assert result.source_hint == "search"


def test_youtube_music_is_audio() -> None:
    result = classify("https://music.youtube.com/watch?v=abc")
    assert result.content_type is ContentType.AUDIO
    assert result.source_hint == "youtube_music"


def test_spotify_url_is_audio() -> None:
    assert classify("https://open.spotify.com/track/x").source_hint == "spotify"


def test_tidal_video_path_is_video() -> None:
    result = classify("https://tidal.com/browse/video/12345")
    assert result.content_type is ContentType.VIDEO
    assert result.source_hint == "tidal_video"


def test_tidal_audio_url_is_audio() -> None:
    result = classify("https://tidal.com/browse/track/999")
    assert result.content_type is ContentType.AUDIO
    assert result.source_hint == "tidal"


def test_soundcloud_url_is_audio() -> None:
    assert classify("https://soundcloud.com/artist/track").source_hint == "soundcloud"


def test_direct_video_extension_is_video() -> None:
    result = classify("https://cdn.example.com/clip.mp4")
    assert result.content_type is ContentType.VIDEO
    assert result.source_hint == "direct_video"


def test_stream_manifest_is_radio() -> None:
    result = classify("https://cdn.example.com/stream.m3u8")
    assert result.content_type is ContentType.RADIO
    assert result.source_hint == "stream_manifest"


def test_radio_host_is_radio() -> None:
    result = classify("https://ice1.somafm.com/groovesalad-128-mp3")
    assert result.content_type is ContentType.RADIO
    assert result.source_hint == "radio_host"


def test_youtube_url_default_audio() -> None:
    result = classify("https://www.youtube.com/watch?v=abc")
    assert result.content_type is ContentType.AUDIO
    assert result.confidence == "default"
    assert result.source_hint == "youtube"


def test_unknown_url_defaults_audio() -> None:
    result = classify("https://example.org/whatever")
    assert result.content_type is ContentType.AUDIO
    assert result.source_hint == "unknown_url"
