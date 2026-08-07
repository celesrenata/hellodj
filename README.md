# Hello DJ

A voice-activated Discord music bot with a custom "Hello DJ" wake word.

## Project Structure

```
hellodj/
├── bot/          # Discord music bot (wavelink + Lavalink)
├── training/     # Custom wake word model training pipeline
└── docker-compose.yml
```

## Bot (`bot/`)

A Discord music bot built with discord.py and wavelink 3.5, backed by Lavalink for audio playback.

**Features:**
- Slash commands: `/play`, `/queue`, `/skip`, `/pause`, `/resume`, `/stop`
- Playlist management: `/playlist create`, `/playlist play`, etc.
- Audio filters: bassboost, nightcore, 8D, custom EQ
- Autoplay with genre-based recommendations
- Session persistence and auto-resume after restarts
- Paginated queue and now-playing progress bar

### Quick Start

```bash
cd bot/
cp .env.example .env
# Edit .env with your Discord bot token and API keys

# Run with Docker Compose (from repo root):
docker compose up -d
```

See `bot/.env.example` for all configuration options.

## Training (`training/`)

Custom "Hello DJ" wake word model training using openWakeWord and piper-sample-generator.

- Generates 200k positive + 200k negative training clips
- Exports ONNX model for deployment on Raspberry Pi (goblin nodes)
- Requires GPU with CUDA (trained on RTX 4070 Ti SUPER)

See `training/README.md` for setup and usage.

## Deployment

```bash
# Start Lavalink + Bot containers
docker compose up -d

# Or run the bot directly (requires a Lavalink instance):
cd bot/
pip install -r requirements.txt
python bot.py
```

## License

MIT
