"""Shared blacklist module — imported by bot.py and cogs/admin.py."""

# Guild → list of user IDs
blacklist: dict[int, list[int]] = {}


def is_blacklisted(guild_id: int, user_id: int) -> bool:
    return user_id in blacklist.get(guild_id, [])
