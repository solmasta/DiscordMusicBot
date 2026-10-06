import os
import asyncio
import logging
import time
import xml.etree.ElementTree as ET
import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
RADIO_CHANNEL_ID = os.getenv("RADIO_CHANNEL_ID")
# Direct streamtheworld URL — FFmpeg follows the 302 redirect to the live Q101 stream
RADIO_URL = os.getenv(
    "RADIO_URL",
    "https://playerservices.streamtheworld.com/api/livestream-redirect/WKQXFM.mp3",
)

# Rock 95.5 (WCHI-FM Chicago, TuneIn s21577) plays while Q101 is in a commercial break
BREAK_URL = os.getenv("BREAK_URL", "https://stream.revma.ihrhls.com/zc857")
TRITON_MOUNT = "WKQXFM"
TRITON_URL = "https://np.tritondigital.com/public/nowplaying?mountName={mount}&numberToFetch=1&eventType={event}"
BREAK_GRACE_MS = 30_000

FFMPEG_RADIO_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self._on_break = False
        self._http: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self._http = aiohttp.ClientSession()
        await self.load_extension("cogs.music")

        guild = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
        try:
            if guild:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("Slash commands synced to guild %s", GUILD_ID)
            else:
                await self.tree.sync()
                log.info("Slash commands synced globally (may take up to 1 hour)")
        except discord.Forbidden:
            log.warning("Guild sync forbidden (Missing Access) — falling back to global sync")
            try:
                await self.tree.sync()
                log.info("Slash commands synced globally")
            except Exception as e:
                log.warning("Global slash command sync failed (non-fatal): %s", e)
        except Exception as e:
            log.warning("Slash command sync failed (non-fatal): %s", e)

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="Crue FM 📻" if RADIO_URL else "/play to add music",
            )
        )
        if RADIO_URL and RADIO_CHANNEL_ID:
            if not self._radio_keepalive.is_running():
                self._radio_keepalive.start()
            if not self._break_monitor.is_running():
                self._break_monitor.start()

    async def close(self):
        if self._http:
            await self._http.close()
        await super().close()

    async def on_voice_state_update(self, member, before, after):
        if RADIO_URL and RADIO_CHANNEL_ID:
            return
        if member == self.user:
            return
        if before.channel and before.channel.guild.voice_client:
            vc = before.channel.guild.voice_client
            if vc.channel == before.channel:
                members = [m for m in vc.channel.members if not m.bot]
                if not members:
                    await asyncio.sleep(30)
                    vc = before.channel.guild.voice_client
                    if vc and vc.channel == before.channel:
                        members = [m for m in vc.channel.members if not m.bot]
                        if not members:
                            await vc.disconnect()
                            log.info("Auto-disconnected from empty channel in %s", before.channel.guild.name)

    def _start_playing(self, vc: discord.VoiceClient):
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        url = BREAK_URL if self._on_break else RADIO_URL
        source = discord.FFmpegPCMAudio(url, **FFMPEG_RADIO_OPTIONS)
        vc.play(source, after=lambda err: log.warning("Stream ended: %s", err) if err else None)
        log.info("Radio: playing %s → %s", "Rock 95.5 (commercial break)" if self._on_break else "Q101", url)

    async def _latest_cue(self, event: str):
        """Return (start_ms, duration_ms) of the newest Triton cue of this type, or None."""
        url = TRITON_URL.format(mount=TRITON_MOUNT, event=event)
        async with self._http.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            root = ET.fromstring(await resp.text())
        for info in root:
            props = {p.get("name"): p.text for p in info}
            return int(props["cue_time_start"]), int(props["cue_time_duration"])
        return None

    async def _in_ad_break(self) -> bool:
        track, ad = await asyncio.gather(self._latest_cue("track"), self._latest_cue("ad"))
        if not ad:
            return False
        if track and track[0] >= ad[0]:
            return False
        return time.time() * 1000 < ad[0] + ad[1] + BREAK_GRACE_MS

    @tasks.loop(seconds=5)
    async def _break_monitor(self):
        """Swap to Rock 95.5 during Q101 commercial breaks, back to Q101 when music resumes."""
        try:
            in_break = await self._in_ad_break()
        except Exception as e:
            log.warning("Break check failed: %s", e)
            return
        if in_break == self._on_break:
            return
        self._on_break = in_break
        log.info("Break: %s", "commercial detected" if in_break else "music resumed")
        channel = self.get_channel(int(RADIO_CHANNEL_ID))
        vc = channel.guild.voice_client if channel else None
        if vc and vc.is_connected():
            self._start_playing(vc)

    @_break_monitor.before_loop
    async def _before_break_monitor(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=5)
    async def _radio_keepalive(self):
        """Keep Q101 playing 24/7."""
        channel = self.get_channel(int(RADIO_CHANNEL_ID))
        if not channel or not isinstance(channel, discord.VoiceChannel):
            log.warning("RADIO_CHANNEL_ID %s not found or not a voice channel", RADIO_CHANNEL_ID)
            return

        vc = channel.guild.voice_client

        if not vc or not vc.is_connected():
            try:
                vc = await channel.connect(timeout=10.0, reconnect=True)
                log.info("Radio: connected to %s", channel.name)
            except Exception as e:
                log.error("Radio: failed to connect: %s", e)
                return

        if vc.channel != channel:
            await vc.move_to(channel)

        if not vc.is_playing() and not vc.is_paused():
            self._start_playing(vc)

    @_radio_keepalive.before_loop
    async def _before_keepalive(self):
        await self.wait_until_ready()

    async def on_app_command_error(self, interaction: discord.Interaction, error: Exception):
        msg = f"Something went wrong: `{error}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass
        log.error("App command error in %s: %s", interaction.command, error, exc_info=error)


async def main():
    log.info("Starting bot (GUILD_ID=%r)", GUILD_ID)

    if not TOKEN:
        log.error(
            "DISCORD_TOKEN is not set! "
            "Run: fly secrets set DISCORD_TOKEN=<token> --app discordmusicbot-k-zztq"
        )
        while True:
            await asyncio.sleep(60)

    while True:
        try:
            bot = MusicBot()
            async with bot:
                await bot.start(TOKEN)
        except discord.LoginFailure as e:
            log.error("Invalid Discord token — check DISCORD_TOKEN secret: %s", e)
            await asyncio.sleep(60)
        except Exception as e:
            log.error("Bot crashed, restarting in 30s: %s", e, exc_info=True)
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
