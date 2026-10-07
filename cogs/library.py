"""/library: browse songs by genre and play them."""
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

import library as L
from libpicker import LibraryView

log = logging.getLogger("library")
HOME_GUILD_ID = os.getenv("GUILD_ID")


class SongLibrary(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def is_home(guild_id) -> bool:
        return bool(HOME_GUILD_ID) and str(guild_id) == HOME_GUILD_ID

    async def open_library(self, interaction: discord.Interaction):
        home = self.is_home(interaction.guild_id)
        if not home and not self.bot.library.free_enabled:
            await interaction.response.send_message("The song library isn't set up on this bot yet.", ephemeral=True)
            return
        view = LibraryView(self, interaction.user.id, home)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    async def play(self, interaction: discord.Interaction, songs: list) -> tuple[bool, str]:
        """Play songs here: through the home radio, or the public player (free music only)."""
        if self.is_home(interaction.guild_id):
            return await self.bot.play_songs(interaction.user, songs)
        free = [s for s in songs if s.source == L.FREE]
        return await self.bot.public.play_songs(interaction.user, free, interaction.channel_id)

    async def skip(self, interaction: discord.Interaction):
        if self.is_home(interaction.guild_id):
            ok, message = await self.bot.skip_song(interaction.user)
            await interaction.response.send_message(("⏭ " if ok else "❌ ") + message, ephemeral=True)
            return
        player = self.bot.public.players.get(interaction.guild_id)
        if not player or not player.is_library:
            await interaction.response.send_message("No library songs are playing.", ephemeral=True)
            return
        if not self.bot.public.may_control(interaction.user, player):
            await interaction.response.send_message("Someone else is controlling the radio here.", ephemeral=True)
            return
        self.bot.public.skip(interaction.guild_id)
        await interaction.response.send_message("⏭ Skipped.", ephemeral=True)

    @app_commands.command(name="library", description="Browse songs by genre and play them")
    @app_commands.guild_only()
    async def library(self, interaction: discord.Interaction):
        await self.open_library(interaction)


async def setup(bot):
    await bot.add_cog(SongLibrary(bot))
