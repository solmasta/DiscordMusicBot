"""/vote: vote for the bot on top.gg, then spend the vote on this server for a free 12-hour boost."""
import time

import discord
from discord import app_commands
from discord.ext import commands

import votes as V


def _hours(seconds: float) -> str:
    h = seconds / 3600
    return f"{h:.0f} hour{'s' if round(h) != 1 else ''}" if h >= 1 else f"{max(1, int(seconds // 60))} minutes"


class VoteView(discord.ui.View):
    def __init__(self, cog, bot_id: int, user_id: int, credits: int):
        super().__init__(timeout=300)
        self.cog, self.user_id = cog, user_id
        self.add_item(discord.ui.Button(label="Vote on top.gg", url=f"https://top.gg/bot/{bot_id}/vote", emoji="🗳️"))
        claim = discord.ui.Button(label=f"Use my vote here ({credits})" if credits else "Use my vote here",
                                  style=discord.ButtonStyle.success, emoji="⚡", disabled=credits <= 0)
        claim.callback = self._claim
        self.add_item(claim)

    async def _claim(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This button is for the person who ran the command.", ephemeral=True)
            return
        ok, left = await self.cog.bot.votes.claim(interaction.user.id, interaction.guild_id)
        if not ok:
            await interaction.response.edit_message(content="No votes waiting. Vote first, then press the button again.", view=None)
            return
        self.cog.bot.public.changed(interaction.guild_id)
        await interaction.response.edit_message(
            content=f"⚡ Thank you! **{interaction.guild.name}** has a boost for the next **{_hours(left)}**: true 24/7 and priority slots.",
            view=None)


class VoteCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="vote", description="Vote for Crüe FM and give this server a free 12-hour boost")
    @app_commands.guild_only()
    async def vote(self, interaction: discord.Interaction):
        votes = self.bot.votes
        if not votes.enabled:
            await interaction.response.send_message("Vote boosts aren't available yet.", ephemeral=True)
            return
        if self.bot.public.is_premium(interaction.guild_id):
            await interaction.response.send_message("⭐ This server already has **Premium**, so it has everything a vote gives. Thanks!", ephemeral=True)
            return
        credits, left = votes.credits_for(interaction.user.id), votes.boost_left(interaction.guild_id)
        lines = [f"Vote for Crüe FM on top.gg (once every 12 hours), then press **Use my vote here**. Each vote gives this server "
                 f"**{_hours(V.BOOST_S)}** of true **24/7** and **priority slots**, and helps more people find the bot."]
        if left:
            lines.append(f"⚡ This server's boost has **{_hours(left)}** left.")
        lines.append(f"🗳️ You have **{credits}** vote{'s' if credits != 1 else ''} waiting." if credits
                     else "🗳️ You have no votes waiting yet. Vote, wait a few seconds, then run `/vote` again.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True,
                                                view=VoteView(self, self.bot.user.id, interaction.user.id, credits))


async def setup(bot: commands.Bot):
    await bot.add_cog(VoteCog(bot))
