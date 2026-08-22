# Commands Reference

## Playback Commands

| Command | Description |
|---------|-------------|
| `/play song:<query>` | Search for a song and play it |
| `/play link:<url>` | Play a direct URL (YouTube, Spotify, Tidal, SoundCloud) |
| `/play album:<query>` | Search and play an album |
| `/play playlist:<url>` | Play a playlist URL |
| `/play mode:video <url>` | Stream a video via Discord Activity |
| `/play mode:music_video <query>` | Find and play a music video |
| `/join` | Join your current voice channel |
| `/leave` | Disconnect from voice |
| `/skip` | Skip to the next track |
| `/previous` | Go back to the previous track |
| `/pause` | Pause playback |
| `/resume` | Resume playback |
| `/stop` | Stop playback and clear queue |
| `/nowplaying` | Show the currently playing track |
| `/queue` | Display the current queue |
| `/shuffle` | Randomize the queue order |
| `/repeat off\|single\|queue` | Set repeat mode |
| `/remove <position>` | Remove a track from the queue |
| `/move <from> <to>` | Move a track in the queue |
| `/seek <position>` | Seek to a position in the current track |

## Source & Provider

| Command | Description |
|---------|-------------|
| `/source youtube\|spotify\|tidal\|soundcloud\|youtube_music` | Set preferred music source |

## Audio Filters

| Command | Description |
|---------|-------------|
| `/filter bass_boost` | Apply bass boost |
| `/filter nightcore` | Speed up + pitch up |
| `/filter slowed` | Slow down + pitch down |
| `/filter vaporwave` | Slow + reverb aesthetic |
| `/filter 8d` | Rotating spatial audio |
| `/filter reset` | Remove all filters |
| `/tune` | Toggle enhanced audio processing |
| `/crossfade <seconds>` | Set crossfade between tracks (0 = off) |
| `/equalizer` | Open 15-band parametric EQ |

## Playlists

| Command | Description |
|---------|-------------|
| `/playlist save <name>` | Save current queue as playlist |
| `/playlist load <name>` | Load a saved playlist |
| `/playlist list` | List all saved playlists |
| `/playlist delete <name>` | Delete a playlist |

## Radio

| Command | Description |
|---------|-------------|
| `/radio <station>` | Play a radio station (pre-defined streams) |
| `/radio list` | List available stations |

## Autoplay

| Command | Description |
|---------|-------------|
| `/autoplay enable\|disable` | Toggle auto-play when queue empties |
| `/autoplay genres <genre1> <genre2> ...` | Set genre seeds for recommendations |

## Voice Activation

| Command | Description |
|---------|-------------|
| `/voice enable\|disable` | Toggle voice activation for this guild |
| `/wakeword on\|off` | Toggle wake word listening (Manage Guild) |
| `/voice_status` | Show voice activation status |

## Voice Commands (spoken after "Hello DJ")

| Voice Command | Action |
|--------------|--------|
| "Play [song] on [source]" | Play a song |
| "Skip" | Skip current track |
| "Pause" / "Resume" | Toggle playback |
| "Stop" | Stop playback |
| "Shuffle" | Shuffle the queue |
| "What's the weather in [city]?" | Get weather info |
| "What's in the news?" | Get news headlines |
| "How's [TICKER] doing?" | Get stock price |

## Admin Commands

| Command | Permission | Description |
|---------|-----------|-------------|
| `/activate <key>` | None | Activate HelloDJ for this server |
| `/blacklist <user>` | Manage Guild | Block a user from using the bot |
| `/unblacklist <user>` | Manage Guild | Restore a user's access |
| `/allowlist <user>` | Manage Guild | Allow a user (in allow_all mode) |
| `/mode restrictive\|allow_all` | Manage Guild | Set guild restriction mode |

## HelloDJ Admin Panel

| Command | Permission | Description |
|---------|-----------|-------------|
| `/hellodj ping` | None | Bot latency + Lavalink + instance health |
| `/hellodj status` | None | Active sessions in this guild |
| `/hellodj settings` | Manage Guild | Guild configuration display |
| `/hellodj block artist <name>` | Manage Guild | Block an artist |
| `/hellodj block track <url>` | Manage Guild | Block a track URL |
| `/hellodj block domain <pattern>` | Manage Guild | Block a domain pattern |
| `/hellodj block keyword <word>` | Manage Guild | Block a keyword |
| `/hellodj block list` | Manage Guild | List all block rules |
| `/hellodj unblock <rule_id>` | Manage Guild | Remove a block rule |
| `/hellodj ban <user>` | Manage Guild | Ban user from playback |
| `/hellodj unban <user>` | Manage Guild | Unban user |
| `/hellodj banlist` | Manage Guild | List banned users |
| `/hellodj instances` | Manage Guild | View bot instance assignments |

## Info & Utility

| Command | Description |
|---------|-------------|
| `/lyrics` | Display lyrics for current track |
| `/whosampled` | Show sample/interpolation info |
| `/artist` | Display artist information |
| `/help` | Show help information |
| `/sleep <duration>` | Set a sleep timer (auto-stop after duration) |

## Stream Commands

| Command | Description |
|---------|-------------|
| `/stream <tidal_url>` | Stream Tidal music video to file |
