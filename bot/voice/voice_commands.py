"""Voice command orchestrator: wake→STT→intent→action→TTS state machine.

Orchestrates the full voice interaction cycle per guild per user.
"""

import asyncio
import logging
import re

import discord
import numpy as np
import wavelink
from wavelink import Playable

import player
import storage
import blacklist as _blacklist

from .audio_pipeline import AudioPipeline
from .intent import classify_intent, intent_to_string
from .llm_intent import LLMIntentExtractor, ADMIN_ACTIONS
from .stt import STTEngine
from .tts import TTSEngine, TTSPLayer
from .query_handler import QueryHandler
from debug import get_debug_logger

log = logging.getLogger(__name__)
dbg = get_debug_logger("voice_cmd")

# ── confirmation keywords ─────────────────────────────────────────────────

CONFIRM_WORDS = {"confirm", "yes", "proceed", "do it", "yeah", "yep", "sure", "go ahead"}
CANCEL_WORDS = {"cancel", "no", "stop", "abort", "never mind", "nope", "don't"}

# ── admin command parsing ─────────────────────────────────────────────────

def _parse_target_user(transcript: str, guild: discord.Guild) -> discord.Member | None:
    """Extract a Discord member mention from transcript.

    Tries to match @username, nickname, or display name.
    """
    # Try to find a Discord ID mention pattern
    mention_match = re.search(r"<@!?(\d+)>", transcript)
    if mention_match:
        user_id = int(mention_match.group(1))
        return guild.get_member(user_id)

    # Try matching by name
    words = transcript.split()
    for word in words:
        # Strip punctuation
        clean = word.strip(",.!?@#")
        member = guild.get_member_named(clean)
        if member:
            return member

    return None


def _parse_duration(transcript: str) -> int | None:
    """Extract duration in minutes from transcript."""
    match = re.search(r"(\d+)\s*(min|minutes|minute|m|hour|hours|h)", transcript)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit in ("hour", "hours", "h"):
            return value * 60
        return value
    return None


# ── VoiceCommandSession ──────────────────────────────────────────────────

class VoiceCommandSession:
    """Tracks the state of a voice interaction for one user in one guild.

    States:
        IDLE       — waiting for wake word
        WAKE_TICK  — wake word detected, capturing STT audio
        PROCESSING — STT done, executing action
        CONFIRM    — waiting for admin confirmation
        RESPONDING — TTS is speaking
    """

    IDLE = "idle"
    WAKE_TICK = "wake_tick"
    PROCESSING = "processing"
    CONFIRM = "confirm"
    RESPONDING = "responding"

    def __init__(self, guild_id: int, user_id: int):
        self.guild_id = guild_id
        self.user_id = user_id
        self.state = self.IDLE
        self.wake_ssrc: int | None = None
        self.wake_frame_index: int | None = None
        self.transcript: str = ""
        self.intent: dict | None = None
        self.confirm_action: dict | None = None
        self.confirm_task: asyncio.Task | None = None
        self.member: discord.Member | None = None


# ── VoiceCommandOrchestrator ─────────────────────────────────────────────

class VoiceCommandOrchestrator:
    """Central orchestrator for voice interactions.

    Wires together: AudioPipeline → STT → Intent → Action → TTS
    """

    def __init__(self, wakeword_model, bot: discord.ext.commands.Bot):
        self.bot = bot
        self.pipeline = AudioPipeline(wakeword_model)
        self.stt = STTEngine()
        self.tts = TTSEngine()
        self.query = QueryHandler()
        self.llm_intent = LLMIntentExtractor()
        self._sessions: dict[str, VoiceCommandSession] = {}  # key: "{guild_id}:{user_id}"
        self._pending_confirm: dict[str, asyncio.Task] = {}

    async def close(self) -> None:
        """Clean up resources (aiohttp sessions, etc.)."""
        await self.llm_intent.close()

    def _session_key(self, guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"

    def get_or_create_session(self, guild_id: int, user_id: int) -> VoiceCommandSession:
        key = self._session_key(guild_id, user_id)
        if key not in self._sessions:
            self._sessions[key] = VoiceCommandSession(guild_id, user_id)
        return self._sessions[key]

    # ── pipeline integration ─────────────────────────────────────────────

    async def on_voice_receive(
        self,
        guild_id: int,
        ssrc: int,
        opus_data: bytes,
        user_id: int | None = None,
    ) -> None:
        """Called every 20ms when an Opus frame arrives."""
        self.pipeline.on_voice_receive(ssrc, opus_data, user_id)

    async def on_speaking_start(self, guild_id: int, ssrc: int, user_id: int) -> None:
        self.pipeline.on_speaking_start(ssrc, user_id)

    async def on_speaking_stop(self, guild_id: int, ssrc: int) -> None:
        self.pipeline.on_speaking_stop(ssrc)

    async def tick(self) -> None:
        """Called every 80ms. Runs wake word detection and orchestrates responses."""
        # Wake word detection
        detection = self.pipeline.tick()
        if detection:
            ssrc, user_id = detection
            await self._on_wake_detected(ssrc, user_id)

    async def _on_wake_detected(self, ssrc: int, user_id: int) -> None:
        """Wake word detected — start STT capture and begin interaction."""
        guild_id = 0  # We need guild_id — derive from user's voice channel

        # Find which guild this user is in
        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if member and member.voice:
                guild_id = guild.id
                break

        if not guild_id:
            log.warning("Wake word detected but user %s not in any guild voice", user_id)
            return

        # Blacklist gate — revoked/blacklisted users must NOT trigger wake word.
        if _blacklist.is_blacklisted(guild_id, user_id):
            log.info(
                "Ignoring wake word from blacklisted user %s in guild %s",
                user_id, guild_id,
            )
            return

        session = self.get_or_create_session(guild_id, user_id)
        if session.state != VoiceCommandSession.IDLE:
            return  # Already in an interaction

        session.state = VoiceCommandSession.WAKE_TICK
        session.wake_ssrc = ssrc
        session.wake_frame_index = self.pipeline._frame_counter.get(ssrc, 0)

        log.info(
            "Wake word → starting STT capture (guild=%s, user=%s, ssrc=%s)",
            guild_id, user_id, ssrc,
        )

        # Wait for silence (async, up to 10s)
        await self._wait_for_speech_end(session)

    async def _wait_for_speech_end(self, session: VoiceCommandSession) -> None:
        """Wait for the user to stop speaking, then transcribe."""
        ssrc = session.wake_ssrc
        if ssrc is None:
            return

        # Poll every 200ms for silence
        for _ in range(50):  # up to 10 seconds
            await asyncio.sleep(0.2)
            source = self.pipeline._sources.get(ssrc)
            if source is None:
                break
            # Check RMS of recent audio
            recent = source.get_latest(3200)  # last 200ms at 16kHz
            if len(recent) == 0:
                continue
            rms = np.sqrt(np.mean(recent.astype(np.float32) ** 2))
            if rms < 500:  # silence threshold
                break

        # Capture the audio (guard against None frame_index)
        wake_idx = session.wake_frame_index or 0
        pcm = self.pipeline.capture_speech_since(ssrc, wake_idx)
        if len(pcm) < 1600:  # less than 100ms — too short
            log.info("STT audio too short (%d samples), skipping", len(pcm))
            session.state = VoiceCommandSession.IDLE
            return

        # Transcribe
        transcript, _ = self.stt.transcribe_with_detection(pcm)
        if not transcript.strip():
            log.info("STT returned empty transcript")
            session.state = VoiceCommandSession.IDLE
            return

        session.transcript = transcript
        log.info("STT transcript (%s): %s", session.guild_id, transcript[:120])

        # Resolve guild and member for command processing
        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            log.warning("Guild %s not found", session.guild_id)
            session.state = VoiceCommandSession.IDLE
            return

        member = guild.get_member(session.user_id)
        if member is None:
            log.warning("Member %s not found in guild %s", session.user_id, session.guild_id)
            session.state = VoiceCommandSession.IDLE
            return

        session.member = member

        # Extract commands via LLM (falls back to keyword classifier on failure)
        commands = await self.llm_intent.extract(transcript)
        if not commands:
            await self._speak(session, guild, "I didn't understand that.")
            session.state = VoiceCommandSession.IDLE
            return

        # Execute command array sequentially
        session.state = VoiceCommandSession.PROCESSING
        await self._process_commands(session, guild, member, commands)

    async def _execute_intent(self, session: VoiceCommandSession) -> None:
        """Execute the classified intent."""
        intent = session.intent
        if intent is None:
            return

        guild_id = session.guild_id
        user_id = session.user_id

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        member = guild.get_member(user_id)
        if member is None:
            return

        session.member = member

        try:
            if intent["intent"] == "music":
                await self._execute_music(session, guild, member)
            elif intent["intent"] == "admin":
                await self._execute_admin(session, guild, member)
            elif intent["intent"] == "general":
                await self._execute_general(session, guild, member)
        except Exception as exc:
            log.error("Intent execution failed: %s", exc)
            await self._speak(session, guild, "Sorry, something went wrong.")

        # Reset session after execution (guard against None ssrc)
        wake_ssrc = session.wake_ssrc or 0
        self.pipeline.reset_ssrc(wake_ssrc)
        session.state = VoiceCommandSession.IDLE

    # ── compound command processing (LLM pipeline) ────────────────────────

    async def _process_commands(
        self,
        session: VoiceCommandSession,
        guild: discord.Guild,
        member: discord.Member,
        commands: list[dict],
    ) -> None:
        """Process a Command_Object array sequentially.

        Routes admin commands through CONFIRM state, executes non-admin
        commands immediately. Halts on failure (Requirement 5.3).
        Produces a single summary TTS for compound utterances (Requirement 8.2).
        """
        results: list[dict] = []

        for cmd in commands:
            action = cmd.get("action", "")

            if action in ADMIN_ACTIONS:
                # Permission check before entering CONFIRM
                if (
                    not member.guild_permissions.administrator
                    and not member.guild_permissions.moderate_members
                ):
                    results.append({
                        "action": action,
                        "success": False,
                        "message": "You need administrator permission for that.",
                    })
                    break  # Halt on failure

                result = await self._execute_admin_from_command(
                    session, guild, member, cmd
                )
                result["_admin_handled"] = True  # Already spoke its own TTS
            else:
                result = await self._execute_command(session, guild, member, cmd)

            results.append(result)

            if not result.get("success", False):
                # Halt on failure (Requirement 5.3)
                break

        # Build and speak summary TTS (Requirement 8.2)
        summary = self._build_summary(results)
        if summary:
            await self._speak(session, guild, summary)

        # Reset session after execution
        wake_ssrc = session.wake_ssrc or 0
        self.pipeline.reset_ssrc(wake_ssrc)
        session.state = VoiceCommandSession.IDLE

    async def _execute_command(
        self,
        session: VoiceCommandSession,
        guild: discord.Guild,
        member: discord.Member,
        cmd: dict,
    ) -> dict:
        """Execute a single non-admin Command_Object.

        Uses cmd["source"] for source override without mutating guild state.
        Returns a result dict with action, success, and message fields.
        """
        action = cmd.get("action", "")
        query = cmd.get("query")
        arguments = cmd.get("arguments", {})
        state = player.get_state(guild.id)
        player_obj = player.get_player(guild.id)

        # Per-command source override (Reqs 4.3, 4.4) — NEVER mutate guild state
        source = cmd.get("source") or state.get("source_provider", "youtube")

        try:
            if action == "play":
                if not query:
                    return {"action": action, "success": False, "message": "No song specified."}
                msg = await self._resolve_and_play_with_source(guild, query, member, source)
                return {"action": action, "success": True, "message": msg}

            elif action == "skip":
                if player_obj and (player_obj.playing or player_obj.paused):
                    await player_obj.stop()
                    return {"action": action, "success": True, "message": "Skipped."}
                return {"action": action, "success": False, "message": "Nothing to skip."}

            elif action == "pause":
                if player_obj and player_obj.playing:
                    await player_obj.pause(True)
                    return {"action": action, "success": True, "message": "Paused."}
                return {"action": action, "success": False, "message": "Nothing playing."}

            elif action == "resume":
                if player_obj and player_obj.paused:
                    await player_obj.pause(False)
                    return {"action": action, "success": True, "message": "Resumed."}
                return {"action": action, "success": False, "message": "Nothing paused."}

            elif action == "stop":
                state["persist_enabled"] = False
                player.clear_queue(state)
                if player_obj and (player_obj.playing or player_obj.paused):
                    await player_obj.stop()
                state["current"] = None
                return {"action": action, "success": True, "message": "Stopped and cleared."}

            elif action == "shuffle":
                player.shuffle_queue(state)
                player.persist(guild.id)
                return {"action": action, "success": True, "message": "Queue shuffled."}

            elif action == "remove":
                index = arguments.get("index")
                if index is None:
                    return {"action": action, "success": False, "message": "No track number specified."}
                removed = player.remove_from_queue(state, int(index) - 1)
                if removed is None:
                    return {"action": action, "success": False, "message": "Invalid track number."}
                player.persist(guild.id)
                return {"action": action, "success": True, "message": "Removed from queue."}

            elif action == "repeat":
                mode = arguments.get("mode", "off")
                if mode in ("on", "single", "one"):
                    player.set_repeat(state, "single")
                    return {"action": action, "success": True, "message": "Repeat single track."}
                elif mode in ("all", "queue"):
                    player.set_repeat(state, "queue")
                    return {"action": action, "success": True, "message": "Repeat entire queue."}
                else:
                    player.set_repeat(state, "off")
                    return {"action": action, "success": True, "message": "Repeat off."}

            elif action == "queue":
                q_len = len(state["queue"])
                if q_len == 0:
                    return {"action": action, "success": True, "message": "Queue is empty."}
                return {"action": action, "success": True, "message": f"Queue has {q_len} tracks."}

            elif action == "join":
                if member.voice:
                    p = await player.connect_player(member.voice.channel)
                    state["player"] = p
                    state["voice_channel"] = member.voice.channel
                    return {
                        "action": action, "success": True,
                        "message": f"Joined {member.voice.channel.name}.",
                    }
                return {"action": action, "success": False, "message": "You're not in a voice channel."}

            elif action == "leave":
                if player_obj and player_obj.connected:
                    player.clear_queue(state)
                    state["current"] = None
                    await player_obj.disconnect()
                    return {"action": action, "success": True, "message": "Left the voice channel."}
                return {"action": action, "success": False, "message": "Not in a voice channel."}

            elif action == "load_playlist":
                name = arguments.get("name", query or "")
                if not name:
                    return {"action": action, "success": False, "message": "No playlist name specified."}
                playlist_data = storage.get(guild.id, name)
                if playlist_data is None:
                    # Req 7.3: list up to 5 available names as suggestions
                    available = storage.names(guild.id)[:5]
                    suggestions = ", ".join(available) if available else "none"
                    return {"action": action, "success": False, "message": f"Playlist not found. Try: {suggestions}"}
                tracks = playlist_data.get("tracks", [])
                if not tracks:
                    return {"action": action, "success": False, "message": f"Playlist '{name}' is empty."}
                was_empty = len(state["queue"]) == 0
                for t in tracks:
                    state["queue"].append(t)
                player.persist(guild.id)
                # Begin playback if queue was previously empty (Req 7.1)
                if was_empty:
                    p = player.get_player(guild.id)
                    if p and p.connected and not p.playing and not p.paused:
                        await player._play_next_from_queue(guild.id)
                return {"action": action, "success": True, "message": f"Loaded {len(tracks)} tracks from '{name}'."}

            elif action == "save_playlist":
                name = arguments.get("name", query or "")
                if not name:
                    return {"action": action, "success": False, "message": "No playlist name specified."}
                # Req 7.2: validate name length (1-64 chars)
                if len(name) > 64:
                    return {"action": action, "success": False, "message": "Playlist name must be 1-64 characters."}
                # Req 7.4: empty queue check
                if not state["queue"]:
                    return {"action": action, "success": False, "message": "Nothing in the queue to save."}
                # Req 7.5: name collision check (case-insensitive)
                if storage._find(guild.id, name):
                    return {"action": action, "success": False, "message": f"'{name}' already exists. Pick another name."}
                # Create and populate the playlist
                await storage.create(guild.id, name, member.id)
                for t in state["queue"]:
                    track = {
                        "url": t.get("webpage_url") or t.get("url", ""),
                        "title": t.get("title", "Unknown"),
                        "duration": t.get("duration", 0),
                    }
                    await storage.add_track(guild.id, name, track)
                count = len(state["queue"])
                return {"action": action, "success": True, "message": f"Saved {count} tracks as '{name}'."}

            else:
                return {"action": action, "success": False, "message": f"Unknown command: {action}"}

        except Exception as exc:
            log.error("Command execution failed (%s): %s", action, exc)
            return {"action": action, "success": False, "message": f"Failed: {action}"}

    async def _execute_admin_from_command(
        self,
        session: VoiceCommandSession,
        guild: discord.Guild,
        member: discord.Member,
        cmd: dict,
    ) -> dict:
        """Route an admin Command_Object through the CONFIRM state.

        Uses the existing _confirm_and_execute flow which handles its own
        TTS (confirmation prompt, confirmed/cancelled/failed messages).
        Returns a result dict so _process_commands knows whether to halt.

        Note: Since _confirm_and_execute speaks its own TTS, the summary
        builder should account for admin commands having already spoken.
        """
        action = cmd.get("action", "")
        query = cmd.get("query")
        arguments = cmd.get("arguments", {})

        # Parse target user from query field
        target = None
        if query:
            target = guild.get_member_named(query)
            if target is None:
                # Try partial match on display names
                for m in guild.members:
                    if query.lower() in m.display_name.lower():
                        target = m
                        break

        if action == "mute":
            if target is None:
                return {"action": action, "success": False, "message": "User not found."}
            duration = arguments.get("duration")
            dur_text = f" for {duration} minutes" if duration else ""
            await self._confirm_and_execute(
                session, guild, member,
                f"mute {target.display_name}{dur_text}",
                lambda: self._do_mute(target, duration),
            )
            return {"action": action, "success": True, "message": f"Muted {target.display_name}."}

        elif action == "kick":
            if target is None:
                return {"action": action, "success": False, "message": "User not found."}
            await self._confirm_and_execute(
                session, guild, member,
                f"kick {target.display_name}",
                lambda: self._do_kick(target),
            )
            return {"action": action, "success": True, "message": f"Kicked {target.display_name}."}

        elif action == "ban":
            if target is None:
                return {"action": action, "success": False, "message": "User not found."}
            await self._confirm_and_execute(
                session, guild, member,
                f"ban {target.display_name}",
                lambda: self._do_ban(target),
            )
            return {"action": action, "success": True, "message": f"Banned {target.display_name}."}

        elif action == "timeout":
            if target is None:
                return {"action": action, "success": False, "message": "User not found."}
            duration = arguments.get("duration", 10)
            await self._confirm_and_execute(
                session, guild, member,
                f"timeout {target.display_name} for {duration} minutes",
                lambda: self._do_timeout(target, duration),
            )
            return {"action": action, "success": True, "message": f"Timed out {target.display_name}."}

        elif action == "revoke":
            if target is None:
                return {"action": action, "success": False, "message": "User not found."}
            await self._confirm_and_execute(
                session, guild, member,
                f"revoke {target.display_name}",
                lambda: self._do_revoke(target, guild),
            )
            return {"action": action, "success": True, "message": f"Revoked {target.display_name}."}

        elif action in ("restart", "shutdown"):
            label = "restart the bot" if action == "restart" else "shut down the bot"
            await self._confirm_and_execute(
                session, guild, member,
                label,
                lambda: self._do_shutdown(action),
            )
            return {"action": action, "success": True, "message": f"{action.capitalize()} initiated."}

        return {"action": action, "success": False, "message": f"Unknown admin command: {action}"}

    async def _resolve_and_play_with_source(
        self,
        guild: discord.Guild,
        song: str,
        member: discord.Member,
        source: str,
    ) -> str:
        """Search for a song using a specific source and enqueue it.

        Like _resolve_and_play but accepts an explicit source parameter
        so that per-command source overrides work without mutating guild state.
        """
        state = player.get_state(guild.id)
        voice_channel = member.voice.channel if member.voice else None
        if voice_channel is None:
            return "You need to be in a voice channel."

        player_obj = state.get("player")
        if not player_obj or not player_obj.connected:
            player_obj = await player.connect_player(voice_channel)
            state["player"] = player_obj

        tracks = await Playable.search(song, source=_source_for(source))
        if not tracks:
            return "I couldn't find that song."

        info = player._track_entry(tracks[0], source)
        was_playing = player_obj.playing or player_obj.paused
        await player.add_track(state, guild.id, info)
        p = player.get_player(guild.id)
        started = False
        if p and p.connected and not p.playing and not p.paused:
            await player._play_next_from_queue(guild.id)
            started = await self._wait_until_playing(p)
        if started and not was_playing:
            artist = info.get("author") or "Unknown Artist"
            return f"Now Playing: {info['title']} by {artist}"
        return f"Added {info['title']} to the queue."

    def _build_summary(self, results: list[dict]) -> str:
        """Build a ≤150 char TTS summary for compound utterances.

        For single commands, returns the command's message directly.
        For multiple commands, produces a compact summary.
        Admin commands that already spoke their own TTS are excluded.
        (Requirement 8.1, 8.2)
        """
        if not results:
            return "Done."

        # Filter out admin commands that already spoke via _confirm_and_execute
        speakable = [r for r in results if not r.get("_admin_handled")]

        if not speakable:
            # All commands were admin (already spoke their own TTS)
            return ""

        if len(speakable) == 1:
            msg = speakable[0].get("message", "Done.")
            return msg[:150]

        succeeded = sum(1 for r in speakable if r.get("success"))
        failed = len(speakable) - succeeded

        if failed == 0:
            summary = f"{succeeded} commands completed successfully."
        else:
            # Include the failed action name for context
            failed_actions = [r["action"] for r in speakable if not r.get("success")]
            fail_text = ", ".join(failed_actions[:3])
            summary = f"{succeeded} done, {failed} failed ({fail_text})."

        # Enforce ≤150 char limit
        return summary[:150]

    # ── music commands ───────────────────────────────────────────────────

    async def _resolve_and_play(self, guild: discord.Guild, song: str, member: discord.Member) -> str:
        """Search for a song and enqueue it via the existing player system.

        Returns a TTS response that reflects the actual playback state:
        "Now Playing: {title} by {artist}" when the first track actually starts,
        otherwise "Added {title} to the queue."
        """
        state = player.get_state(guild.id)
        voice_channel = member.voice.channel if member.voice else None
        if voice_channel is None:
            return "You need to be in a voice channel."

        player_obj = state.get("player")
        if not player_obj or not player_obj.connected:
            player_obj = await player.connect_player(voice_channel)
            state["player"] = player_obj

        provider = state.get("source_provider", "youtube")
        tracks = await Playable.search(song, source=_source_for(provider))
        if not tracks:
            return "I couldn't find that song."

        info = player._track_entry(tracks[0], provider)
        was_playing = player_obj.playing or player_obj.paused
        await player.add_track(state, guild.id, info)
        p = player.get_player(guild.id)
        started = False
        if p and p.connected and not p.playing and not p.paused:
            await player._play_next_from_queue(guild.id)
            # Reflect whether the first track actually started playing.
            # _play_next_from_queue pops the just-added entry and calls play();
            # give the player a moment to transition into the playing state.
            started = await self._wait_until_playing(p)
        if started and not was_playing:
            artist = info.get("author") or "Unknown Artist"
            return f"Now Playing: {info['title']} by {artist}"
        return f"Added {info['title']} to the queue."

    async def _wait_until_playing(self, p) -> bool:
        """Wait briefly for the player to start playing a track.

        Returns True if playback actually started, False otherwise.
        """
        for _ in range(10):  # up to ~0.5s
            if p.playing and not p.paused:
                return True
            if p.paused:
                return False
            await asyncio.sleep(0.05)
        return bool(p.playing and not p.paused)

    async def _execute_music(
        self, session: VoiceCommandSession, guild: discord.Guild, member: discord.Member
    ) -> None:
        """Execute a music command."""
        intent = session.intent or {}
        sub = intent.get("subcommand", "")
        args = intent.get("args", {})
        state = player.get_state(guild.id)
        player_obj = player.get_player(guild.id)

        if sub in ("play", "add"):
            song = args.get("song", "")
            if not song:
                await self._speak(session, guild, "Please specify a song to play.")
                return
            response = await self._resolve_and_play(guild, song, member)
            await self._speak(session, guild, response)

        elif sub == "skip":
            if player_obj and (player_obj.playing or player_obj.paused):
                await player_obj.stop()
                await self._speak(session, guild, "Skipped.")
            else:
                await self._speak(session, guild, "Nothing to skip.")

        elif sub == "pause":
            if player_obj and player_obj.playing:
                await player_obj.pause(True)
                await self._speak(session, guild, "Paused.")
            else:
                await self._speak(session, guild, "Nothing playing.")

        elif sub in ("resume", "start"):
            if player_obj and player_obj.paused:
                await player_obj.pause(False)
                await self._speak(session, guild, "Resumed.")
            else:
                await self._speak(session, guild, "Nothing paused.")

        elif sub == "stop":
            state["persist_enabled"] = False
            player.clear_queue(state)
            if player_obj and (player_obj.playing or player_obj.paused):
                await player_obj.stop()
            state["current"] = None
            await self._speak(session, guild, "Stopped and cleared.")

        elif sub in ("queue", "list"):
            q_len = len(state["queue"])
            if q_len == 0:
                await self._speak(session, guild, "Queue is empty.")
            else:
                await self._speak(session, guild, f"Queue has {q_len} tracks.")

        elif sub == "shuffle":
            player.shuffle_queue(state)
            player.persist(guild.id)
            await self._speak(session, guild, "Queue shuffled.")

        elif sub == "remove":
            index = args.get("index")
            if index is None:
                await self._speak(session, guild, "Please specify a track number.")
                return
            removed = player.remove_from_queue(state, index - 1)
            if removed is None:
                await self._speak(session, guild, "Invalid track number.")
            else:
                player.persist(guild.id)
                await self._speak(session, guild, "Removed that track from the queue.")

        elif sub == "repeat":
            mode = args.get("mode", "")
            if mode in ("on", "single", "one"):
                player.set_repeat(state, "single")
                await self._speak(session, guild, "Repeat single track.")
            elif mode in ("all", "queue"):
                player.set_repeat(state, "queue")
                await self._speak(session, guild, "Repeat entire queue.")
            else:
                player.set_repeat(state, "off")
                await self._speak(session, guild, "Repeat off.")

        elif sub == "join":
            if member.voice:
                player_obj = await player.connect_player(member.voice.channel)
                state["player"] = player_obj
                state["voice_channel"] = member.voice.channel
                await self._speak(session, guild, f"Joined {member.voice.channel.name}.")
            else:
                await self._speak(session, guild, "You need to be in a voice channel.")

        elif sub == "leave":
            if player_obj and player_obj.connected:
                player.clear_queue(state)
                state["current"] = None
                await player_obj.disconnect()
                await self._speak(session, guild, "Left the voice channel.")
            else:
                await self._speak(session, guild, "Not in a voice channel.")

        else:
            await self._speak(session, guild, "I don't understand that music command.")

    # ── admin commands ───────────────────────────────────────────────────

    async def _execute_admin(
        self, session: VoiceCommandSession, guild: discord.Guild, member: discord.Member
    ) -> None:
        """Execute an admin command with permission check and confirmation."""
        # Permission check
        if not member.guild_permissions.administrator and not member.guild_permissions.moderate_members:
            await self._speak(session, guild, "You need administrator permission for that.")
            return

        intent = session.intent or {}
        sub = intent.get("subcommand", "")

        # Parse target user
        target = _parse_target_user(session.transcript, guild)

        if sub == "mute":
            if target is None:
                await self._speak(session, guild, "Please specify a user to mute.")
                return
            duration = _parse_duration(session.transcript)
            dur_text = f" for {duration} minutes" if duration else ""
            await self._confirm_and_execute(
                session, guild, member,
                f"mute {target.display_name}{dur_text}",
                lambda: self._do_mute(target, duration),
            )

        elif sub == "kick":
            if target is None:
                await self._speak(session, guild, "Please specify a user to kick.")
                return
            await self._confirm_and_execute(
                session, guild, member,
                f"kick {target.display_name}",
                lambda: self._do_kick(target),
            )

        elif sub == "ban":
            if target is None:
                await self._speak(session, guild, "Please specify a user to ban.")
                return
            await self._confirm_and_execute(
                session, guild, member,
                f"ban {target.display_name}",
                lambda: self._do_ban(target),
            )

        elif sub == "timeout":
            if target is None:
                await self._speak(session, guild, "Please specify a user.")
                return
            duration = _parse_duration(session.transcript) or 10
            await self._confirm_and_execute(
                session, guild, member,
                f"timeout {target.display_name} for {duration} minutes",
                lambda: self._do_timeout(target, duration),
            )

        elif sub == "ticket":
            await self._do_ticket(session, guild, member)

        elif sub == "revoke":
            if target is None:
                await self._speak(session, guild, "Please specify a user.")
                return
            await self._confirm_and_execute(
                session, guild, member,
                f"revoke {target.display_name}",
                lambda: self._do_revoke(target, guild),
            )

        elif sub in ("restart", "shutdown"):
            # Destructive — require verbal confirmation.
            label = "restart the bot" if sub == "restart" else "shut down the bot"
            await self._confirm_and_execute(
                session, guild, member,
                label,
                lambda: self._do_shutdown(sub),
            )

        else:
            await self._speak(session, guild, "I don't understand that admin command.")

    async def _confirm_and_execute(
        self,
        session: VoiceCommandSession,
        guild: discord.Guild,
        member: discord.Member,
        action_text: str,
        action,
    ) -> None:
        """Ask for verbal confirmation, then execute.

        Waits up to 15 seconds for the user's spoken confirmation
        (Requirement 10.3). Uses silence-based capture (via
        ``detect_silence``) rather than a hard-coded frame slice, and
        resets session state on both the confirm and cancel branches.
        """
        session.member = member
        await self._speak(
            session, guild,
            f"You requested to {action_text}. Say confirm to proceed, or cancel to abort.",
        )

        # Wait for confirmation
        session.state = VoiceCommandSession.CONFIRM
        session.confirm_action = {"action_text": action_text, "action": action}

        # 15-second deadline for the confirmation utterance (Requirement 10.3).
        try:
            confirmed, outcome = await asyncio.wait_for(
                self._wait_for_confirmation(session, guild, action),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            await self._speak(session, guild, "Action cancelled due to timeout.")
            self._reset_after_confirm(session)
            return

        if outcome == "cancelled":
            await self._speak(session, guild, "Cancelled.")
            self._reset_after_confirm(session)
            return
        # outcome == "confirmed" (or "failed")
        if outcome == "failed":
            await self._speak(session, guild, "Action failed.")
            self._reset_after_confirm(session)
            return

    async def _wait_for_confirmation(
        self,
        session: VoiceCommandSession,
        guild: discord.Guild,
        action,
    ) -> tuple[bool, str]:
        """Poll for the user's confirmation utterance.

        Returns ``(True, "confirmed" | "cancelled" | "failed")`` once the
        user has spoken a confirm/cancel word, or after an action executes.
        """
        ssrc = session.wake_ssrc
        if ssrc is None:
            return True, "cancelled"

        # Capture only NEW audio after the confirmation prompt — not the
        # original command utterance already in the ring buffer.
        source0 = self.pipeline._sources.get(ssrc)
        start_idx = source0.frame_count if source0 is not None else 0
        while True:
            await asyncio.sleep(0.2)
            source = self.pipeline._sources.get(ssrc)
            if source is None:
                continue

            recent = source.get_latest(3200)
            if len(recent) == 0:
                continue
            rms = np.sqrt(np.mean(recent.astype(np.float32) ** 2))
            if rms < 500:  # silence — nothing to transcribe yet
                continue

            # Speech detected — capture from the wake point and transcribe.
            pcm = self.pipeline.capture_speech_since(ssrc, start_idx)
            if len(pcm) < 1600:  # too short
                continue
            transcript, _ = self.stt.transcribe_with_detection(pcm)
            transcript = transcript.strip().lower()

            if any(w in transcript for w in CONFIRM_WORDS):
                await self._speak(session, guild, "Confirmed.")
                try:
                    await action()
                except Exception as exc:
                    log.error("Admin action failed: %s", exc)
                    return True, "failed"
                return True, "confirmed"
            elif any(w in transcript for w in CANCEL_WORDS):
                return True, "cancelled"
            else:
                await self._speak(session, guild, "I didn't understand. Please say confirm or cancel.")
                continue

    def _reset_after_confirm(self, session: VoiceCommandSession) -> None:
        """Reset pipeline/session state after a confirm/cancel branch."""
        ssrc = session.wake_ssrc
        if ssrc is not None:
            self.pipeline.reset_ssrc(ssrc)
        session.confirm_action = None
        session.state = VoiceCommandSession.IDLE

    async def _do_mute(self, target: discord.Member, duration: int | None) -> None:
        """Mute a user."""
        if duration:
            await target.timeout(duration * 60, reason="HelloDJ voice command")
        else:
            await target.edit(mute=True, reason="HelloDJ voice command")
        await self._log_moderation(target, "mute", duration)

    async def _do_kick(self, target: discord.Member) -> None:
        """Kick a user."""
        await target.kick(reason="HelloDJ voice command")
        await self._log_moderation(target, "kick", None)

    async def _do_ban(self, target: discord.Member) -> None:
        """Ban a user."""
        await target.ban(reason="HelloDJ voice command")
        await self._log_moderation(target, "ban", None)

    async def _do_timeout(self, target: discord.Member, duration: int) -> None:
        """Timeout a user."""
        await target.timeout(duration * 60, reason="HelloDJ voice command")
        await self._log_moderation(target, "timeout", duration)

    async def _do_revoke(self, target: discord.Member, guild: discord.Guild) -> None:
        """Revoke bot access (add to blacklist)."""
        import blacklist as _blacklist
        gid = guild.id
        if gid not in _blacklist.blacklist:
            _blacklist.blacklist[gid] = []
        if target.id not in _blacklist.blacklist[gid]:
            _blacklist.blacklist[gid].append(target.id)
        await self._log_moderation(target, "revoke", None)

    async def _do_shutdown(self, sub: str) -> None:
        """Execute a destructive restart/shutdown.

        Mirrors the admin cog's ``restart`` (``os._exit(42)``) and ``kill``
        (``sys.exit(0)``) slash commands.
        """
        import os
        import sys
        log.info("Voice admin shutdown triggered: %s", sub)
        if sub == "restart":
            os._exit(42)
        else:  # shutdown
            sys.exit(0)

    async def _do_ticket(
        self, session: VoiceCommandSession, guild: discord.Guild, member: discord.Member
    ) -> None:
        """Create a ticket."""
        transcript = session.transcript
        # Extract reason after "ticket" keyword
        ticket_idx = transcript.lower().index("ticket")
        reason = transcript[ticket_idx + 6:].strip()
        if not reason:
            reason = "No reason specified."
        await self._create_ticket(session, guild, member, None, reason)

    async def _log_moderation(
        self,
        target: discord.Member,
        action: str,
        duration: int | None,
    ) -> None:
        """Create a moderation log/ticket entry after a major admin action.

        Includes the triggering moderator's mention, the target user's mention,
        the action taken, and the reason, and mentions the ticket-handler role.
        """
        # Resolve the guild from the target's voice state.
        guild = target.guild
        # The triggering moderator is whoever issued the command — the bot can't
        # know it from the target alone, so we look it up from active sessions.
        session = self._session_for_guild(guild.id)
        if session is None:
            return

        reason = "HelloDJ voice command"
        if action == "mute" and duration:
            reason = f"muted for {duration} minutes"
        elif action == "timeout" and duration:
            reason = f"timed out for {duration} minutes"

        await self._create_ticket(
            session, guild, session.member,
            target, f"{action}: {reason}",
        )

    def _session_for_guild(self, guild_id: int) -> VoiceCommandSession | None:
        """Return the first active session for a guild, if any."""
        prefix = f"{guild_id}:"
        for key, sess in self._sessions.items():
            if key.startswith(prefix):
                return sess
        return None

    async def _create_ticket(
        self,
        session: VoiceCommandSession,
        guild: discord.Guild,
        moderator: discord.Member | None,
        target: discord.Member | None,
        reason: str,
    ) -> None:
        """Post a ticket / moderation log entry mentioning the ticket-handler role."""
        moderator = moderator or guild.get_member(session.user_id)
        mod_text = moderator.mention if moderator else "Unknown moderator"
        target_text = target.mention if target else "Unknown user"

        # Find a support/admin role to mention
        support_role = discord.utils.get(guild.roles, name="Support")
        if support_role is None:
            support_role = discord.utils.get(guild.roles, name="Admin")

        # Find a channel to post the ticket in (use the first text channel)
        text_channel = None
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                text_channel = channel
                break

        mention = support_role.mention if support_role else "@support"
        if text_channel:
            await text_channel.send(
                f"🎫 **Action by {mod_text}**\nTarget: {target_text}\n"
                f"Action: {reason}\n{mention} please review."
            )
            await self._speak(
                session, guild,
                f"Done. {support_role.name if support_role else 'support'} has been notified.",
            )
        else:
            await self._speak(session, guild, "Done. An admin has been notified.")

    # ── general queries ──────────────────────────────────────────────────

    async def _execute_general(
        self, session: VoiceCommandSession, guild: discord.Guild, member: discord.Member
    ) -> None:
        """Execute a general query via LLM + MCP."""
        response = await self.query.handle_query(session.transcript)
        await self._speak(session, guild, response)

    # ── TTS helper ───────────────────────────────────────────────────────

    async def _speak(
        self, session: VoiceCommandSession, guild: discord.Guild, text: str
    ) -> None:
        """Generate TTS and play through the voice channel."""
        if not text:
            return

        session.state = VoiceCommandSession.RESPONDING

        # Get voice client
        state = player.get_state(guild.id)
        player_obj = state.get("player")

        if not player_obj or not player_obj.connected:
            log.warning("No voice client to speak through")
            session.state = VoiceCommandSession.IDLE
            return

        # Pause music
        was_playing = player_obj.playing and not player_obj.paused
        if was_playing:
            await player_obj.pause(True)

        # Generate TTS PCM
        result = self.tts.synthesize(text)
        if result is None:
            log.warning("TTS synthesis failed for: %s", text[:60])
            if was_playing:
                await player_obj.pause(False)
            session.state = VoiceCommandSession.IDLE
            return

        pcm, sample_rate = result

        # Play through voice client. play_pcm re-raises on TTS send failure so
        # this caller learns the TTS failed; the try/finally still resumes music
        # so the pause→speak→resume flow stays intact.
        tts_player = TTSPLayer(guild.id, player_obj)
        try:
            await tts_player.play_pcm(pcm, sample_rate)
        finally:
            # Resume music
            if was_playing:
                await player_obj.pause(False)

        session.state = VoiceCommandSession.IDLE


def _source_for(provider: str):
    """Map a source provider string to a wavelink source (TrackSource or search-prefix string)."""
    from wavelink import TrackSource
    return {
        "youtube": TrackSource.YouTube,
        "youtube_music": TrackSource.YouTubeMusic,
        "soundcloud": TrackSource.SoundCloud,
        "spotify": "spsearch",
    }.get(provider, TrackSource.YouTube)
