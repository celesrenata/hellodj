"""HelloDJ — Switchable voice-connect debug layer.

Diagnosis layer for the recurring `ChannelTimeoutException` on /play. It answers
the decisive question: after the bot sends op-4 (voice join), do the bot's OWN
VOICE_STATE_UPDATE / VOICE_SERVER_UPDATE events arrive on the gateway?

- If they NEVER arrive  -> Discord is silently dropping the join, almost always
  because the bot lacks per-channel `Connect` (or `Speak`) on the exact voice
  channel. The guild-level check (guild.me.guild_permissions) cannot see
  channel overwrites, so it wrongly reports "holds all".
- If they arrive but wavelink's session_id/token/endpoint stay empty -> a
  registration-timing race (event discarded because no voice client was
  registered at arrival time).

Enabled/disabled via env `HELLODJ_VOICE_DEBUG` (default "1" = on, so the live
diagnosis is active). Set to "0" to turn the layer off after diagnosis.
"""

import logging
import os

log = logging.getLogger(__name__)

ENABLED = os.getenv("HELLODJ_VOICE_DEBUG", "1") == "1"

# Which guilds the bot's own voice events are being tracked for (guild_id).
_tracked_guilds: set[int] = set()


def is_enabled() -> bool:
    return ENABLED


def log_per_channel_perms(guild, channel, label: str = "") -> None:
    """Log the ACTUAL per-channel permissions for the bot on ``channel``.

    Uses ``channel.permissions_for(guild.me)`` (channel-level, honors overwrites)
    — NOT ``guild.me.guild_permissions`` (guild-level only, ignores overwrites).
    This is the check the prior diagnostic was missing.
    """
    if not ENABLED:
        return
    try:
        me = guild.me if guild is not None else None
        if me is None:
            log.info(
                "VOICE_DEBUG[%s] per-channel perms: guild.me is None (cannot check)",
                label,
            )
            return
        perms = channel.permissions_for(me) if channel is not None else None
        if perms is None:
            log.info(
                "VOICE_DEBUG[%s] per-channel perms: channel=%s perms unavailable",
                label, channel,
            )
            return
        log.info(
            "VOICE_DEBUG[%s] PER-CHANNEL perms channel=%s member=%s "
            "connect=%s speak=%s view_channel=%s manage_channels=%s "
            "move_members=%s use_voice_activity=%s",
            label, getattr(channel, "id", None), me,
            getattr(perms, "connect", False),
            getattr(perms, "speak", False),
            getattr(perms, "view_channel", False),
            getattr(perms, "manage_channels", False),
            getattr(perms, "move_members", False),
            getattr(perms, "use_voice_activity", False),
        )
        if not (perms.connect and perms.speak):
            log.error(
                "VOICE_DEBUG[%s] *** per-channel Connect/Speak DENIED on %s — "
                "Discord silently drops the voice join; this is the likely "
                "ChannelTimeoutException cause. Guild-level check reports "
                "'holds all' but the channel overwrite denies it.",
                label, getattr(channel, "name", channel),
            )
    except Exception:
        log.exception("VOICE_DEBUG[%s] could not dump per-channel perms", label)


def log_op4_send(guild, channel) -> None:
    """Log that the bot is about to send opcode 4 (voice join) to the gateway."""
    if not ENABLED:
        return
    try:
        gid = guild.id if guild is not None else None
        cid = getattr(channel, "id", None)
        log.info(
            "VOICE_DEBUG op-4 SEND guild_id=%s channel_id=%s channel=%s",
            gid, cid, getattr(channel, "name", channel),
        )
    except Exception:
        log.exception("VOICE_DEBUG could not log op-4 send")


def install_raw_listeners(bot) -> None:
    """Install a socket raw-listener that logs the bot's OWN voice events.

    Registers a `on_socket_raw_receive` listener that captures every
    VOICE_STATE_UPDATE / VOICE_SERVER_UPDATE payload and logs:
      - whether it is the bot's own voice state (user_id == bot id)
      - whether ConnectionState._get_voice_client(guild.id) is registered at
        arrival time (the registration-race discriminator)
      - the session_id / token / endpoint fields that wavelink needs
    """
    if not ENABLED:
        return

    async def _on_raw(data):
        try:
            t = data.get("t")
            if t not in ("VOICE_STATE_UPDATE", "VOICE_SERVER_UPDATE"):
                return
            d = data.get("d") or {}
            gid = d.get("guild_id")
            if gid is None:
                return
            state = getattr(bot, "_connection_state", None) or getattr(
                bot, "http", None
            )
            # Resolve ConnectionState to check the registered voice client.
            conn = None
            for attr in ("_connection_state", "_state", "_connection"):
                if hasattr(bot, attr):
                    conn = getattr(bot, attr)
                    break
            vc = None
            if conn is not None and hasattr(conn, "_get_voice_client"):
                try:
                    vc = conn._get_voice_client(int(gid))
                except Exception:
                    vc = None
            if t == "VOICE_STATE_UPDATE":
                uid = d.get("user_id")
                is_self = uid == str(bot.user.id) if bot.user else False
                channel_id = d.get("channel_id")
                session_id = d.get("session_id")
                log.info(
                    "VOICE_DEBUG raw VOICE_STATE_UPDATE guild_id=%s user_id=%s "
                    "self=%s channel_id=%s session_id=%r voice_client_registered=%s",
                    gid, uid, is_self, channel_id, session_id, vc is not None,
                )
            elif t == "VOICE_SERVER_UPDATE":
                token = d.get("token")
                endpoint = d.get("endpoint")
                log.info(
                    "VOICE_DEBUG raw VOICE_SERVER_UPDATE guild_id=%s "
                    "token=%r endpoint=%r voice_client_registered=%s",
                    gid, token, endpoint, vc is not None,
                )
        except Exception:
            log.exception("VOICE_DEBUG raw-listener error")

    try:
        bot.add_listener(_on_raw, "on_socket_raw_receive")
        log.info("VOICE_DEBUG raw voice-event listener installed (HELLODJ_VOICE_DEBUG=%s)", ENABLED)
    except Exception:
        log.exception("VOICE_DEBUG could not install raw listener")
