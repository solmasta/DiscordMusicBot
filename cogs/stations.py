"""/stations: let anyone find radio stations from where they live and tune the bot to one."""
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

import directory as dirmod

log = logging.getLogger("stations")

HOME_GUILD_ID = os.getenv("GUILD_ID")
PAGE = 25   # Discord allows at most 25 options in one select menu
FOOTER = "Station list: Radio Browser (radio-browser.info) · Streams belong to their stations"
ONLINE = dirmod.ONLINE


def clean(text: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text or ""))


def station_embed(station, headline: str) -> discord.Embed:
    embed = discord.Embed(title=f"📻 {headline}", description=f"**{clean(station.name)}**", color=discord.Color.red())
    if station.place:
        embed.add_field(name="Location", value=clean(station.place), inline=True)
    if station.genres:
        embed.add_field(name="Genre", value=", ".join(sorted(station.genres))[:200], inline=True)
    quality = " · ".join(x for x in (station.codec, f"{station.bitrate} kbps" if station.bitrate else "") if x)
    if quality:
        embed.add_field(name="Stream", value=quality, inline=True)
    if station.homepage.startswith(("http://", "https://")):
        embed.add_field(name="Website", value=station.homepage[:200], inline=False)
    embed.set_footer(text=FOOTER)
    return embed


class StationPicker(discord.ui.View):
    """Ephemeral results list: a select menu of up to 25 stations per page, plus paging."""

    def __init__(self, cog: "Stations", user_id: int, stations: list, title: str):
        super().__init__(timeout=600)
        self.cog, self.user_id, self.stations, self.title = cog, user_id, stations, title
        self.page = 0
        self._render()

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.stations) // PAGE))

    def current(self) -> list:
        return self.stations[self.page * PAGE:(self.page + 1) * PAGE]

    def embed(self) -> discord.Embed:
        lines = []
        for i, s in enumerate(self.current(), start=self.page * PAGE + 1):
            where = f" · {clean(s.place)}" if s.place else ""
            lines.append(f"`{i:>2}.` {clean(s.name)[:60]}{where}")
        embed = discord.Embed(title=f"📻 {self.title}", description="\n".join(lines), color=discord.Color.red())
        embed.set_footer(text=f"{len(self.stations)} stations · page {self.page + 1}/{self.pages} · {FOOTER}")
        return embed

    def _render(self):
        self.clear_items()
        options = []
        for s in self.current():
            bits = [s.place] + ([", ".join(sorted(s.genres)[:2])] if s.genres else []) + ([f"{s.bitrate}k"] if s.bitrate else [])
            options.append(discord.SelectOption(
                label=s.name[:100] or "Station", value=s.uuid[:100], description=" · ".join(b for b in bits if b)[:100] or None,
            ))
        select = discord.ui.Select(placeholder="Pick a station to play in your voice channel…", options=options)
        select.callback = self._picked
        self.add_item(select)
        prev_b = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
        next_b = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, disabled=self.page >= self.pages - 1)
        close_b = discord.ui.Button(label="Close", style=discord.ButtonStyle.danger)
        prev_b.callback, next_b.callback, close_b.callback = self._prev, self._next, self._close
        for b in (prev_b, next_b, close_b):
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This list belongs to someone else. Use `/stations browse` to get your own.", ephemeral=True)
            return False
        return True

    async def _flip(self, interaction: discord.Interaction, delta: int):
        self.page = min(self.pages - 1, max(0, self.page + delta))
        self._render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _prev(self, interaction):
        await self._flip(interaction, -1)

    async def _next(self, interaction):
        await self._flip(interaction, +1)

    async def _close(self, interaction):
        self.stop()
        await interaction.response.edit_message(content="Closed.", embed=None, view=None)

    async def _picked(self, interaction: discord.Interaction):
        uuid = interaction.data["values"][0]
        station = self.cog.bot.directory.by_uuid.get(uuid)
        await interaction.response.defer(ephemeral=True)
        if station is None:
            await interaction.followup.send("That station is no longer in the list. Please search again.", ephemeral=True)
            return
        ok, message = await self.cog.bot.public.tune(interaction.user, station, interaction.channel_id)
        if not ok:
            await interaction.followup.send(f"❌ {message}", ephemeral=True)
            return
        embed = station_embed(station, "Now playing")
        embed.set_author(name=f"Tuned by {interaction.user.display_name}")
        try:
            await interaction.channel.send(embed=embed)
            await interaction.followup.send("✅ Tuned in! Use `/stations stop` to stop.", ephemeral=True)
        except (discord.HTTPException, AttributeError):
            await interaction.followup.send(embed=embed, ephemeral=True)


class Stations(commands.Cog):
    stations = app_commands.Group(name="stations", description="Find radio stations from your area and play them", guild_only=True)

    def __init__(self, bot):
        self.bot = bot

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

    # ---- autocomplete
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
    @stations.command(name="browse", description="Find stations by state, city and genre")
    @app_commands.describe(state="Your state (start typing)", city="A major city or market (optional)", genre="Narrow by genre (optional)")
    @app_commands.autocomplete(state=state_autocomplete, city=city_autocomplete)
    @app_commands.choices(genre=[app_commands.Choice(name=g, value=g) for g in dirmod.GENRES])
    async def browse(self, interaction: discord.Interaction, state: str, city: str | None = None,
                     genre: app_commands.Choice[str] | None = None):
        if not await self._gate(interaction):
            return
        if state != ONLINE and state not in dirmod.STATES:
            await interaction.response.send_message("Pick a state from the list as you type.", ephemeral=True)
            return
        found = self.bot.directory.browse(state, city, genre.value if genre else None)
        where = "Nationwide / online" if state == ONLINE else (f"{city}, {state}" if city else dirmod.STATES[state])
        title = f"{where}" + (f" · {genre.value}" if genre else "")
        if not found:
            hint = " Try removing the genre or the city." if (genre or city) else ""
            await interaction.response.send_message(f"No stations found for **{clean(title)}**.{hint}", ephemeral=True)
            return
        view = StationPicker(self, interaction.user.id, found, title)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

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
        view = StationPicker(self, interaction.user.id, found, f"Results for “{query}”")
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @stations.command(name="now", description="Show what's playing in this server")
    async def now(self, interaction: discord.Interaction):
        if not await self._gate(interaction):
            return
        player = self.bot.public.players.get(interaction.guild_id)
        if not player:
            await interaction.response.send_message("Nothing is playing. Use `/stations browse` to find a station.", ephemeral=True)
            return
        await interaction.response.send_message(embed=station_embed(player.station, "Now playing"))

    @stations.command(name="stop", description="Stop the radio and leave the voice channel")
    async def stop(self, interaction: discord.Interaction):
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


async def setup(bot):
    await bot.add_cog(Stations(bot))
