"""Tests for Now Playing and Queue embed source-type-aware formatting.

Validates: Requirements 3.2, 4.3, 6.1, 6.2
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Set up environment so credentials.py can initialize (uses tmp for DB)
os.environ.setdefault("HELLODJ_DB_KEY", "test-key-for-unit-tests-only")
os.environ.setdefault("DATA_DIR", "/tmp/hellodj_test_data")

# Add parent to path so we can import from cogs
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Mock discord module before importing the cog
import discord  # noqa: E402

from video import VideoSource  # noqa: E402


# We need to import the embed functions from cogs.video
# They rely on discord being available
from cogs.video import _build_now_playing_embed, _build_queue_embed  # noqa: E402


class TestNowPlayingEmbedTidal:
    """Tidal source displays title as 'Artist — Title' (pre-formatted by TidalResolver)."""

    def test_tidal_title_with_artist(self):
        """Tidal source shows pre-formatted 'Artist — Title' in embed description."""
        source = VideoSource(
            source_type="tidal",
            file_path="/tmp/video.mp4",
            title="Daft Punk — Around the World",
            duration_seconds=240.0,
            metadata={"artist": "Daft Punk", "track_title": "Around the World", "video_id": 12345},
        )
        embed = _build_now_playing_embed(source, queue_length=0)
        assert "Daft Punk — Around the World" in embed.description

    def test_tidal_no_artist_just_title(self):
        """Tidal source with no artist just shows the title."""
        source = VideoSource(
            source_type="tidal",
            file_path="/tmp/video.mp4",
            title="Mystery Video",
            duration_seconds=120.0,
            metadata={"artist": "", "track_title": "Mystery Video", "video_id": 11111},
        )
        embed = _build_now_playing_embed(source, queue_length=0)
        assert "Mystery Video" in embed.description

    def test_tidal_no_upload_footer(self):
        """Tidal source should not have an upload footer."""
        source = VideoSource(
            source_type="tidal",
            file_path="/tmp/video.mp4",
            title="Daft Punk — Around the World",
            duration_seconds=240.0,
            metadata={"artist": "Daft Punk", "track_title": "Around the World"},
        )
        embed = _build_now_playing_embed(source, queue_length=0)
        assert embed.footer.text is discord.utils.MISSING or embed.footer.text is None or embed.footer.text == ""


class TestNowPlayingEmbedUpload:
    """Upload source displays 'Uploaded by {display_name}' in footer."""

    def test_upload_footer_attribution(self):
        """Upload source shows uploader name in embed footer."""
        source = VideoSource(
            source_type="upload",
            file_path="/tmp/uploaded.mp4",
            title="concert_clip",
            duration_seconds=60.0,
            metadata={"uploader": "CelesRenata", "original_filename": "concert_clip.mp4"},
            cleanup_on_finish=True,
        )
        embed = _build_now_playing_embed(source, queue_length=0)
        assert embed.footer.text == "Uploaded by CelesRenata"

    def test_upload_fallback_unknown_uploader(self):
        """Upload source with missing uploader metadata shows 'Unknown'."""
        source = VideoSource(
            source_type="upload",
            file_path="/tmp/uploaded.mp4",
            title="video",
            duration_seconds=30.0,
            metadata={},
            cleanup_on_finish=True,
        )
        embed = _build_now_playing_embed(source, queue_length=0)
        assert embed.footer.text == "Uploaded by Unknown"

    def test_upload_title_in_description(self):
        """Upload source shows title in embed description."""
        source = VideoSource(
            source_type="upload",
            file_path="/tmp/uploaded.mp4",
            title="concert_clip",
            duration_seconds=60.0,
            metadata={"uploader": "CelesRenata"},
            cleanup_on_finish=True,
        )
        embed = _build_now_playing_embed(source, queue_length=0)
        assert "concert_clip" in embed.description


class TestNowPlayingEmbedURL:
    """URL source displays filename from URL path (existing behavior)."""

    def test_url_source_title(self):
        """URL source shows title (filename) in description."""
        source = VideoSource(
            source_type="url",
            file_path="/tmp/funny_cat.mp4",
            title="funny_cat.mp4",
            duration_seconds=15.0,
            metadata={},
        )
        embed = _build_now_playing_embed(source, queue_length=0)
        assert "funny_cat.mp4" in embed.description

    def test_url_source_no_footer(self):
        """URL source should not have a footer."""
        source = VideoSource(
            source_type="url",
            file_path="/tmp/funny_cat.mp4",
            title="funny_cat.mp4",
            duration_seconds=15.0,
            metadata={},
        )
        embed = _build_now_playing_embed(source, queue_length=0)
        # Footer text should be empty/unset for non-upload sources
        assert embed.footer.text is discord.utils.MISSING or embed.footer.text is None or embed.footer.text == ""


class TestQueueEmbedUploadAttribution:
    """Queue listing includes '(uploaded by display_name)' for upload sources."""

    def test_queue_now_playing_upload_attribution(self):
        """Current source in queue embed shows upload attribution."""
        source = VideoSource(
            source_type="upload",
            file_path="/tmp/uploaded.mp4",
            title="my_video",
            duration_seconds=60.0,
            metadata={"uploader": "CelesRenata"},
            cleanup_on_finish=True,
        )
        embed = _build_queue_embed(source, queue=[])
        field = embed.fields[0]
        assert field.name == "Now Playing"
        assert "my_video (uploaded by CelesRenata)" in field.value

    def test_queue_up_next_upload_attribution(self):
        """Queued upload sources in 'Up Next' show upload attribution."""
        current = VideoSource(
            source_type="youtube",
            file_path="/tmp/yt.mp4",
            title="YouTube Video",
            duration_seconds=300.0,
            metadata={},
        )
        upload_source = VideoSource(
            source_type="upload",
            file_path="/tmp/uploaded.mp4",
            title="user_clip",
            duration_seconds=45.0,
            metadata={"uploader": "SomeUser"},
            cleanup_on_finish=True,
        )
        embed = _build_queue_embed(current, queue=[upload_source])
        up_next_field = embed.fields[1]
        assert up_next_field.name == "Up Next"
        assert "1. user_clip (uploaded by SomeUser)" in up_next_field.value

    def test_queue_non_upload_no_attribution(self):
        """Non-upload sources in queue don't get upload attribution."""
        current = VideoSource(
            source_type="tidal",
            file_path="/tmp/tidal.mp4",
            title="Daft Punk — Around the World",
            duration_seconds=240.0,
            metadata={"artist": "Daft Punk"},
        )
        url_source = VideoSource(
            source_type="url",
            file_path="/tmp/url.mp4",
            title="remote_video.mp4",
            duration_seconds=120.0,
            metadata={},
        )
        embed = _build_queue_embed(current, queue=[url_source])
        up_next_field = embed.fields[1]
        assert "(uploaded by" not in up_next_field.value
        assert "1. remote_video.mp4" in up_next_field.value

    def test_queue_mixed_sources(self):
        """Mixed queue correctly attributes only upload sources."""
        current = VideoSource(
            source_type="youtube",
            file_path="/tmp/yt.mp4",
            title="YT Video",
            duration_seconds=300.0,
            metadata={},
        )
        queue = [
            VideoSource(
                source_type="upload",
                file_path="/tmp/up1.mp4",
                title="clip_1",
                duration_seconds=30.0,
                metadata={"uploader": "Alice"},
                cleanup_on_finish=True,
            ),
            VideoSource(
                source_type="tidal",
                file_path="/tmp/tidal.mp4",
                title="Daft Punk — Something",
                duration_seconds=200.0,
                metadata={"artist": "Daft Punk"},
            ),
            VideoSource(
                source_type="upload",
                file_path="/tmp/up2.mp4",
                title="clip_2",
                duration_seconds=45.0,
                metadata={"uploader": "Bob"},
                cleanup_on_finish=True,
            ),
        ]
        embed = _build_queue_embed(current, queue=queue)
        up_next_field = embed.fields[1]
        lines = up_next_field.value.split("\n")
        assert "1. clip_1 (uploaded by Alice)" == lines[0]
        assert "2. Daft Punk — Something" == lines[1]
        assert "(uploaded by" not in lines[1]
        assert "3. clip_2 (uploaded by Bob)" == lines[2]
