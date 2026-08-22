"""HelloDJ — Interactive Equalizer View (Discord UI).

A visual 10-band equalizer controlled entirely via buttons and a preset dropdown.
Integrates into the Now Playing panel via the filters dropdown.
"""

import discord
import wavelink

import player

# ── Band definitions ─────────────────────────────────────────────────────────

BAND_LABELS = ["25", "63", "160", "400", "630", "1k6", "2k5", "4k", "10k", "16k"]
BAND_COUNT = 10  # We expose 10 bands (wavelink supports 15, we use the first 10)

GAIN_MIN = -0.25
GAIN_MAX = 1.0
GAIN_STEP = 0.05

# ── Presets ──────────────────────────────────────────────────────────────────

PRESETS = {
    "flat": [0.0] * BAND_COUNT,
    "bass_boost": [0.6, 0.45, 0.35, 0.2, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0],
    "treble_boost": [0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.2, 0.35, 0.45, 0.5],
    "v_shape": [0.5, 0.35, 0.15, 0.0, -0.1, -0.1, 0.0, 0.15, 0.35, 0.5],
    "mid_scoop": [-0.1, 0.0, 0.2, 0.4, 0.5, 0.5, 0.4, 0.2, 0.0, -0.1],
    "vocal_boost": [0.0, 0.0, 0.1, 0.3, 0.4, 0.4, 0.3, 0.1, 0.0, 0.0],
    "loudness": [0.4, 0.3, 0.1, 0.0, -0.1, -0.1, 0.0, 0.1, 0.3, 0.4],
}

PRESET_NAMES = {
    "flat": "Flat (Reset)",
    "bass_boost": "Bass Boost",
    "treble_boost": "Treble Boost",
    "v_shape": "V-Shape",
    "mid_scoop": "Mid Scoop",
    "vocal_boost": "Vocal Boost",
    "loudness": "Loudness",
}


# ── Visualization ────────────────────────────────────────────────────────────

BLOCKS = " ▁▂▃▄▅▆▇█"


def _gain_to_block(gain: float) -> str:
    """Convert a gain value (-0.25 to 1.0) to a Unicode block character."""
    normalized = (gain - GAIN_MIN) / (GAIN_MAX - GAIN_MIN)
    idx = int(normalized * (len(BLOCKS) - 1))
    idx = max(0, min(len(BLOCKS) - 1, idx))
    return BLOCKS[idx]


def _build_eq_display(gains: list[float], selected_band: int) -> str:
    """Build the text visualization of the EQ.

    Discord code blocks on embeds fit ~32 monospace chars before wrapping.
    Labels need 3 chars each + 1 space separator = 39 chars for 10 bands.
    We use tight spacing: no separator, just right-pad to 3 chars.
    Block chars (▁▂▃) render ~1.5x wide in Discord mono, so bars get 2-char slots.
    """
    # Bars and indicator in a code block (box), labels as small text below
    # Use 2-space gaps — fits in the code block without wrapping
    bars = "  ".join(_gain_to_block(g) for g in gains)
    indicator = "  ".join("▲" if i == selected_band else "·" for i in range(BAND_COUNT))

    # Labels: -# small text below the code block
    viz_labels = ["25", "63", "160", "400", "630", "1k6", "2k5", "4k", "10k", "16k"]
    labels = "  ".join(viz_labels)

    return f"```{bars}\n{indicator}```\n-# {labels}"


def _build_eq_embed(gains: list[float], selected_band: int) -> discord.Embed:
    """Build the full equalizer embed."""
    band_label = BAND_LABELS[selected_band]
    gain_val = gains[selected_band]
    sign = "+" if gain_val >= 0 else ""

    embed = discord.Embed(
        title="🎛️ HelloDJ — Equalizer",
        description=f"**Band {selected_band + 1}: {band_label} Hz** `[{sign}{gain_val:.2f}]`\n"
                    + _build_eq_display(gains, selected_band),
        colour=discord.Colour.orange(),
    )
    embed.set_footer(text="◀▶ select band • ▲▼ adjust gain • Presets dropdown for quick setup")
    return embed


# ── View ─────────────────────────────────────────────────────────────────────

class EqualizerView(discord.ui.View):
    """Interactive 10-band equalizer with buttons and preset dropdown."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.selected_band = 0

        # Load current gains from state, or start flat
        state = player.get_state(guild_id)
        saved_gains = (state.get("filters") or {}).get("equalizer", {}).get("gains")
        if saved_gains and len(saved_gains) >= BAND_COUNT:
            self.gains = [float(g) for g in saved_gains[:BAND_COUNT]]
        else:
            self.gains = [0.0] * BAND_COUNT

        # Add preset dropdown
        preset_select = discord.ui.Select(
            placeholder="Presets…",
            options=[
                discord.SelectOption(label=name, value=key)
                for key, name in PRESET_NAMES.items()
            ],
            row=0,
        )
        preset_select.callback = self._on_preset
        self.add_item(preset_select)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=1)
    async def prev_band(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected_band = (self.selected_band - 1) % BAND_COUNT
        await interaction.response.edit_message(embed=_build_eq_embed(self.gains, self.selected_band), view=self)

    @discord.ui.button(label="▲", style=discord.ButtonStyle.success, row=1)
    async def gain_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.gains[self.selected_band] = min(GAIN_MAX, self.gains[self.selected_band] + GAIN_STEP)
        await self._apply_and_respond(interaction)

    @discord.ui.button(label="▼", style=discord.ButtonStyle.danger, row=1)
    async def gain_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.gains[self.selected_band] = max(GAIN_MIN, self.gains[self.selected_band] - GAIN_STEP)
        await self._apply_and_respond(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_band(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected_band = (self.selected_band + 1) % BAND_COUNT
        await interaction.response.edit_message(embed=_build_eq_embed(self.gains, self.selected_band), view=self)

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.secondary, row=1)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.gains = [0.0] * BAND_COUNT
        await self._apply_and_respond(interaction)

    async def _on_preset(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        preset = PRESETS.get(value, [0.0] * BAND_COUNT)
        self.gains = list(preset)
        await self._apply_and_respond(interaction)

    async def _apply_and_respond(self, interaction: discord.Interaction):
        """Apply the EQ to the player and update the embed."""
        player_obj = player.get_player(self.guild_id)
        if player_obj and player_obj.connected:
            filters = player_obj.filters
            bands = [{"band": i, "gain": g} for i, g in enumerate(self.gains)]
            filters.equalizer.set(bands=bands)
            await player_obj.set_filters(filters)

            # Persist to state
            state = player.get_state(self.guild_id)
            if "filters" not in state:
                state["filters"] = {}
            state["filters"]["equalizer"] = {"gains": list(self.gains)}
            player.persist(self.guild_id)

        await interaction.response.edit_message(embed=_build_eq_embed(self.gains, self.selected_band), view=self)

    async def on_timeout(self):
        pass  # Just let the view expire silently
