import os
import asyncio
import logging
import re
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

# Rock 95.5 (WCHI-FM Chicago, TuneIn s21577) is the second station. Whichever station is on
# keeps playing until it hits a commercial break, then the bot swaps to the other one.
ROCK_URL = os.getenv("ROCK_URL", "https://stream.revma.ihrhls.com/zc857")
TRITON_MOUNT = "WKQXFM"
TRITON_URL = "https://np.tritondigital.com/public/nowplaying?mountName={mount}&numberToFetch=1&eventType={event}"
Q101_BREAK_GRACE_MS = 30_000
MIN_SWITCH_SECONDS = 10

FFMPEG_RADIO_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self._http: aiohttp.ClientSession | None = None
        self._current = "q101"          # station being played: "q101" or "rock"
        self._q_break = False           # Q101 is in a commercial break
        self._r_break: bool | None = None  # Rock 95.5 break state; None = unknown / reader down
        self._last_switch = 0.0
        self._rock_task: asyncio.Task | None = None

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
            if self._rock_task is None or self._rock_task.done():
                self._rock_task = asyncio.create_task(self._rock_reader())

    async def close(self):
        if self._rock_task:
            self._rock_task.cancel()
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
        url = ROCK_URL if self._current == "rock" else RADIO_URL
        source = discord.FFmpegPCMAudio(url, **FFMPEG_RADIO_OPTIONS)
        vc.play(source, after=lambda err: log.warning("Stream ended: %s", err) if err else None)
        log.info("Radio: playing %s → %s", "Rock 95.5" if self._current == "rock" else "Q101", url)

    def _reevaluate(self):
        """Swap stations when the one that's playing hits a commercial and the other is on music."""
        target = self._current
        if self._current == "q101":
            if self._q_break and self._r_break is False:
                target = "rock"
        else:
            if self._r_break is None:
                target = "q101"
            elif self._r_break and not self._q_break:
                target = "q101"
        if target == self._current:
            return
        if time.monotonic() - self._last_switch < MIN_SWITCH_SECONDS:
            return
        if self._current == "rock" and self._r_break is None:
            log.info("Rock 95.5 metadata unavailable — switching back to Q101")
        else:
            log.info(
                "Break: %s hit a commercial — switching to %s",
                "Q101" if self._current == "q101" else "Rock 95.5",
                "Rock 95.5" if target == "rock" else "Q101",
            )
        self._current = target
        self._last_switch = time.monotonic()
        channel = self.get_channel(int(RADIO_CHANNEL_ID))
        vc = channel.guild.voice_client if channel else None
        if vc and vc.is_connected():
            self._start_playing(vc)

    async def _latest_cue(self, event: str):
        """Return (start_ms, duration_ms) of the newest Triton cue of this type, or None."""
        url = TRITON_URL.format(mount=TRITON_MOUNT, event=event)
        async with self._http.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            root = ET.fromstring(await resp.text())
        for info in root:
            props = {p.get("name"): p.text for p in info}
            return int(props["cue_time_start"]), int(props["cue_time_duration"])
        return None

    async def _q101_in_break(self) -> bool:
        track, ad = await asyncio.gather(self._latest_cue("track"), self._latest_cue("ad"))
        if not ad:
            return False
        if track and track[0] >= ad[0]:
            return False
        return time.time() * 1000 < ad[0] + ad[1] + Q101_BREAK_GRACE_MS

    @tasks.loop(seconds=5)
    async def _break_monitor(self):
        """Refresh Q101's commercial state and re-check whether to swap stations."""
        try:
            self._q_break = await self._q101_in_break()
        except Exception as e:
            log.warning("Q101 break check failed: %s", e)
            return
        self._reevaluate()

    @_break_monitor.before_loop
    async def _before_break_monitor(self):
        await self.wait_until_ready()

    @staticmethod
    def _rock_meta_is_ad(meta: str):
        """iHeart ICY metadata: song_spot T = commercial, M/F = music. None if it says nothing."""
        m = re.search(r'song_spot="(\w)"', meta)
        if m:
            return m.group(1) == "T"
        if "Spot Block End" in meta:
            return False
        return None

    async def _read_rock_once(self):
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
        headers = {"Icy-MetaData": "1", "User-Agent": "Mozilla/5.0"}
        async with self._http.get(ROCK_URL, headers=headers, timeout=timeout) as resp:
            metaint = int(resp.headers.get("icy-metaint", 0))
            if not metaint:
                raise RuntimeError("Rock 95.5 stream has no ICY metadata")
            log.info("Rock 95.5 metadata reader connected")
            while True:
                await resp.content.readexactly(metaint)
                length = (await resp.content.readexactly(1))[0] * 16
                if not length:
                    continue
                meta = (await resp.content.readexactly(length)).decode("utf-8", "ignore")
                is_ad = self._rock_meta_is_ad(meta)
                if is_ad is not None and is_ad != self._r_break:
                    self._r_break = is_ad
                    log.info("Rock 95.5: %s", "commercial" if is_ad else "music")
                    self._reevaluate()

    async def _rock_reader(self):
        """Keep one metadata connection open to Rock 95.5 so its break state is always current."""
        while True:
            try:
                await self._read_rock_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Rock 95.5 metadata reader error: %s", e)
            self._r_break = None
            self._reevaluate()
            await asyncio.sleep(5)

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
