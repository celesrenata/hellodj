"""HelloDJ — HLS file cleanup utilities.

Provides functions for cleaning up HLS temporary directories:
- Per-session cleanup (on session end or transcode crash)
- Per-guild cleanup (remove empty guild directory)
- Orphan scan (on bot startup, remove dirs not matching active sessions)
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from video.hls_transcode import _HLS_BASE_DIR

log = logging.getLogger(__name__)


def cleanup_session_dir(guild_id: int, session_id: str) -> None:
    """Delete all files in the session directory and remove it.

    Handles the case where the directory doesn't exist (no-op) and
    logs any OS-level errors without raising.

    Args:
        guild_id: The guild's Discord snowflake ID.
        session_id: The UUID session identifier.
    """
    session_dir = _HLS_BASE_DIR / str(guild_id) / session_id
    if not session_dir.exists():
        return

    try:
        shutil.rmtree(session_dir)
        log.info("Cleaned up HLS session directory: %s", session_dir)
    except OSError as exc:
        log.warning("Failed to clean up HLS session directory %s: %s", session_dir, exc)


def cleanup_guild_dir(guild_id: int) -> None:
    """Remove the guild directory if it is empty.

    After session directories are removed, the parent guild directory
    may be left empty. This removes it to keep /tmp/hellodj_hls/ tidy.

    Args:
        guild_id: The guild's Discord snowflake ID.
    """
    guild_dir = _HLS_BASE_DIR / str(guild_id)
    if not guild_dir.exists():
        return

    try:
        # Only remove if empty — rmdir raises OSError if not empty
        guild_dir.rmdir()
        log.info("Removed empty guild directory: %s", guild_dir)
    except OSError:
        # Directory not empty or other issue — that's fine
        pass


def cleanup_orphaned_dirs(active_sessions: set[tuple[int, str]]) -> None:
    """Scan /tmp/hellodj_hls/ and remove directories not matching active sessions.

    Iterates over guild directories, then session directories within each guild.
    Any session directory whose (guild_id, session_id) pair is NOT in the
    active_sessions set is removed.

    After cleaning sessions, empty guild directories are also removed.

    Args:
        active_sessions: Set of (guild_id, session_id) tuples representing
            currently active sessions that should NOT be cleaned up.
    """
    if not _HLS_BASE_DIR.exists():
        log.info("HLS base directory does not exist, nothing to clean up")
        return

    removed_count = 0

    try:
        for guild_dir in _HLS_BASE_DIR.iterdir():
            if not guild_dir.is_dir():
                continue

            # Parse guild_id from directory name
            try:
                guild_id = int(guild_dir.name)
            except ValueError:
                # Not a valid guild directory — remove it
                log.warning("Removing invalid HLS directory: %s", guild_dir)
                try:
                    shutil.rmtree(guild_dir)
                    removed_count += 1
                except OSError as exc:
                    log.warning("Failed to remove invalid directory %s: %s", guild_dir, exc)
                continue

            # Scan session directories within the guild
            for session_dir in guild_dir.iterdir():
                if not session_dir.is_dir():
                    # Stray file — remove it
                    try:
                        session_dir.unlink()
                    except OSError:
                        pass
                    continue

                session_id = session_dir.name

                if (guild_id, session_id) not in active_sessions:
                    try:
                        shutil.rmtree(session_dir)
                        removed_count += 1
                        log.info(
                            "Removed orphaned HLS session directory: %s",
                            session_dir,
                        )
                    except OSError as exc:
                        log.warning(
                            "Failed to remove orphaned directory %s: %s",
                            session_dir,
                            exc,
                        )

            # Clean up empty guild directory
            cleanup_guild_dir(guild_id)

    except OSError as exc:
        log.warning("Error scanning HLS base directory for orphans: %s", exc)

    if removed_count > 0:
        log.info("Startup cleanup: removed %d orphaned HLS directories", removed_count)
    else:
        log.info("Startup cleanup: no orphaned HLS directories found")


def cleanup_all() -> None:
    """Remove everything under /tmp/hellodj_hls/.

    Used on fresh startup when no sessions exist. Removes the entire
    base directory tree and recreates the empty base directory.
    """
    if not _HLS_BASE_DIR.exists():
        return

    try:
        shutil.rmtree(_HLS_BASE_DIR)
        log.info("Removed all HLS temporary files from %s", _HLS_BASE_DIR)
    except OSError as exc:
        log.warning("Failed to remove HLS base directory %s: %s", _HLS_BASE_DIR, exc)
