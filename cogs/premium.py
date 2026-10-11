"""/premium: show whether this server has premium and offer the subscribe button."""
import asyncio

import discord
from discord import app_commands
from discord.ext import commands

import directory as dirmod
import theme as T
from picker import now_playing


class PremiumCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="premium", description="See this server's plan and upgrade to Premium")
    @app_commands.guild_only()
    async def premium(self, interaction: discord.Interaction):
        prem = self.bot.premium
        if not prem.enabled:
            await interaction.response.send_message("Premium isn't available yet. Everything is free for now.", ephemeral=True)
            return
        if prem.is_premium(interaction.guild_id):
            await interaction.response.send_message("⭐ This server has **Premium**. Thank you for supporting Crüe FM! You get 24/7 radio, `/theme` to make it yours, 25 favorites and priority slots.", ephemeral=True)
            return
        view = discord.ui.View()
        view.add_item(discord.ui.Button(sku_id=int(prem.sku_id)))   # Discord renders the Subscribe button
        await interaction.response.send_message(
            "This server is on the **free plan**. **Premium** adds:\n"
            "• True **24/7**: the radio never stops (free servers get a few hours at a time) and stays on with nobody listening\n"
            "• **Your own look**: name, colour, tagline and logo (`/theme`)\n"
            "• **25 saved favorite stations** (free servers get 3)\n"
            "• **Priority slots**: starts even when the bot is busy",
            view=view, ephemeral=True,
        )


PREVIEW = dirmod.Station(uuid="preview", name="Your Station 101.5", url="", state=None, city=None, market=None,
                         genres=frozenset({"Rock"}), bitrate=128, codec="MP3")


class ThemeCog(commands.Cog):
    """/theme: Premium servers make the radio card and panel their own."""
    theme = app_commands.Group(name="theme", description="Make the radio look like your server (Premium)", guild_only=True,
                               default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot):
        self.bot = bot

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You need the **Manage Server** permission to change the theme.", ephemeral=True)
            return False
        if not self.bot.public.is_premium(interaction.guild_id):
            await interaction.response.send_message("Custom themes are a **Premium** feature. See `/premium`.", ephemeral=True)
            return False
        return True

    async def _preview(self, interaction: discord.Interaction, text: str):
        theme = self.bot.public.theme_for(interaction.guild_id)
        embed, card = await now_playing(PREVIEW, "Now playing", theme=theme)
        extra = {"file": card} if card else {}
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(text, embed=embed, ephemeral=True, **extra)

    @theme.command(name="set", description="Set your radio's name, colour and tagline")
    @app_commands.describe(name="Shown as the card title, e.g. Pixel Lounge FM", tagline="A short line under the card",
                           preset="A ready-made colour", color="Your own colour as hex, e.g. #ff8800 (overrides the preset)")
    @app_commands.choices(preset=[app_commands.Choice(name=n, value=n) for n in T.PRESETS])
    async def set(self, interaction: discord.Interaction, name: str | None = None, tagline: str | None = None,
                  preset: app_commands.Choice[str] | None = None, color: str | None = None):
        if not await self._allowed(interaction):
            return
        pub = self.bot.public
        theme = pub.saved_theme(interaction.guild_id)
        if color:
            value = T.parse_color(color)
            if value is None:
                await interaction.response.send_message("Colours look like `#ff8800` (six hex digits). Or pick a preset.", ephemeral=True)
                return
            theme.color = value
        elif preset:
            theme.color = T.PRESETS[preset.value]
        if name is not None:
            theme.name = T.clean_text(name, T.NAME_MAX)
        if tagline is not None:
            theme.tagline = T.clean_text(tagline, T.TAGLINE_MAX)
        await pub.save_theme(theme)
        await self._preview(interaction, "✅ Theme saved. Here's how your card looks:")

    @theme.command(name="logo", description="Add your server's logo to the radio card")
    @app_commands.describe(image="A square PNG, JPG or WebP under 2 MB")
    async def logo(self, interaction: discord.Interaction, image: discord.Attachment):
        if not await self._allowed(interaction):
            return
        if image.size > T.LOGO_MAX_BYTES or not (image.content_type or "").startswith("image/"):
            await interaction.response.send_message("Please attach an image (PNG, JPG or WebP) under 2 MB.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            png = await asyncio.to_thread(T.make_logo, await image.read())
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        theme = self.bot.public.saved_theme(interaction.guild_id)
        theme.logo = png
        await self.bot.public.save_theme(theme)
        await self._preview(interaction, "✅ Logo saved. Here's how your card looks:")

    @theme.command(name="preview", description="See how your radio card looks")
    async def preview(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await self._preview(interaction, "Your card:")

    @theme.command(name="reset", description="Go back to the default look")
    async def reset(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await self.bot.public.save_theme(T.Theme(interaction.guild_id))
        await interaction.response.send_message("Back to the default look.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PremiumCog(bot))
    await bot.add_cog(ThemeCog(bot))
