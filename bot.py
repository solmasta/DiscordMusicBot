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

STATIONS = {
    "crue": {
        "name": "CRUE FM",
        "emoji": "🤘",
        "url": "https://ice3.somafm.com/metal-128-mp3",
    },
    "q101": {
        "name": "Q101",
        "emoji": "🎶",
        "url": "https://ice3.somafm.com/u80s-128-mp3",
    },
}

DEFAULT_STATION = os.getenv("DEFAULT_STATION", "crue")

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class RadioBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.current_station = DEFAULT_STATION

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        await self._update_presence()
        if not self.watchdog.is_running():
            self.watchdog.start()
        await self.tree.sync()

    async def _update_presence(self):
        s = STATIONS[self.current_station]
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"{s['emoji']} {s['name']}",
            )
        )

    @tasks.loop(seconds=30)
    async def watchdog(self):
        try:
            channel = self.get_channel(VOICE_CHANNEL_ID) or await self.fetch_channel(VOICE_CHANNEL_ID)
            vc = discord.utils.get(self.voice_clients, guild=channel.guild)
            if vc is None:
                vc = await channel.connect()
                log.info("Joined %s", channel.name)
            if not vc.is_playing() and not vc.is_paused():
                await self._start_stream(vc)
        except Exception as e:
            log.error("Watchdog error: %s", e)

    @watchdog.before_loop
    async def before_watchdog(self):
        await self.wait_until_ready()

    async def _start_stream(self, vc: discord.VoiceClient):
        s = STATIONS[self.current_station]
        log.info("Streaming %s (%s)", s['name'], s['url'])
        source = discord.FFmpegPCMAudio(s["url"], **FFMPEG_OPTIONS)
        vc.play(
            source,
            after=lambda e: log.error("Stream error: %s", e) if e else None,
        )

    async def switch_station(self, key: str, vc: discord.VoiceClient):
        if key not in STATIONS:
            return False
        self.current_station = key
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await self._start_stream(vc)
        await self._update_presence()
        return True


async def setup_commands(bot: RadioBot):
    @bot.tree.command(name="station", description="Switch radio station")
    @discord.app_commands.describe(name="Station to switch to")
    @discord.app_commands.choices(name=[
        discord.app_commands.Choice(name="CRUE FM 🤘 (Metal/Hard Rock)", value="crue"),
        discord.app_commands.Choice(name="Q101 🎶 (Underground 80s)", value="q101"),
    ])
    async def station_cmd(interaction: discord.Interaction, name: str):
        vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        if not vc:
            await interaction.response.send_message("Bot isn't in a voice channel yet.", ephemeral=True)
            return
        ok = await bot.switch_station(name, vc)
        s = STATIONS[name]
        if ok:
            await interaction.response.send_message(f"Switched to **{s['emoji']} {s['name']}**!")
        else:
            await interaction.response.send_message("Unknown station.", ephemeral=True)

    @bot.tree.command(name="nowplaying", description="Show current station")
    async def nowplaying_cmd(interaction: discord.Interaction):
        s = STATIONS[bot.current_station]
        embed = discord.Embed(
            title=f"{s['emoji']} {s['name']}",
            description="Streaming live 24/7",
            color=0xCC0000,
        )
        await interaction.response.send_message(embed=embed)


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
    await setup_commands(bot)
    async with bot:
        await asyncio.gather(bot.start(TOKEN), health_server())


if __name__ == "__main__":
    asyncio.run(main())
