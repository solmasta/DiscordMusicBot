"""/stations: let anyone find radio stations from where they live and tune the bot to one."""
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

import directory as dirmod
from picker import FOOTER, GuidedPicker, PanelView, clean, station_embed  # noqa: F401  (station_embed is re-exported)

log = logging.getLogger("stations")

HOME_GUILD_ID = os.getenv("GUILD_ID")
ONLINE = dirmod.ONLINE


class Stations(commands.Cog):
    stations = app_commands.Group(name="stations", description="Find radio stations from your area and play them", guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Re-attach the permanent panel buttons so panels posted before a restart keep working.
        self.bot.add_view(PanelView(self))

    # ---- helpers
    async def _gate(self, interaction: discord.Interaction) -> bool:
        """False (after replying) when this command can't run here."""
        if HOME_GUILD_ID and str(interaction.guild_id) == HOME_GUILD_ID:
            await interaction.response.send_message(
                "This server runs the Crüe FM station rotation, so `/stations` is for other servers. Add the bot to a test server to try it.",
                ephemeral=True,
            )
            return False
        if not self.bot.directory.stations:
            await interaction.response.send_message("The station list is still loading, try again in a few seconds.", ephemeral=True)
            return False
        return True

    async def open_picker(self, interaction: discord.Interaction, state: str | None = None, city: str | None = None):
        """Open the guided picker privately for whoever asked, optionally already on a state or city."""
        if not await self._gate(interaction):
            return
        area = self.bot.public.area_for(interaction.user.id)
        picker = GuidedPicker(self, interaction.user.id, saved_area=area)
        if state:
            picker.show_stations(state, city) if city else picker.show_areas(state)
        await interaction.response.send_message(embed=picker.embed(), view=picker, ephemeral=True)

    async def show_now(self, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        player = self.bot.public.players.get(interaction.guild_id)
        if not player:
            await interaction.response.send_message("Nothing is playing. Use `/stations browse` or the **Find a station** button.", ephemeral=True)
            return
        await interaction.response.send_message(embed=station_embed(player.station, "Now playing"))

    async def stop_radio(self, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        player = self.bot.public.players.get(interaction.guild_id)
        if not player:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if not self.bot.public.may_control(interaction.user, player):
            await interaction.response.send_message("Someone else is controlling the radio here.", ephemeral=True)
            return
        await self.bot.public.stop(interaction.guild, f"stopped by {interaction.user}")
        await interaction.response.send_message("⏹ Stopped. Thanks for listening!")

    # ---- autocomplete (for people who prefer typing)
    async def state_autocomplete(self, interaction: discord.Interaction, current: str):
        d, cur = self.bot.directory, current.lower().strip()
        rows = d.states()
        if not cur:
            rows = sorted(rows, key=lambda r: r[2], reverse=True)
        out = [
            app_commands.Choice(name=f"{name} ({ab}) · {n} stations", value=ab)
            for ab, name, n in rows if not cur or cur in name.lower() or cur == ab.lower()
        ]
        online = sum(1 for s in d.stations if not s.state)
        if not cur or cur in "nationwide online":
            out.insert(0, app_commands.Choice(name=f"Nationwide / online-only · {online} stations", value=ONLINE))
        return out[:25]

    async def city_autocomplete(self, interaction: discord.Interaction, current: str):
        state = getattr(interaction.namespace, "state", None)
        if not state or state == ONLINE:
            return []
        cur = current.lower().strip()
        return [
            app_commands.Choice(name=f"{city} · {n} stations", value=city)
            for city, n in self.bot.directory.cities(state) if not cur or cur in city.lower()
        ][:25]

    # ---- commands
    @stations.command(name="browse", description="Pick your state and find a station, no typing needed")
    @app_commands.describe(state="Skip ahead to a state (optional, start typing)", city="A major city or market (optional)",
                           genre="Narrow by genre (optional)")
    @app_commands.autocomplete(state=state_autocomplete, city=city_autocomplete)
    @app_commands.choices(genre=[app_commands.Choice(name=g, value=g) for g in dirmod.GENRES])
    async def browse(self, interaction: discord.Interaction, state: str | None = None, city: str | None = None,
                     genre: app_commands.Choice[str] | None = None):
        if state is None and city is None and genre is None:
            await self.open_picker(interaction)
            return
        if not await self._gate(interaction):
            return
        if state is None:
            await interaction.response.send_message("Pick a state as well, or run `/stations browse` on its own for the guided menu.", ephemeral=True)
            return
        if state != ONLINE and state not in dirmod.STATES:
            await interaction.response.send_message("Pick a state from the list as you type.", ephemeral=True)
            return
        found = self.bot.directory.browse(state, city, genre.value if genre else None)
        if found and state != ONLINE and city is None and genre is None:
            await self.open_picker(interaction, state=state)    # a state alone: let them choose a city next
            return
        where = "Nationwide / online" if state == ONLINE else (f"{city}, {state}" if city else dirmod.STATES[state])
        if not found:
            hint = " Try removing the genre or the city." if (genre or city) else ""
            await interaction.response.send_message(f"No stations found for **{clean(where)}**.{hint}", ephemeral=True)
            return
        picker = GuidedPicker(self, interaction.user.id, saved_area=self.bot.public.area_for(interaction.user.id))
        picker.show_stations(state, city)
        if genre:
            picker.genre = genre.value
            picker.stations = [s for s in picker.base if genre.value in s.genres]
            picker._render()
        await interaction.response.send_message(embed=picker.embed(), view=picker, ephemeral=True)

    @stations.command(name="search", description="Search by station name, call letters, frequency or genre")
    @app_commands.describe(query="e.g. WLS, 94.7, classic rock, jazz", state="Limit to one state (optional)")
    @app_commands.autocomplete(state=state_autocomplete)
    async def search(self, interaction: discord.Interaction, query: app_commands.Range[str, 2, 60], state: str | None = None):
        if not await self._gate(interaction):
            return
        found = self.bot.directory.search(query, state if state in dirmod.STATES else None)
        if not found:
            await interaction.response.send_message(f"No stations matched **{clean(query)}**. Try fewer words, or `/stations browse`.", ephemeral=True)
            return
        picker = GuidedPicker(self, interaction.user.id, saved_area=self.bot.public.area_for(interaction.user.id))
        picker.show_results(found, f"Results for “{query}”")
        await interaction.response.send_message(embed=picker.embed(), view=picker, ephemeral=True)

    @stations.command(name="now", description="Show what's playing in this server")
    async def now(self, interaction: discord.Interaction):
        await self.show_now(interaction)

    @stations.command(name="stop", description="Stop the radio and leave the voice channel")
    async def stop(self, interaction: discord.Interaction):
        await self.stop_radio(interaction)

    @stations.command(name="volume", description="Set the radio volume for this server (1-100)")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 100]):
        if not await self._gate(interaction):
            return
        player = self.bot.public.players.get(interaction.guild_id)
        if not player:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if not self.bot.public.may_control(interaction.user, player):
            await interaction.response.send_message("Someone else is controlling the radio here.", ephemeral=True)
            return
        self.bot.public.set_volume(interaction.guild_id, level / 100)
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")

    @stations.command(name="panel", description="Post a permanent Find-a-station button panel in this channel (managers only)")
    @app_commands.default_permissions(manage_guild=True)
    async def panel(self, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You need the **Manage Server** permission to post the panel.", ephemeral=True)
            return
        embed = discord.Embed(
            title="📻 Radio from where you live",
            description="Tap **Find a station**, pick your state and city, then choose a station.\n"
                        "Join a voice channel first and I'll play it for you.",
            color=discord.Color.red(),
        )
        embed.set_footer(text=FOOTER)
        await interaction.response.send_message(embed=embed, view=PanelView(self))

    @stations.command(name="forget", description="Forget the area I remembered for you")
    async def forget(self, interaction: discord.Interaction):
        had = await self.bot.public.forget_area(interaction.user.id)
        await interaction.response.send_message(
            "Done. I've forgotten your area." if had else "I hadn't saved an area for you.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Stations(bot))
