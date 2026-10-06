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

ROCK_URL = os.getenv("ROCK_URL", "https://stream.revma.ihrhls.com/zc857")
DRIVE_MOUNT = os.getenv("DRIVE_MOUNT", "WDRVFM")
TRITON_URL = "https://np.tritondigital.com/public/nowplaying?mountName={mount}&numberToFetch=1&eventType={event}"
TRITON_BREAK_GRACE_MS = 30_000
# iHeart's track history gives start/end times per song. Normal song-to-song gaps are 3-12s;
# commercial breaks run 4+ minutes, so a gap longer than this means a break.
IHEART_URL = "https://us.api.iheart.com/api/v3/live-meta/stream/{stream}/trackHistory?limit=1"
ROCK_STREAM_ID = os.getenv("ROCK_STREAM_ID", "857")
ROCK_BREAK_GRACE_S = 15
MIN_SWITCH_SECONDS = 10
FAILS_BEFORE_UNKNOWN = 3

# Rotation order. The station that's on keeps playing until it hits a commercial break, then the
# bot moves to the next station in this list that is on music (wrapping around).
#   triton = streamtheworld stations (Triton now-playing ad cues); iheart = Rock 95.5.
STATIONS = [
    {"key": "q101", "name": "Q101", "url": RADIO_URL, "kind": "triton", "mount": "WKQXFM"},
    {"key": "rock", "name": "Rock 95.5", "url": ROCK_URL, "kind": "iheart"},
    {
        "key": "drive", "name": "97.1 The Drive", "kind": "triton", "mount": DRIVE_MOUNT,
        "url": f"https://playerservices.streamtheworld.com/api/livestream-redirect/{DRIVE_MOUNT}.mp3",
    },
]
STATION_BY_KEY = {st["key"]: st for st in STATIONS}

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
        self._current = STATIONS[0]["key"]
        self._breaks: dict[str, bool | None] = {st["key"]: None for st in STATIONS}  # None = unknown
        self._fails = {st["key"]: 0 for st in STATIONS}
        self._last_switch = 0.0
        self._rock_music_until = 0.0    # monotonic deadline: ICY said a Rock 95.5 song is playing
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
                # Earlier runs registered these globally too; clear them so commands don't show twice.
                try:
                    self.tree.clear_commands(guild=None)
                    await self.tree.sync()
                    log.info("Cleared duplicate global slash commands")
                except Exception as e:
                    log.warning("Could not clear global slash commands (non-fatal): %s", e)
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
                self._rock_task = asyncio.create_task(self._rock_icy_loop())

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
        st = STATION_BY_KEY[self._current]
        source = discord.FFmpegPCMAudio(st["url"], **FFMPEG_RADIO_OPTIONS)
        vc.play(source, after=lambda err: log.warning("Stream ended: %s", err) if err else None)
        log.info("Radio: playing %s → %s", st["name"], st["url"])

    def _reevaluate(self):
        """If the playing station is in a commercial break, move to the next one that's on music."""
        keys = [st["key"] for st in STATIONS]
        cur = self._current
        state = self._breaks[cur]
        target = cur
        if state is True:
            i = keys.index(cur)
            for k in keys[i + 1:] + keys[:i]:
                if self._breaks[k] is False:
                    target = k
                    break
        elif state is None and cur != keys[0]:
            good = [k for k in keys if self._breaks[k] is False]
            target = good[0] if good else keys[0]
        if target == cur:
            return
        if time.monotonic() - self._last_switch < MIN_SWITCH_SECONDS:
            return
        if state is None:
            log.info("%s status unavailable — switching to %s", STATION_BY_KEY[cur]["name"], STATION_BY_KEY[target]["name"])
        else:
            log.info(
                "Break: %s hit a commercial — switching to %s",
                STATION_BY_KEY[cur]["name"], STATION_BY_KEY[target]["name"],
            )
        self._current = target
        self._last_switch = time.monotonic()
        channel = self.get_channel(int(RADIO_CHANNEL_ID))
        vc = channel.guild.voice_client if channel else None
        if vc and vc.is_connected():
            self._start_playing(vc)

    async def _latest_cue(self, mount: str, event: str):
        """Return (start_ms, duration_ms) of the newest Triton cue of this type, or None."""
        url = TRITON_URL.format(mount=mount, event=event)
        async with self._http.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            root = ET.fromstring(await resp.text())
        for info in root:
            props = {p.get("name"): p.text for p in info}
            return int(props["cue_time_start"]), int(props["cue_time_duration"])
        return None

    async def _triton_in_break(self, mount: str) -> bool:
        track, ad = await asyncio.gather(self._latest_cue(mount, "track"), self._latest_cue(mount, "ad"))
        if not ad:
            return False
        if track and track[0] >= ad[0]:
            return False
        return time.time() * 1000 < ad[0] + ad[1] + TRITON_BREAK_GRACE_MS

    async def _station_in_break(self, st: dict) -> bool:
        if st["kind"] == "triton":
            return await self._triton_in_break(st["mount"])
        return await self._rock_in_break()

    def _rock_icy_event(self, meta: str):
        """Fast signal: iHeart tags songs song_spot M/F (with a length) and commercials/sweepers T."""
        spot = re.search(r'song_spot="(\w)"', meta)
        if not spot:
            return
        if spot.group(1) == "T":
            self._rock_music_until = 0.0
            return
        length = re.search(r'length="(\d+):(\d+):(\d+)"', meta)
        if length:
            h, m, sec = map(int, length.groups())
            if h * 3600 + m * 60 + sec > 0:
                self._rock_music_until = time.monotonic() + h * 3600 + m * 60 + sec + 5

    async def _rock_icy_once(self):
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
        headers = {"Icy-MetaData": "1", "User-Agent": "Mozilla/5.0"}
        async with self._http.get(ROCK_URL, headers=headers, timeout=timeout) as resp:
            metaint = int(resp.headers.get("icy-metaint", 0))
            if not metaint:
                raise RuntimeError("no ICY metadata")
            first = True
            while True:
                await resp.content.readexactly(metaint)
                length = (await resp.content.readexactly(1))[0] * 16
                if not length:
                    continue
                meta = (await resp.content.readexactly(length)).decode("utf-8", "ignore")
                # The block sent on connect describes a song already in progress, so only
                # trust song starts seen after that.
                if first:
                    first = False
                    continue
                self._rock_icy_event(meta)

    async def _rock_icy_loop(self):
        while True:
            try:
                await self._rock_icy_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Rock 95.5 ICY reader error: %s", e)
            self._rock_music_until = 0.0
            await asyncio.sleep(5)

    async def _rock_in_break(self) -> bool:
        # iHeart publishes a new song ~70s after it starts, so a live song-start signal from the
        # ICY reader (valid until that song should end) overrides the history-based gap check.
        url = IHEART_URL.format(stream=ROCK_STREAM_ID)
        async with self._http.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            tracks = (await resp.json())["data"]
        if not tracks:
            raise ValueError("empty track history")
        if time.monotonic() < self._rock_music_until:
            return False
        return time.time() > tracks[0]["endTime"] + ROCK_BREAK_GRACE_S

    @tasks.loop(seconds=5)
    async def _break_monitor(self):
        """Refresh every station's commercial state, then re-check whether to move."""
        results = await asyncio.gather(*(self._station_in_break(st) for st in STATIONS), return_exceptions=True)
        for st, res in zip(STATIONS, results):
            key = st["key"]
            if isinstance(res, Exception):
                self._fails[key] += 1
                if self._fails[key] == FAILS_BEFORE_UNKNOWN:
                    log.warning("%s break check failing (%s) — treating as unknown", st["name"], res)
                    self._breaks[key] = None
                continue
            self._fails[key] = 0
            if res != self._breaks[key]:
                log.info("%s: %s", st["name"], "commercial" if res else "music")
            self._breaks[key] = res
        self._reevaluate()

    @_break_monitor.before_loop
    async def _before_break_monitor(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=5)
    async def _radio_keepalive(self):
        """Keep the radio connected and playing 24/7."""
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
