"""REST client for Lavalink audio pipe endpoints."""
import aiohttp
import logging
import wavelink

from config import cfg

log = logging.getLogger(__name__)


class LavalinkPipeClient:
    """REST client for Lavalink's audio pipe management endpoints.

    Uses the same Lavalink connection details as the main wavelink pool.
    Requires a connected wavelink node to obtain the session ID.
    """

    def __init__(self):
        self._host = cfg("lavalink.host", "losingtime.dpaste.org")
        self._port = cfg.int("lavalink.port", 2124)
        self._password = cfg("lavalink.password", "SleepingOnTrains")
        self._base_url = f"http://{self._host}:{self._port}"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._password}

    def _get_session_id(self) -> str | None:
        """Get the session ID from the first connected wavelink node."""
        nodes = wavelink.Pool.nodes
        if not nodes:
            return None
        for node in nodes.values():
            if node.status == wavelink.NodeStatus.CONNECTED:
                return node.session_id
        return None

    async def enable_pipe(self, guild_id: int, pipe_path: str) -> bool:
        """Enable audio pipe for a guild's player.

        POSTs the socket path to Lavalink, which opens the FIFO for PCM writing.

        Returns True on success, False on failure.
        """
        session_id = self._get_session_id()
        if not session_id:
            log.error("No connected Lavalink node — cannot enable audio pipe")
            return False

        url = f"{self._base_url}/v4/sessions/{session_id}/players/{guild_id}/audiopipe"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={"socketPath": pipe_path},
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        log.info("Audio pipe enabled for guild %d: %s", guild_id, pipe_path)
                        return True
                    else:
                        body = await resp.text()
                        log.error("Failed to enable audio pipe for guild %d: %d %s", guild_id, resp.status, body)
                        return False
        except Exception as exc:
            log.error("Error enabling audio pipe for guild %d: %s", guild_id, exc)
            return False

    async def disable_pipe(self, guild_id: int) -> bool:
        """Disable audio pipe for a guild's player.

        DELETEs the pipe endpoint, closing the FIFO on Lavalink's side.

        Returns True on success, False on failure.
        """
        session_id = self._get_session_id()
        if not session_id:
            log.error("No connected Lavalink node — cannot disable audio pipe")
            return False

        url = f"{self._base_url}/v4/sessions/{session_id}/players/{guild_id}/audiopipe"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    url,
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        log.info("Audio pipe disabled for guild %d", guild_id)
                        return True
                    else:
                        body = await resp.text()
                        log.error("Failed to disable audio pipe for guild %d: %d %s", guild_id, resp.status, body)
                        return False
        except Exception as exc:
            log.error("Error disabling audio pipe for guild %d: %s", guild_id, exc)
            return False

    async def is_player_active(self, guild_id: int) -> bool:
        """Check if Lavalink has an active (playing) player for a guild.

        Queries the standard Lavalink player REST endpoint. Returns True
        if a player exists and has a track loaded, False otherwise.
        """
        session_id = self._get_session_id()
        if not session_id:
            return False

        url = f"{self._base_url}/v4/sessions/{session_id}/players/{guild_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Player exists — check if it has a track
                        track = data.get("track")
                        return track is not None and track.get("encoded") is not None
                    return False
        except Exception:
            return False
