"""/premium: show whether this server has premium and offer the subscribe button."""
import discord
from discord import app_commands
from discord.ext import commands


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
            await interaction.response.send_message("⭐ This server has **Premium**. Thank you for supporting Crüe FM! You get 24/7 radio, 25 favorites and priority slots.", ephemeral=True)
            return
        view = discord.ui.View()
        view.add_item(discord.ui.Button(sku_id=int(prem.sku_id)))   # Discord renders the Subscribe button
        await interaction.response.send_message(
            "This server is on the **free plan**. **Premium** adds:\n"
            "• True **24/7**: the radio never stops (free servers get a few hours at a time) and stays on with nobody listening\n"
            "• **25 saved favorite stations** (free servers get 3)\n"
            "• **Priority slots**: starts even when the bot is busy",
            view=view, ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PremiumCog(bot))
