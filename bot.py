import os
import asyncio
import logging
import discord
from discord.ext import commands, tasks
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("radiobot")

TOKEN = os.getenv("DISCORD_TOKEN")
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID", "0"))
STREAM_URL = os.getenv(
    "STREAM_URL",
    "https://playerservices.streamtheworld.com/api/livestream-redirect/WKQXFMAAC.aac",
)

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
                name="CRUE FM 🤘",
            )
        )
        if not self.watchdog.is_running():
            self.watchdog.start()

    @tasks.loop(seconds=30)
    async def watchdog(self):
        try:
            channel = self.get_channel(VOICE_CHANNEL_ID) or await self.fetch_channel(VOICE_CHANNEL_ID)
            vc = discord.utils.get(self.voice_clients, guild=channel.guild)
            if vc is None:
                vc = await channel.connect()
                log.info("Joined voice channel: %s", channel.name)
            if not vc.is_playing() and not vc.is_paused():
                source = discord.FFmpegPCMAudio(STREAM_URL, **FFMPEG_OPTIONS)
                vc.play(source, after=lambda e: log.warning("Stream ended: %s", e) if e else None)
                log.info("Streaming CRUE FM")
        except Exception as e:
            log.error("Watchdog error: %s", e)

    @watchdog.before_loop
    async def before_watchdog(self):
        await self.wait_until_ready()


async def health_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health server on port %d", port)


async def main():
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN is not set")
    bot = RadioBot()
    async with bot:
        await asyncio.gather(bot.start(TOKEN), health_server())


if __name__ == "__main__":
    asyncio.run(main())
