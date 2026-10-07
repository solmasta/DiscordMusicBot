"""Browse the song library by genre and play songs, all in one private message that changes as you tap."""
import random

import discord

import library as L
from picker import BRAND, clean

SOURCE_LABEL = {L.HITS: ("🔥", "Popular hits"), L.FREE: ("🆓", "Free music")}


class LibraryView(discord.ui.View):
    """genres -> songs. The home server can switch between popular hits and free music; every other
    server only gets free music (the hits are not licensed for a public bot)."""

    def __init__(self, cog, user_id: int, home: bool):
        super().__init__(timeout=840)
        self.cog, self.user_id, self.home = cog, user_id, home
        self.library = cog.bot.library
        self.sources = [s for s in ((L.HITS,) if home else ()) + (L.FREE,) if s == L.HITS or self.library.free_enabled]
        self.source = self.sources[0]
        self.screen = "genres"
        self.genre = None
        self.songs: list = []
        self.page = 0
        self.more = False
        self._render()

    # ---- state
    async def show_genre(self, genre: str, page: int = 0):
        self.genre, self.page, self.screen = genre, page, "songs"
        if self.source == L.HITS:
            self.songs, self.more = self.library.hits(genre), False
        else:
            self.songs = await self.library.free(genre, page)
            self.more = len(self.songs) >= L.PAGE
        self._render()

    def show_genres(self):
        self.screen, self.genre, self.songs, self.page = "genres", None, [], 0
        self._render()

    # ---- rendering
    def embed(self) -> discord.Embed:
        icon, label = SOURCE_LABEL[self.source]
        if self.screen == "genres":
            lines = [f"**Pick a genre** to browse songs. Now browsing: {icon} **{label}**."]
            if self.source == L.FREE:
                lines.append("Free music is Creative Commons music from independent artists.")
            else:
                lines.append("Popular songs by genre, found when you play them.")
            lines.append("🎧 Join the voice channel first, then tap a song to play it.")
            return discord.Embed(title="🎵 Song library", description="\n".join(lines), color=BRAND)
        lines = []
        for i, s in enumerate(self.songs, start=self.page * L.PAGE + 1):
            length = f" · {s.duration_str}" if s.duration_str else ""
            lines.append(f"`{i:>2}.` {clean(s.title)[:60]} · {clean(s.artist)[:40]}{length}")
        embed = discord.Embed(title=f"{L.genre_emoji(self.genre)} {self.genre} · {label}", color=BRAND,
                              description="\n".join(lines) or "No songs found for this genre right now.")
        embed.set_footer(text=f"Tap a song to play it, and the rest of the list plays after it · page {self.page + 1}")
        return embed

    def _render(self):
        self.clear_items()
        if self.screen == "genres":
            options = [discord.SelectOption(label=g, value=g, emoji=e) for g, e in self.library.genres(self.source)]
            sel = discord.ui.Select(placeholder="🎵 Choose a genre…", options=options[:25], row=0)
            sel.callback = self._genre_picked
            self.add_item(sel)
            for src in self.sources:
                if len(self.sources) > 1:
                    icon, label = SOURCE_LABEL[src]
                    self._button(label, self._source_clicked(src), emoji=icon, row=1,
                                 style=discord.ButtonStyle.primary if src == self.source else discord.ButtonStyle.secondary)
            self._button("Close", self._close, style=discord.ButtonStyle.danger, row=1)
            return
        row = 0
        if self.songs:
            options = [discord.SelectOption(label=s.title[:100] or "Song", value=s.id[:100], emoji=L.genre_emoji(s.genre),
                                            description=(f"{s.artist} · {s.duration_str}" if s.duration_str else s.artist)[:100])
                       for s in self.songs[:25]]
            sel = discord.ui.Select(placeholder="Tap a song to play it…", options=options, row=0)
            sel.callback = self._song_picked
            self.add_item(sel)
            row = 1
            self._button("Shuffle all", self._shuffle, emoji="🔀", style=discord.ButtonStyle.success, row=row)
        if self.source == L.FREE:
            self._button("◀ Prev", self._prev, disabled=self.page == 0, row=row)
            self._button("Next ▶", self._next, disabled=not self.more, row=row)
        self._button("← Genres", self._back, row=row)
        self._button("Close", self._close, style=discord.ButtonStyle.danger, row=row)

    def _button(self, label, callback, *, style=discord.ButtonStyle.secondary, emoji=None, disabled=False, row=None):
        b = discord.ui.Button(label=label, style=style, emoji=emoji, disabled=disabled, row=row)
        b.callback = callback
        self.add_item(b)

    # ---- interactions
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This menu belongs to someone else. Use `/library` to get your own.", ephemeral=True)
            return False
        return True

    async def _redraw(self, interaction: discord.Interaction):
        await interaction.edit_original_response(embed=self.embed(), view=self)

    async def _genre_picked(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.show_genre(interaction.data["values"][0])
        await self._redraw(interaction)

    def _source_clicked(self, src: str):
        async def callback(interaction: discord.Interaction):
            self.source = src
            self._render()
            await interaction.response.edit_message(embed=self.embed(), view=self)
        return callback

    async def _flip(self, interaction: discord.Interaction, delta: int):
        await interaction.response.defer()
        await self.show_genre(self.genre, max(0, self.page + delta))
        await self._redraw(interaction)

    async def _prev(self, interaction):
        await self._flip(interaction, -1)

    async def _next(self, interaction):
        await self._flip(interaction, +1)

    async def _back(self, interaction: discord.Interaction):
        self.show_genres()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="Closed.", embed=None, view=None)

    async def _play(self, interaction: discord.Interaction, songs: list):
        await interaction.response.defer(ephemeral=True)
        ok, message = await self.cog.play(interaction, songs)
        if not ok:
            await interaction.followup.send(f"❌ {message}", ephemeral=True)
            return
        tip = ""
        player = None if self.home else self.cog.bot.public.players.get(interaction.guild_id)
        if player is not None and getattr(player, "volume", 1) <= 0.5:
            tip = f"\n🔈 Volume starts low ({round(player.volume * 100)}%). Use the 🔊 button on the radio panel to raise it."
        await interaction.followup.send(f"✅ {message}. The rest of this list plays after it." + tip, ephemeral=True)

    async def _song_picked(self, interaction: discord.Interaction):
        wanted = interaction.data["values"][0]
        idx = next((i for i, s in enumerate(self.songs) if s.id[:100] == wanted), None)
        if idx is None:
            await interaction.response.send_message("That song is no longer in the list. Please pick again.", ephemeral=True)
            return
        await self._play(interaction, self.songs[idx:] + self.songs[:idx])

    async def _shuffle(self, interaction: discord.Interaction):
        songs = list(self.songs)
        random.shuffle(songs)
        await self._play(interaction, songs)
