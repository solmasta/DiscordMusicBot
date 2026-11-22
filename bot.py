import os
import asyncio
import logging
import discord
from discord.ext import commands, tasks
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

TOKEN            = os.environ["DISCORD_TOKEN"]
VOICE_CHANNEL_ID = int(os.environ["VOICE_CHANNEL_ID"])

STATIONS = {
    "crue": ("CRUE FM 🤘",  "https://ice3.somafm.com/metal-128-mp3"),
    "q101": ("Q101 🎵",     "https://ice3.somafm.com/u80s-128-mp3"),
}
STATION = "crue"

FF_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class CrueFM(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.station = STATION

    async def on_ready(self):
        log.info("Ready: %s | discord.py %s", self.user, discord.__version__)
        await self._sync_commands()
        await self._set_presence()
        if not self.guard.is_running():
            self.guard.start()

    async def _sync_commands(self):
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash command(s)", len(synced))
        except Exception as e:
            log.error("Sync error: %s", e)

    async def _set_presence(self):
        name, _ = STATIONS[self.station]
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name=name)
        )

    @tasks.loop(seconds=20)
    async def guard(self):
        try:
            ch = self.get_channel(VOICE_CHANNEL_ID) \
                 or await self.fetch_channel(VOICE_CHANNEL_ID)
            vc = discord.utils.get(self.voice_clients, guild=ch.guild)

            if vc is None:
                vc = await ch.connect()
                log.info("Joined %s", ch.name)
                await self._diagnose(ch)

            if not vc.is_playing() and not vc.is_paused():
                await self._play(vc)
        except Exception:
            log.exception("guard error")

    @guard.before_loop
    async def _wait(self):
        await self.wait_until_ready()

    async def _diagnose(self, ch):
        me = ch.guild.me
        perms = ch.permissions_for(me)
        vs = me.voice
        log.info("DIAG channel type=%s connect=%s speak=%s | server_mute=%s self_mute=%s suppress=%s",
                 type(ch).__name__, perms.connect, perms.speak,
                 vs and vs.mute, vs and vs.self_mute, vs and vs.suppress)

        if not perms.speak:
            log.warning("DIAG bot has NO SPEAK permission in %s", ch.name)

        if vs and vs.mute:
            try:
                await me.edit(mute=False)
                log.info("DIAG removed own server mute")
            except discord.Forbidden:
                log.warning("DIAG bot is SERVER MUTED and cannot unmute itself")

        if isinstance(ch, discord.StageChannel) and vs and vs.suppress:
            try:
                await me.edit(suppress=False)
                log.info("DIAG stage channel: now a speaker")
            except discord.Forbidden:
                await me.request_to_speak()
                log.warning("DIAG stage channel: requested to speak, a moderator must accept")

    async def _play(self, vc: discord.VoiceClient):
        name, url = STATIONS[self.station]
        log.info("Playing %s  %s", name, url)
        src = await discord.FFmpegOpusAudio.from_probe(url, **FF_OPTS)
        vc.play(src, after=lambda e: log.error("after: %s", e) if e else None)


def register(bot: CrueFM):
    @bot.tree.command(name="station", description="Switch radio station")
    @discord.app_commands.choices(name=[
        discord.app_commands.Choice(name="CRUE FM 🤘  Metal/Hard Rock", value="crue"),
        discord.app_commands.Choice(name="Q101 🎵  Underground 80s",    value="q101"),
    ])
    async def station(interaction: discord.Interaction,
                      name: discord.app_commands.Choice[str]):
        vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        if not vc:
            await interaction.response.send_message(
                "Not in a voice channel yet — try again in a moment.", ephemeral=True)
            return
        await interaction.response.defer()
        bot.station = name.value
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await bot._play(vc)
        await bot._set_presence()
        label, _ = STATIONS[name.value]
        await interaction.followup.send(f"Now playing **{label}**")

    @bot.tree.command(name="nowplaying", description="What's on?")
    async def nowplaying(interaction: discord.Interaction):
        label, url = STATIONS[bot.station]
        embed = discord.Embed(title=label, description=f"`{url}`", color=0xCC0000)
        await interaction.response.send_message(embed=embed)


async def health():
    app = web.Application()
    app.router.add_get("/",       lambda _: web.Response(text="ok"))
    app.router.add_get("/health", lambda _: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()


async def main():
    await health()
    bot = CrueFM()
    register(bot)
    async with bot:
        await bot.start(TOKEN)

asyncio.run(main())
