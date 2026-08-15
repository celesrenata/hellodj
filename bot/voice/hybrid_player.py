"""Hybrid voice player combining wavelink (Lavalink music) with voice receive.

wavelink.Player is a ``discord.VoiceProtocol`` that only forwards voice state to
Lavalink — it cannot receive incoming audio. Vanilla ``discord.VoiceClient``
also has no ``receive()``. The only supported receive API is
``discord.ext.voice_recv.VoiceRecvClient`` (extends ``discord.VoiceClient``).

This module provides ``HybridPlayer``: a single VoiceProtocol that is a
wavelink Player (so the existing music system works unchanged) AND a
VoiceRecvClient (so it can receive Opus frames and send TTS via ``play``).

Only one VoiceProtocol is allowed per guild, so the hybrid is the only way to
have both Lavalink playback and incoming-audio receive on one connection.
"""

import logging

import discord
import wavelink

log = logging.getLogger(__name__)

try:
    import discord.ext.voice_recv as _voice_recv
    _VOICE_RECV_AVAILABLE = True
except ImportError:
    _voice_recv = None  # type: ignore[assignment]
    _VOICE_RECV_AVAILABLE = False


def _build_hybrid():
    """Build and return the HybridPlayer class (requires voice_recv)."""
    from discord.ext import voice_recv

    class HybridPlayer(wavelink.Player, voice_recv.VoiceRecvClient):
        """wavelink Player + voice_recv VoiceRecvClient combined into one class."""

        def __init__(
            self,
            client: discord.Client = discord.utils.MISSING,
            channel: discord.abc.Connectable = discord.utils.MISSING,
            *,
            nodes: list | None = None,
            **kwargs,
        ) -> None:
            # Initialise the wavelink Player half.
            wavelink.Player.__init__(self, client, channel, nodes=nodes, **kwargs)

            # VoiceRecvClient state is set up lazily to avoid clashing with
            # wavelink's connect()/on_voice_*_update flow.
            self._recv_initialised = False
            self._sink = None
            self._sink_after = None
            self._recv_listeners = []

        def _init_recv(self) -> None:
            """Initialise the VoiceRecvClient receive machinery once.

            The wavelink ``Player.__init__`` chain already ran ``VoiceRecvClient.__init__``
            via the cooperative ``super().__init__`` MRO (HybridPlayer → Player(wavelink)
            → VoiceRecvClient → VoiceClient → VoiceProtocol), so ``self._connection``,
            ``self._reader``, ``self._ssrc_to_id``, ``self._id_to_ssrc`` and
            ``self._event_listeners`` are already set. Re-calling ``VoiceRecvClient.__init__``
            here would recreate ``self._connection`` (fresh ``VoiceConnectionState`` + a new
            ``SocketReader`` thread) and re-set ``self.client``/``self.channel`` — a
            state-clobber/thread-leak latent bug. Only ensure the voice_recv-specific attrs
            exist, and never touch ``_connection``.
            """
            if self._recv_initialised:
                return
            # voice_recv-specific attrs are set by the wavelink init chain; only
            # backfill them defensively if that chain did not run (e.g. exotic MRO).
            if not hasattr(self, "_reader"):
                self._reader = None
            if not hasattr(self, "_ssrc_to_id"):
                self._ssrc_to_id = {}
            if not hasattr(self, "_id_to_ssrc"):
                self._id_to_ssrc = {}
            if not hasattr(self, "_event_listeners"):
                self._event_listeners = {}
            self._recv_initialised = True

        def listen(self, sink, *, after=None) -> None:
            """Start receiving audio into the given AudioSink."""
            if not isinstance(sink, voice_recv.AudioSink):
                raise TypeError("sink must be an AudioSink instance.")
            self._init_recv()
            if self.is_listening():
                raise discord.ClientException("Already receiving audio.")
            voice_recv.VoiceRecvClient.listen(self, sink, after=after)

        def is_listening(self) -> bool:
            if not self._recv_initialised:
                return False
            return voice_recv.VoiceRecvClient.is_listening(self)

        def stop_listening(self) -> None:
            if not self._recv_initialised:
                return
            voice_recv.VoiceRecvClient.stop_listening(self)

        def get_speaking(self, member) -> bool:
            if not self._recv_initialised:
                return False
            return voice_recv.VoiceRecvClient.get_speaking(self, member)

        def add_listener(self, func) -> None:
            self._recv_listeners.append(func)

        def remove_listener(self, func) -> None:
            try:
                self._recv_listeners.remove(func)
            except ValueError:
                pass

        def _handle_receive(self, user, data) -> None:
            """Called by the registered sink for each incoming audio packet."""
            for listener in self._recv_listeners:
                try:
                    listener(user, data)
                except Exception:
                    log.exception("voice receive listener error")

        async def disconnect(self, **kwargs) -> None:
            if self._recv_initialised:
                try:
                    self.stop_listening()
                except Exception:
                    pass
            await wavelink.Player.disconnect(self, **kwargs)

        def _invalidate(self) -> None:
            if self._recv_initialised:
                try:
                    self.stop_listening()
                except Exception:
                    pass
            wavelink.Player._invalidate(self)

    return HybridPlayer


HybridPlayer = None
if _VOICE_RECV_AVAILABLE:
    HybridPlayer = _build_hybrid()


async def connect_hybrid(channel: discord.abc.Connectable):
    """Connect a HybridPlayer to a voice channel.

    Usage mirrors ``wavelink.Player.connect`` / ``VoiceChannel.connect(cls=...)``.
    Returns None if the voice_recv extension is unavailable.
    """
    if HybridPlayer is None:
        log.warning(
            "discord-ext-voice-recv is not installed — voice activation receive "
            "is disabled. Add 'discord-ext-voice-recv' to requirements.txt"
        )
        return None
    return await channel.connect(cls=HybridPlayer)
