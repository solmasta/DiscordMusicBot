import os
import asyncio
import logging
import discord
from discord.ext import commands, tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("radiobot")

TOKEN = os.getenv("DISCORD_TOKEN")
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID", "0"))

STATION = {
    "name": "Q101",
    "emoji": "🎶",
    # Set STATION_URL to point Q101 at a different stream.
    "url": os.getenv("STATION_URL", "https://ice3.somafm.com/u80s-128-mp3"),
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class RadioBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"{STATION['emoji']} {STATION['name']}",
            )
        )
        if not self.watchdog.is_running():
            self.watchdog.start()
        await self.tree.sync()

    @tasks.loop(seconds=30)
    async def watchdog(self):
        try:
            channel = self.get_channel(VOICE_CHANNEL_ID) or await self.fetch_channel(VOICE_CHANNEL_ID)
            vc = discord.utils.get(self.voice_clients, guild=channel.guild)
            if vc is None:
                vc = await channel.connect()
                log.info("Joined %s", channel.name)
            if not vc.is_playing() and not vc.is_paused():
                self._start_stream(vc)
        except Exception as e:
            log.error("Watchdog error: %s", e)

    @watchdog.before_loop
    async def before_watchdog(self):
        await self.wait_until_ready()

    def _start_stream(self, vc: discord.VoiceClient):
        log.info("Streaming %s (%s)", STATION["name"], STATION["url"])
        source = discord.FFmpegPCMAudio(STATION["url"], **FFMPEG_OPTIONS)
        vc.play(
            source,
            after=lambda e: log.error("Stream error: %s", e) if e else None,
        )


def setup_commands(bot: RadioBot):
    @bot.tree.command(name="nowplaying", description="Show the current station")
    async def nowplaying_cmd(interaction: discord.Interaction):
        embed = discord.Embed(
            title=f"{STATION['emoji']} {STATION['name']}",
            description="Streaming live 24/7",
            color=0xCC0000,
        )
        await interaction.response.send_message(embed=embed)


async def main():
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN is not set")
    if not VOICE_CHANNEL_ID:
        raise ValueError("VOICE_CHANNEL_ID is not set")
    bot = RadioBot()
    setup_commands(bot)
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
