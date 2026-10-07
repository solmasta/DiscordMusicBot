"""Guided station picker. No typing needed: one private message that changes as you tap
(state -> area -> genre -> station), plus a search box and a permanent panel with buttons."""
import asyncio
import io
import math
import os

import discord

import directory as dirmod
import visuals

PAGE = 25   # Discord allows at most 25 options in one select menu
FOOTER = "Station list: Radio Browser (radio-browser.info) · Streams belong to their stations"
ALL = "*"
BRAND = discord.Color.from_rgb(230, 57, 70)
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.png")
GENRE_EMOJI = {
    "Rock": "🎸", "Classic Rock": "🎸", "Alternative": "🎧", "Metal": "🤘", "Pop / Top 40": "🎤",
    "Country": "🤠", "Hip Hop / R&B": "🎤", "Jazz": "🎷", "Classical": "🎻", "News / Talk": "🗞️",
    "Sports": "🏟️", "Christian / Gospel": "🙏", "Latin": "💃", "Oldies": "📼", "Electronic": "🎛️",
    "Blues / Folk": "🪕",
}


def clean(text: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text or ""))


def count(n: int) -> str:
    return f"{n:,} station{'' if n == 1 else 's'}"


def genre_emoji(station) -> str:
    for g in sorted(station.genres):
        if g in GENRE_EMOJI:
            return GENRE_EMOJI[g]
    return "📻"


def brand_file() -> discord.File | None:
    """The Crüe FM icon, attached so embeds can show it as a thumbnail."""
    try:
        return discord.File(ICON_PATH, filename="icon.png")
    except OSError:
        return None


_cards: dict[str, bytes] = {}


async def now_playing(station, headline: str, by: str | None = None) -> tuple[discord.Embed, discord.File | None]:
    """The 'now playing' message: an embed with a drawn station card (cached per station)."""
    embed = station_embed(station, headline)
    if by:
        embed.set_author(name=f"Tuned by {by}")
    key = station.uuid
    png = _cards.get(key)
    if png is None:
        quality = " · ".join(x for x in (station.codec, f"{station.bitrate} kbps" if station.bitrate else "") if x)
        try:
            png = await asyncio.to_thread(visuals.render_station_card, station.name, station.place or "", sorted(station.genres), quality)
        except Exception:
            return embed, None
        if len(_cards) >= 64:
            _cards.pop(next(iter(_cards)))
        _cards[key] = png
    embed = station_embed(station, headline, detailed=False)     # the card already shows place, genre and quality
    if by:
        embed.set_author(name=f"Tuned by {by}")
    embed.set_image(url="attachment://station.png")
    return embed, discord.File(io.BytesIO(png), filename="station.png")


def station_embed(station, headline: str, detailed: bool = True) -> discord.Embed:
    embed = discord.Embed(title=f"📻 {headline}", description=f"**{clean(station.name)}**", color=BRAND)
    if not detailed:
        if station.homepage.startswith(("http://", "https://")):
            embed.add_field(name="Website", value=station.homepage[:200], inline=False)
        embed.set_footer(text=FOOTER)
        return embed
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


def state_groups(rows: list[tuple[str, str, int]], parts: int = 3) -> list[list[tuple[str, str, int]]]:
    """Split the (code, name, count) state rows into a few alphabetical groups, each small enough for
    one select menu (Discord allows 25 options and there are 51 states)."""
    rows = sorted(rows, key=lambda r: r[1])
    size = max(1, math.ceil(len(rows) / parts))
    return [rows[i:i + size] for i in range(0, len(rows), size)]


class SearchModal(discord.ui.Modal, title="Search stations"):
    query = discord.ui.TextInput(label="Name, call letters, frequency or genre", placeholder="e.g. WLS, 94.7, classic rock",
                                 min_length=2, max_length=60)

    def __init__(self, picker: "GuidedPicker"):
        super().__init__()
        self.picker = picker

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.query).strip()
        found = self.picker.directory.search(text)
        if not found:
            await interaction.response.send_message(f"No stations matched **{clean(text)}**. Try fewer words.", ephemeral=True)
            return
        self.picker.show_results(found, f"Results for “{text}”")
        await interaction.response.edit_message(embed=self.picker.embed(), view=self.picker)


class GuidedPicker(discord.ui.View):
    """States, then area, then genre and station, all in one private message."""

    def __init__(self, cog, user_id: int, saved_area: tuple[str, str | None] | None = None):
        super().__init__(timeout=840)
        self.cog, self.user_id = cog, user_id
        self.directory = cog.bot.directory
        self.saved_area = saved_area
        self.screen = "states"
        self.state = self.city = self.genre = None
        self.base: list = []
        self.stations: list = []
        self.title = "Find a station"
        self.back_to = "states"
        self.page = 0
        self._render()

    # ---- navigation (each only changes state; callers re-render and edit the message)
    def show_states(self):
        self.screen, self.state, self.city, self.genre = "states", None, None, None
        self.base = self.stations = []
        self._render()

    def show_areas(self, state: str):
        self.state, self.city, self.genre = state, None, None
        if not self.directory.cities(state):
            self.show_stations(state, None)
            return
        self.screen = "areas"
        self._render()

    def show_stations(self, state: str | None, city: str | None):
        self.state, self.city, self.genre = state, city, None
        self.base = self.directory.browse(state, city)
        self.stations, self.page = list(self.base), 0
        self.title = self._breadcrumb()
        self.back_to = "areas" if state in dirmod.STATES and self.directory.cities(state) else "states"
        self.screen = "stations"
        self._render()

    def show_results(self, stations: list, title: str):
        """A ready-made list (search results, online-only stations)."""
        self.state = self.city = self.genre = None
        self.base, self.stations, self.page, self.title = list(stations), list(stations), 0, title
        self.back_to, self.screen = "states", "stations"
        self._render()

    def _breadcrumb(self) -> str:
        if self.state == dirmod.ONLINE:
            return "Nationwide / online"
        name = dirmod.STATES.get(self.state, "")
        return f"{self.city}, {self.state}" if self.city else name

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.stations) // PAGE))

    def current(self) -> list:
        return self.stations[self.page * PAGE:(self.page + 1) * PAGE]

    # ---- rendering
    def embed(self) -> discord.Embed:
        if self.screen == "states":
            lines = ["**Where are you listening from?** Pick your state below."]
            if self.saved_area:
                lines.append(f"📍 Last time: **{clean(self._area_label(self.saved_area))}**, use the green button for one tap.")
            lines.append("🎧 Join a voice channel first, then tap a station to play it.")
            embed = discord.Embed(title="📻 Find a station", description="\n".join(lines), color=BRAND)
            embed.set_thumbnail(url="attachment://icon.png")
            return embed
        if self.screen == "areas":
            name = dirmod.STATES[self.state]
            return discord.Embed(
                title=f"📻 {name}", color=BRAND,
                description=f"Pick your city or area, or choose **All of {name}**.",
            )
        lines = []
        for i, s in enumerate(self.current(), start=self.page * PAGE + 1):
            where = f" · {clean(s.place)}" if self._show_place(s) else ""
            lines.append(f"`{i:>2}.` {genre_emoji(s)} {clean(s.name)[:60]}{where}")
        title = self.title + (f" · {self.genre}" if self.genre else "")
        embed = discord.Embed(title=f"📻 {title}", description="\n".join(lines) or "No stations.", color=BRAND)
        embed.set_footer(text=f"{count(len(self.stations))} · page {self.page + 1}/{self.pages} · {FOOTER}")
        return embed

    def _show_place(self, s) -> bool:
        """Only show a station's location when it adds something: not inside a single city, and not
        when the station's own name already says it."""
        return bool(s.place) and not self.city and s.place.lower() not in s.name.lower()

    @staticmethod
    def _area_label(area: tuple[str, str | None]) -> str:
        state, city = area
        return f"{city}, {state}" if city else dirmod.STATES.get(state, state)

    def _button(self, label: str, callback, *, style=discord.ButtonStyle.secondary, emoji=None, disabled=False, row=None):
        b = discord.ui.Button(label=label, style=style, emoji=emoji, disabled=disabled, row=row)
        b.callback = callback
        self.add_item(b)

    def _select(self, placeholder: str, options: list, callback, row: int):
        sel = discord.ui.Select(placeholder=placeholder, options=options[:25], row=row)
        sel.callback = callback
        self.add_item(sel)

    def _render(self):
        self.clear_items()
        getattr(self, f"_render_{self.screen}")()

    def _render_states(self):
        rows = self.directory.states()
        for idx, group in enumerate(state_groups(rows)):
            options = [discord.SelectOption(label=name, value=ab, description=count(n)) for ab, name, n in group]
            self._select(f"States: {group[0][1]} – {group[-1][1]}", options, self._state_picked, row=idx)
        if self.saved_area and self.saved_area[0] in dirmod.STATES:
            self._button(self._area_label(self.saved_area)[:80], self._saved_clicked, style=discord.ButtonStyle.success, emoji="📍", row=3)
        self._button("Online & nationwide", self._online_clicked, emoji="🌐", row=3)
        self._button("Search", self._search_clicked, emoji="🔎", row=3)
        self._button("Close", self._close, style=discord.ButtonStyle.danger, row=3)

    def _render_areas(self):
        name = dirmod.STATES[self.state]
        total = len(self.directory.browse(self.state))
        options = [discord.SelectOption(label=f"All of {name}", value=ALL, description=count(total), emoji="🗺️")]
        options += [discord.SelectOption(label=city, value=city, description=count(n)) for city, n in self.directory.cities(self.state)[:24]]
        self._select("Choose your city or area…", options, self._area_picked, row=0)
        self._button("← States", self._back, row=1)
        self._button("Close", self._close, style=discord.ButtonStyle.danger, row=1)

    def _render_stations(self):
        row = 0
        counts: dict[str, int] = {}
        for s in self.base:
            for g in s.genres:
                counts[g] = counts.get(g, 0) + 1
        if counts:
            options = [discord.SelectOption(label="Any genre", value=ALL, description=count(len(self.base)), default=self.genre is None)]
            options += [
                discord.SelectOption(label=g, value=g, description=count(n), default=g == self.genre, emoji=GENRE_EMOJI.get(g))
                for g, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:24]
            ]
            self._select(f"Genre: {self.genre or 'Any'}", options, self._genre_picked, row=row)
            row += 1
        options = []
        for s in self.current():
            bits = ([s.place] if self._show_place(s) else []) + ([", ".join(sorted(s.genres)[:2])] if s.genres else []) + ([f"{s.bitrate}k"] if s.bitrate else [])
            options.append(discord.SelectOption(label=s.name[:100] or "Station", value=s.uuid[:100], emoji=genre_emoji(s),
                                                description=" · ".join(b for b in bits if b)[:100] or None))
        if options:
            self._select("Tap a station to play it in your voice channel…", options, self._station_picked, row=row)
            row += 1
        self._button("◀ Prev", self._prev, disabled=self.page == 0, row=row)
        self._button("Next ▶", self._next, disabled=self.page >= self.pages - 1, row=row)
        self._button("← Back", self._back, row=row)
        self._button("Close", self._close, style=discord.ButtonStyle.danger, row=row)

    # ---- interactions
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This menu belongs to someone else. Use `/stations browse` to get your own.", ephemeral=True)
            return False
        return True

    async def _redraw(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _state_picked(self, interaction: discord.Interaction):
        self.show_areas(interaction.data["values"][0])
        await self._redraw(interaction)

    async def _area_picked(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        self.show_stations(self.state, None if value == ALL else value)
        await self._redraw(interaction)

    async def _genre_picked(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        self.genre = None if value == ALL else value
        self.stations = [s for s in self.base if not self.genre or self.genre in s.genres]
        self.page = 0
        self._render()
        await self._redraw(interaction)

    async def _saved_clicked(self, interaction: discord.Interaction):
        state, city = self.saved_area
        self.show_stations(state, city)
        await self._redraw(interaction)

    async def _online_clicked(self, interaction: discord.Interaction):
        self.show_stations(dirmod.ONLINE, None)
        await self._redraw(interaction)

    async def _search_clicked(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SearchModal(self))

    async def _flip(self, interaction: discord.Interaction, delta: int):
        self.page = min(self.pages - 1, max(0, self.page + delta))
        self._render()
        await self._redraw(interaction)

    async def _prev(self, interaction):
        await self._flip(interaction, -1)

    async def _next(self, interaction):
        await self._flip(interaction, +1)

    async def _back(self, interaction: discord.Interaction):
        if self.screen == "stations" and self.back_to == "areas" and self.state in dirmod.STATES:
            self.show_areas(self.state)
        else:
            self.show_states()
        await self._redraw(interaction)

    async def _close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="Closed.", embed=None, view=None)

    async def _station_picked(self, interaction: discord.Interaction):
        station = self.directory.by_uuid.get(interaction.data["values"][0])
        await interaction.response.defer(ephemeral=True)
        if station is None:
            await interaction.followup.send("That station is no longer in the list. Please search again.", ephemeral=True)
            return
        ok, message = await self.cog.bot.public.tune(interaction.user, station, interaction.channel_id)
        if not ok:
            await interaction.followup.send(f"❌ {message}", ephemeral=True)
            return
        if self.state in dirmod.STATES:
            await self.cog.bot.public.remember_area(interaction.user.id, self.state, self.city)
            self.saved_area = (self.state, self.city)
        embed, card = await now_playing(station, "Now playing", by=interaction.user.display_name)
        player = self.cog.bot.public.players.get(interaction.guild_id)
        tip = ""
        if player is not None and getattr(player, "volume", 1) <= 0.5:
            tip = (f"\n🔈 Volume starts low ({round(player.volume * 100)}%). Use the 🔊 button to raise it for everyone, or "
                   "right-click the bot in the voice channel → **User Volume** to raise it just for you.")
        extra = {"file": card} if card else {}
        try:
            await interaction.channel.send(embed=embed, view=RemoteView(self.cog), **extra)
            await interaction.followup.send("✅ Tuned in!" + tip, ephemeral=True)
        except (discord.HTTPException, AttributeError):
            await interaction.followup.send(content=tip.strip() or None, embed=embed, ephemeral=True, **extra)


class RemoteView(discord.ui.View):
    """Tap-to-control buttons under the Now Playing card. Permanent, like the panel, so they keep
    working after a restart; the cog applies the same who-may-control rules as the slash commands."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Change station", emoji="📻", style=discord.ButtonStyle.primary, custom_id="crue:remote:change")
    async def change(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.open_picker(interaction)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="crue:remote:down")
    async def quieter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.nudge_volume(interaction, -0.1)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="crue:remote:up")
    async def louder(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.nudge_volume(interaction, +0.1)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="crue:remote:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.stop_radio(interaction)


class PanelView(discord.ui.View):
    """A permanent control strip an admin can pin in a channel. Its buttons keep working across
    restarts because the view has no timeout and fixed custom ids."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Find a station", emoji="📻", style=discord.ButtonStyle.success, custom_id="crue:panel:find")
    async def find(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.open_picker(interaction)

    @discord.ui.button(label="Now playing", emoji="🎵", style=discord.ButtonStyle.secondary, custom_id="crue:panel:now")
    async def now(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_now(interaction)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="crue:panel:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.stop_radio(interaction)
