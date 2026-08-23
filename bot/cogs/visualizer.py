"""HelloDJ — Visualizer cog: the /visualizer command group.

Provides the ``/visualizer`` command group for configuring the per-guild
visualizer engine, managing per-engine settings, and saving/loading presets.

Subcommands:
  /visualizer engine <engine>          — Set the visualizer engine
  /visualizer config <engine> <setting> <value>  — Set an engine config value
  /visualizer settings [engine]        — Show current engine config
  /visualizer preset save <name>       — Save current config as a named preset
  /visualizer preset load <name>       — Load a named preset
  /visualizer preset list              — List all presets (factory + user)
  /visualizer preset delete <name>     — Delete a user preset
  /visualizer projectm list-categories — List projectM preset categories
"""

from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import guild_settings
from video.visualizer_engines import get_available_engines
from video.visualizer_engines.config_schema import ENGINE_CONFIG_SCHEMAS, get_default_config
from video.visualizer_engines.factory_presets import (
    is_factory_preset,
    list_factory_presets,
)

log = logging.getLogger(__name__)

# projectM preset directory (bundled in Docker image)
PROJECTM_PRESET_DIR = Path("/app/data/presets/projectm")


# ---------------------------------------------------------------------------
# Subgroups
# ---------------------------------------------------------------------------


class PresetGroup(app_commands.Group):
    """Subcommand group for preset management: /visualizer preset ..."""

    def __init__(self) -> None:
        super().__init__(name="preset", description="Manage visualizer presets")


class ProjectMGroup(app_commands.Group):
    """Subcommand group for projectM-specific commands: /visualizer projectm ..."""

    def __init__(self) -> None:
        super().__init__(name="projectm", description="projectM engine management")


# ---------------------------------------------------------------------------
# Main command group
# ---------------------------------------------------------------------------


class VisualizerGroup(app_commands.Group):
    """Top-level /visualizer command group."""

    def __init__(self) -> None:
        super().__init__(name="visualizer", description="Configure the visualizer engine")


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class VisualizerCog(commands.Cog, name="Visualizer"):
    """Per-guild visualizer engine configuration, settings, and presets."""

    visualizer_group = VisualizerGroup()
    preset_group = PresetGroup()
    projectm_group = ProjectMGroup()

    # Attach subgroups to the main group
    visualizer_group.add_command(preset_group)
    visualizer_group.add_command(projectm_group)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ===================================================================
    # Autocomplete helpers
    # ===================================================================

    async def _engine_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return matching engine choices from get_available_engines().

        Only shows engines that are currently usable (GPU availability gated).
        """
        current_lower = current.casefold()
        available = get_available_engines()
        return [
            app_commands.Choice(name=engine, value=engine)
            for engine in sorted(available)
            if current_lower in engine.casefold()
        ][:25]

    async def _config_engine_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return engines that have configurable settings."""
        current_lower = current.casefold()
        return [
            app_commands.Choice(name=engine, value=engine)
            for engine in sorted(ENGINE_CONFIG_SCHEMAS.keys())
            if current_lower in engine.casefold()
        ][:25]

    async def _setting_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return setting names for the engine specified in the interaction."""
        # Get the engine value from the namespace
        engine = interaction.namespace.engine
        if not engine or engine not in ENGINE_CONFIG_SCHEMAS:
            return []

        current_lower = current.casefold()
        schema = ENGINE_CONFIG_SCHEMAS[engine]
        return [
            app_commands.Choice(name=setting, value=setting)
            for setting in sorted(schema.keys())
            if current_lower in setting.casefold()
        ][:25]

    async def _preset_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return preset names: user presets + factory presets."""
        guild_id = interaction.guild_id
        current_lower = current.casefold()

        choices: list[app_commands.Choice[str]] = []

        # User presets first
        user_presets = guild_settings.get_visualizer_presets(guild_id)
        for name in sorted(user_presets.keys()):
            if current_lower in name.casefold():
                choices.append(app_commands.Choice(name=f"⭐ {name}", value=name))

        # Factory presets
        factory = list_factory_presets()
        for name in sorted(factory.keys()):
            if current_lower in name.casefold():
                choices.append(app_commands.Choice(name=f"🏭 {name}", value=name))

        return choices[:25]

    async def _category_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Return projectM preset category folder names."""
        current_lower = current.casefold()
        categories = _get_projectm_categories()

        choices = [app_commands.Choice(name="all", value="all")]
        for cat_name in sorted(categories.keys()):
            if current_lower in cat_name.casefold():
                choices.append(
                    app_commands.Choice(
                        name=f"{cat_name} ({categories[cat_name]} presets)",
                        value=cat_name,
                    )
                )

        return choices[:25]

    # ===================================================================
    # /visualizer engine <engine>
    # ===================================================================

    @visualizer_group.command(name="engine", description="Set the visualizer engine for this server")
    @app_commands.describe(engine="Visualizer engine to use (e.g. dvd, projectm, off)")
    @app_commands.autocomplete(engine=_engine_autocomplete)
    async def engine(self, interaction: discord.Interaction, engine: str) -> None:
        """Set the visualizer engine for the current guild."""
        guild_id = interaction.guild_id

        try:
            guild_settings.set_visualizer_engine(guild_id, engine)
        except ValueError:
            valid = ", ".join(sorted(guild_settings.VALID_VISUALIZER_ENGINES))
            await interaction.response.send_message(
                f"❌ Invalid engine `{engine}`. Valid options: {valid}",
                ephemeral=True,
            )
            return

        # Build confirmation embed
        embed = discord.Embed(
            title="🎨 Visualizer Updated",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Engine", value=f"`{engine}`", inline=True)

        if engine == "off":
            embed.description = "The visualizer has been disabled."
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed.description = f"Visualizer engine set to **{engine}** for this server."

        # Launch Activity if not already running and user is in voice
        activity_url = None
        if interaction.user.voice:
            activity_url = await self._ensure_activity(interaction)

        if activity_url:
            install_url = "https://discord.com/oauth2/authorize?client_id=1534778518137995325"
            embed.add_field(
                name="Activity",
                value=f"[Join Activity]({activity_url}) • [Install Activity]({install_url})",
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ===================================================================
    # /visualizer config <engine> <setting> <value>
    # ===================================================================

    @visualizer_group.command(name="config", description="Set a visualizer engine configuration value")
    @app_commands.describe(
        engine="Engine to configure",
        setting="Setting name",
        value="New value for the setting",
    )
    @app_commands.autocomplete(engine=_config_engine_autocomplete, setting=_setting_autocomplete)
    async def config(
        self, interaction: discord.Interaction, engine: str, setting: str, value: str
    ) -> None:
        """Validate and store an engine configuration setting."""
        guild_id = interaction.guild_id

        try:
            guild_settings.set_visualizer_config(guild_id, engine, setting, value)
        except ValueError as exc:
            await interaction.response.send_message(
                f"❌ {exc}", ephemeral=True
            )
            return

        # Hot-reload: attempt to notify the active visualizer manager
        await self._try_hot_reload(guild_id, engine, setting)

        # Confirmation embed
        effective = guild_settings.get_visualizer_config(guild_id, engine)
        embed = discord.Embed(
            title="⚙️ Config Updated",
            description=f"**{engine}** • `{setting}` = `{effective.get(setting)}`",
            color=discord.Color.green(),
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ===================================================================
    # /visualizer settings [engine]
    # ===================================================================

    @visualizer_group.command(name="settings", description="Display current visualizer configuration")
    @app_commands.describe(engine="Engine to show settings for (default: current active engine)")
    @app_commands.autocomplete(engine=_config_engine_autocomplete)
    async def settings(self, interaction: discord.Interaction, engine: str | None = None) -> None:
        """Display the current configuration for an engine as an embed."""
        guild_id = interaction.guild_id

        # Default to the currently active engine
        if engine is None:
            engine = guild_settings.get_visualizer_engine(guild_id)

        if engine not in ENGINE_CONFIG_SCHEMAS:
            await interaction.response.send_message(
                f"❌ Engine `{engine}` has no configurable settings.",
                ephemeral=True,
            )
            return

        config = guild_settings.get_visualizer_config(guild_id, engine)
        schema = ENGINE_CONFIG_SCHEMAS[engine]

        embed = discord.Embed(
            title=f"⚙️ {engine} Settings",
            color=discord.Color.blue(),
        )

        for setting_name, setting_schema in schema.items():
            current_val = config.get(setting_name, setting_schema["default"])
            type_info = setting_schema["type"]

            # Build constraint info
            constraints = ""
            if type_info == "float" or type_info == "int":
                min_val = setting_schema.get("min", "—")
                max_val = setting_schema.get("max", "—")
                constraints = f" ({min_val}–{max_val})"
            elif type_info == "choice":
                choices = setting_schema.get("choices", [])
                constraints = f" [{', '.join(str(c) for c in choices)}]"
            elif type_info == "bool":
                constraints = " (true/false)"

            embed.add_field(
                name=setting_name,
                value=f"`{current_val}`{constraints}",
                inline=True,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ===================================================================
    # /visualizer preset save <name>
    # ===================================================================

    @preset_group.command(name="save", description="Save current engine + config as a named preset")
    @app_commands.describe(name="Name for the preset")
    async def preset_save(self, interaction: discord.Interaction, name: str) -> None:
        """Capture the current engine + config as a named preset."""
        guild_id = interaction.guild_id
        engine = guild_settings.get_visualizer_engine(guild_id)
        config = guild_settings.get_visualizer_config(guild_id, engine)

        preset_data = {"engine": engine, "config": config}
        guild_settings.save_visualizer_preset(guild_id, name, preset_data)

        embed = discord.Embed(
            title="💾 Preset Saved",
            description=f"Saved preset **{name}** (engine: `{engine}`)",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ===================================================================
    # /visualizer preset load <name>
    # ===================================================================

    @preset_group.command(name="load", description="Load a named preset (engine + config)")
    @app_commands.describe(name="Preset name to load")
    @app_commands.autocomplete(name=_preset_name_autocomplete)
    async def preset_load(self, interaction: discord.Interaction, name: str) -> None:
        """Apply a saved or factory preset (engine + config)."""
        guild_id = interaction.guild_id
        preset = guild_settings.load_visualizer_preset(guild_id, name)

        if preset is None:
            await interaction.response.send_message(
                f"❌ Preset `{name}` not found.", ephemeral=True
            )
            return

        preset_engine = preset.get("engine", "dvd")
        preset_config = preset.get("config", {})

        # Check engine availability
        if preset_engine not in guild_settings.VALID_VISUALIZER_ENGINES:
            valid = ", ".join(sorted(guild_settings.VALID_VISUALIZER_ENGINES))
            await interaction.response.send_message(
                f"❌ Engine `{preset_engine}` from preset is unavailable. "
                f"Available engines: {valid}",
                ephemeral=True,
            )
            return

        # Apply engine
        guild_settings.set_visualizer_engine(guild_id, preset_engine)

        # Apply config settings
        for setting, value in preset_config.items():
            try:
                guild_settings.set_visualizer_config(guild_id, preset_engine, setting, value)
            except ValueError:
                pass  # Skip invalid settings silently (schema may have changed)

        # Hot-reload
        await self._try_hot_reload(guild_id, preset_engine, None)

        source = "🏭 factory" if preset.get("factory") else "⭐ user"
        embed = discord.Embed(
            title="📂 Preset Loaded",
            description=f"Applied **{name}** ({source}) — engine: `{preset_engine}`",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ===================================================================
    # /visualizer preset list
    # ===================================================================

    @preset_group.command(name="list", description="List all presets (factory + user)")
    async def preset_list(self, interaction: discord.Interaction) -> None:
        """Show all available presets (factory + user) with engine type."""
        guild_id = interaction.guild_id

        embed = discord.Embed(
            title="📋 Visualizer Presets",
            color=discord.Color.blue(),
        )

        # User presets
        user_presets = guild_settings.get_visualizer_presets(guild_id)
        if user_presets:
            lines = []
            for name, data in sorted(user_presets.items()):
                eng = data.get("engine", "?")
                lines.append(f"⭐ **{name}** — `{eng}`")
            embed.add_field(
                name="User Presets",
                value="\n".join(lines[:10]) or "None",
                inline=False,
            )

        # Factory presets grouped by engine
        factory = list_factory_presets()
        engines_seen: dict[str, list[str]] = {}
        for name, data in factory.items():
            eng = data.get("engine", "?")
            engines_seen.setdefault(eng, []).append(name)

        for eng in sorted(engines_seen.keys()):
            names = sorted(engines_seen[eng])
            embed.add_field(
                name=f"🏭 {eng}",
                value=", ".join(f"`{n}`" for n in names),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ===================================================================
    # /visualizer preset delete <name>
    # ===================================================================

    @preset_group.command(name="delete", description="Delete a user-saved preset")
    @app_commands.describe(name="Preset name to delete")
    @app_commands.autocomplete(name=_preset_name_autocomplete)
    async def preset_delete(self, interaction: discord.Interaction, name: str) -> None:
        """Remove a user preset. Errors on factory presets."""
        guild_id = interaction.guild_id

        try:
            guild_settings.delete_visualizer_preset(guild_id, name)
        except ValueError as exc:
            await interaction.response.send_message(
                f"❌ {exc}", ephemeral=True
            )
            return
        except KeyError as exc:
            await interaction.response.send_message(
                f"❌ {exc}", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🗑️ Preset Deleted",
            description=f"Removed preset **{name}**.",
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ===================================================================
    # /visualizer projectm list-categories
    # ===================================================================

    @projectm_group.command(
        name="list-categories",
        description="List available projectM preset categories",
    )
    async def projectm_list_categories(self, interaction: discord.Interaction) -> None:
        """Show available projectM preset category folders with preset counts."""
        categories = _get_projectm_categories()

        if not categories:
            await interaction.response.send_message(
                "❌ No projectM preset categories found. "
                "The preset directory may not be available.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🎵 projectM Preset Categories",
            description="Use `/visualizer config projectm preset_category <name>` to select.",
            color=discord.Color.purple(),
        )

        total = 0
        for cat_name in sorted(categories.keys()):
            count = categories[cat_name]
            total += count
            embed.add_field(name=cat_name, value=f"{count} presets", inline=True)

        embed.set_footer(text=f"Total: {total} presets across {len(categories)} categories")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ===================================================================
    # Internal helpers
    # ===================================================================

    async def _try_hot_reload(
        self, guild_id: int, engine: str, setting: str | None
    ) -> None:
        """Attempt to hot-reload config for the active engine.

        Notifies the VisualizerManager if the engine is currently active,
        so it can apply settings without restarting the HLS pipeline.
        """
        video_cog = self.bot.get_cog("Video")
        if video_cog is None:
            return

        # Access the visualizer registry if available
        registry = getattr(video_cog, "_visualizer_registry", None)
        if registry is None:
            return

        manager = registry.get(guild_id)
        if manager is None:
            return

        # If the manager has a hot_reload method and is active, call it
        if hasattr(manager, "hot_reload_config"):
            try:
                await manager.hot_reload_config(engine, setting)
            except Exception:
                log.debug(
                    "Hot-reload failed for guild %d engine %s setting %s",
                    guild_id,
                    engine,
                    setting,
                    exc_info=True,
                )

    async def _ensure_activity(self, interaction: discord.Interaction) -> str | None:
        """Ensure a Discord Activity is running for the user's voice channel.

        Returns the Activity invite URL, or None if launch failed.
        """
        guild_id = interaction.guild_id
        voice_channel = interaction.user.voice.channel

        video_cog = self.bot.get_cog("Video")
        if video_cog is None:
            return None

        # Check if Activity already exists for this guild
        for key, url in video_cog._activity_urls.items():
            if key[0] == guild_id:
                return url

        # No Activity running — launch one
        if video_cog._launcher is None:
            return None

        try:
            application_id = self.bot.user.id
            invite_data = await video_cog._launcher.launch(voice_channel.id, application_id)
            invite_code = invite_data.get("code", "")
            activity_url = f"https://discord.gg/{invite_code}" if invite_code else None
            if activity_url:
                video_cog._activity_urls[(guild_id, voice_channel.id)] = activity_url
            return activity_url
        except Exception as exc:
            log.warning("Failed to launch Activity for visualizer: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _get_projectm_categories() -> dict[str, int]:
    """Read projectM preset directory and return category names with counts.

    Returns:
        Dict mapping category folder name to number of .milk preset files.
    """
    categories: dict[str, int] = {}

    if not PROJECTM_PRESET_DIR.is_dir():
        return categories

    for entry in PROJECTM_PRESET_DIR.iterdir():
        if entry.is_dir():
            # Count .milk files in the category folder
            count = sum(1 for f in entry.iterdir() if f.suffix == ".milk")
            if count > 0:
                categories[entry.name] = count

    return categories


# ---------------------------------------------------------------------------
# Cog setup
# ---------------------------------------------------------------------------


async def setup(bot: commands.Bot) -> None:
    cog = VisualizerCog(bot)
    # Add the top-level group to the command tree
    bot.tree.add_command(cog.visualizer_group)
    await bot.add_cog(cog)


async def teardown(bot: commands.Bot) -> None:
    bot.tree.remove_command("visualizer")
