import os
import asyncio
import io
import logging
import re
import string
import time
import urllib.parse
import xml.etree.ElementTree as ET
import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

import visuals

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
MIN_RUNWAY_S = 30   # only move to a station that has at least this much of its current song left

# Loudness leveling. The streams arrive at very different levels (measured: Q101 -16.4 LUFS,
# Rock 95.5 -8.2, The Drive -13.8) and all are hot for a voice channel, so each station gets a
# fixed gain that brings it to about -20 LUFS. /volume then scales that level.
TARGET_LUFS = -20

# Rotation order. The station that's on keeps playing until it hits a commercial break, then the
# bot moves to the next station in this list that is on music (wrapping around).
#   triton = streamtheworld stations (Triton now-playing ad cues); iheart = Rock 95.5.
STATIONS = [
    {"key": "q101", "name": "Q101", "url": RADIO_URL, "kind": "triton", "mount": "WKQXFM", "gain_db": -3.6},
    {"key": "rock", "name": "Rock 95.5", "url": ROCK_URL, "kind": "iheart", "gain_db": -11.8},
    {
        "key": "drive", "name": "97.1 The Drive", "kind": "triton", "mount": DRIVE_MOUNT, "gain_db": -6.2,
        "url": f"https://playerservices.streamtheworld.com/api/livestream-redirect/{DRIVE_MOUNT}.mp3",
    },
]
STATION_BY_KEY = {st["key"]: st for st in STATIONS}

FFMPEG_RADIO_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


BRANDING = os.getenv("BRANDING", "1") != "0"   # set the bot's icon and banner from assets/
BOT_NAME = "Crüe FM"
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
NOW_PLAYING = os.getenv("NOW_PLAYING", "1") != "0"
# Where the live Now Playing card lives: a text channel ID, or (default) the radio channel's own chat.
NOW_PLAYING_CHANNEL_ID = os.getenv("NOW_PLAYING_CHANNEL_ID") or RADIO_CHANNEL_ID
CARD_REFRESH_S = 20        # re-render the card at least this often so the equalizer stays fresh
PROMPT_COOLDOWN_S = 1800   # don't re-prompt the same person more often than this
JOIN_MUTE = os.getenv("JOIN_MUTE", "1") != "0"   # silence the radio while a joiner decides
MUTE_TIMEOUT_S = int(os.getenv("MUTE_TIMEOUT_S", "30"))  # ...but never longer than this


class JoinPrompt(discord.ui.View):
    """Per-person prompt shown when someone joins the radio channel. While it's open the radio is
    silent for everyone; either button brings it back."""

    def __init__(self, bot: "MusicBot", member: discord.Member):
        super().__init__(timeout=900)
        self.bot = bot
        self.member = member
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member.id:
            await interaction.response.send_message("This prompt is for someone else.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="👍")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="✅ Radio is back on for everyone. You can change it just for you any time: "
                    "right-click **Crüe FM** in the voice channel → **User Volume** or **Mute**.",
            embed=None, view=None,
        )
        self.stop()
        await self.bot._release(self.member, "approved")

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Okay — the radio is back on for everyone else. Discord doesn't let a bot mute "
                    "itself for just one person, so to keep it off for you: right-click **Crüe FM** "
                    "in the voice channel → **Mute** (takes 2 seconds).",
            embed=None, view=None,
        )
        self.stop()
        await self.bot._release(self.member, "dismissed")

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


def _pretty(text: str) -> str:
    """Some feeds send ALL CAPS names (The Drive, Rock 95.5 jingles); make them readable."""
    text = (text or "").strip()
    if text.isupper() and (" " in text or len(text) > 5):
        text = string.capwords(text)
    return text


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self._http: aiohttp.ClientSession | None = None
        self._current = STATIONS[0]["key"]
        self._breaks: dict[str, bool | None] = {st["key"]: None for st in STATIONS}  # None = unknown
        self._fails = {st["key"]: 0 for st in STATIONS}
        self._song_end: dict[str, float | None] = {st["key"]: None for st in STATIONS}  # epoch secs
        self._checked = False   # True once the first break check has finished
        self._now: dict[str, tuple[str, str] | None] = {st["key"]: None for st in STATIONS}  # (artist, title)
        self._rock_icy_song: tuple[str, str, float] | None = None
        self._last_presence = ""
        self.radio_volume = float(os.getenv("RADIO_VOLUME", "1.0"))  # master level, set by /volume
        self._prompted: dict[int, float] = {}  # user id -> last time they got the join prompt
        self._branding_done = False
        self._tap: visuals.SpectrumTap | None = None
        self._song_span: dict[str, tuple[float, float] | None] = {st["key"]: None for st in STATIONS}
        self._np_message: discord.Message | None = None
        self._np_key = None
        self._np_last = 0.0
        self._np_backoff = 0.0
        self._np_art: bytes | None = None
        self._art_cache: dict[tuple[str, str], bytes | None] = {}
        self._pending: dict[int, asyncio.Task] = {}  # people deciding on the prompt -> timeout timer
        self._last_switch = 0.0
        self._rock_music_until = 0.0    # monotonic deadline: ICY said a Rock 95.5 song is playing
        self._rock_task: asyncio.Task | None = None

    async def setup_hook(self):
        self._http = aiohttp.ClientSession()
        await self.load_extension("cogs.music")

        @self.tree.command(name="radio", description="Show the station and song playing now")
        async def radio(interaction: discord.Interaction):
            await interaction.response.send_message(embed=self._radio_embed())

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
                name="Crüe FM 📻" if RADIO_URL else "/play to add music",
            )
        )
        if RADIO_URL and RADIO_CHANNEL_ID:
            if not self._radio_keepalive.is_running():
                self._radio_keepalive.start()
            if not self._break_monitor.is_running():
                self._break_monitor.start()
            if self._rock_task is None or self._rock_task.done():
                self._rock_task = asyncio.create_task(self._rock_icy_loop())
            if NOW_PLAYING and NOW_PLAYING_CHANNEL_ID and not self._card_loop.is_running():
                self._card_loop.start()
        if BRANDING and not self._branding_done:
            self._branding_done = True
            asyncio.create_task(self._apply_branding())

    async def close(self):
        if self._rock_task:
            self._rock_task.cancel()
        if self._http:
            await self._http.close()
        await super().close()

    def _join_prompt_embed(self, channel_name: str, muted: bool) -> discord.Embed:
        st = STATION_BY_KEY[self._current]
        if self._breaks[self._current] is True:
            now = f"Commercial break · {st['name']}"
        else:
            song = self._song_text(self._current)
            now = f"{song} · {st['name']}" if song else st["name"]
        if muted:
            body = (
                f"**Now playing:** {now}\n\n"
                "🔇 I've muted the radio for a moment so it doesn't blast you. Talking isn't "
                "affected.\n"
                f"**Approve** → radio back on for everyone. **Dismiss** → back on for everyone "
                f"else (then right-click **Crüe FM** → **Mute** to keep it off just for you). "
                f"It also comes back on by itself in {MUTE_TIMEOUT_S} seconds."
            )
        else:
            body = (
                f"**Now playing:** {now}\n\n"
                "Too loud for you? **Right-click Crüe FM** in the voice channel and drag "
                "**User Volume** down, or choose **Mute**. That only changes what *you* hear — "
                "nobody else is affected."
            )
        return discord.Embed(
            title=f"📻 Crüe FM is playing in {channel_name}", description=body, color=discord.Color.red()
        )

    def _effective_volume(self) -> float:
        return 0.0 if self._pending else self.radio_volume

    def _apply_volume(self):
        """Mute or unmute the live stream (it keeps playing underneath, so it stays in sync)."""
        channel = self.get_channel(int(RADIO_CHANNEL_ID)) if RADIO_CHANNEL_ID else None
        vc = channel.guild.voice_client if channel else None
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = self._effective_volume()

    async def _release(self, member: discord.Member, why: str):
        timer = self._pending.pop(member.id, None)
        if timer is None:
            return
        if timer is not asyncio.current_task():
            timer.cancel()
        self._apply_volume()
        log.info("Radio %s for %s (%s)", "unmuted" if not self._pending else "still muted for others", member, why)

    async def _release_after(self, member: discord.Member):
        await asyncio.sleep(MUTE_TIMEOUT_S)
        await self._release(member, "timed out")

    async def _send_join_prompt(self, member: discord.Member, channel: discord.VoiceChannel):
        now = time.monotonic()
        last = self._prompted.get(member.id)
        if last is not None and now - last < PROMPT_COOLDOWN_S:
            return
        self._prompted[member.id] = now
        if JOIN_MUTE:
            # Mute first, before anything slow, so the joiner isn't blasted while the DM sends.
            self._pending[member.id] = asyncio.create_task(self._release_after(member))
            self._apply_volume()
        embed = self._join_prompt_embed(channel.name, JOIN_MUTE)
        view = JoinPrompt(self, member)
        sent = False
        try:
            view.message = await member.send(embed=embed, view=view)
            sent = True
            log.info("Join prompt sent to %s by DM", member)
        except discord.HTTPException:
            try:
                view.message = await channel.send(
                    content=member.mention, embed=embed, view=view, delete_after=MUTE_TIMEOUT_S
                )
                sent = True
                log.info("Join prompt for %s posted in channel chat (DMs closed)", member)
            except discord.HTTPException as e:
                log.warning("Could not send join prompt to %s: %s", member, e)
        if JOIN_MUTE and not sent:
            await self._release(member, "prompt could not be delivered")

    async def on_voice_state_update(self, member, before, after):
        if RADIO_URL and RADIO_CHANNEL_ID:
            radio_id = int(RADIO_CHANNEL_ID)
            if (
                member.id in self._pending
                and (after.channel is None or after.channel.id != radio_id)
            ):
                await self._release(member, "left the channel")
            if (
                not member.bot
                and after.channel
                and after.channel.id == radio_id
                and (before.channel is None or before.channel.id != after.channel.id)
            ):
                asyncio.create_task(self._send_join_prompt(member, after.channel))
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
        opts = {**FFMPEG_RADIO_OPTIONS, "options": f"-vn -af volume={st['gain_db']}dB"}
        self._tap = visuals.SpectrumTap(discord.FFmpegPCMAudio(st["url"], **opts))
        source = discord.PCMVolumeTransformer(self._tap, volume=self._effective_volume())
        vc.play(source, after=lambda err: log.warning("Stream ended: %s", err) if err else None)
        log.info("Radio: playing %s → %s", st["name"], st["url"])

    def _runway(self, key: str) -> float:
        """Seconds left in the song a station is playing (infinite if unknown)."""
        end = self._song_end.get(key)
        return float("inf") if end is None else end - time.time()

    def _pick_music_station(self, order: list[str]) -> str | None:
        """First station in `order` that's on music with enough of its song left, else the one with
        the most left, else None. Avoids hopping onto a station seconds before its own break."""
        good = [k for k in order if self._breaks[k] is False]
        if not good:
            return None
        for k in good:
            if self._runway(k) >= MIN_RUNWAY_S:
                return k
        return max(good, key=self._runway)

    def _reevaluate(self):
        """If the playing station is in a commercial break, move to the next one that's on music."""
        keys = [st["key"] for st in STATIONS]
        cur = self._current
        state = self._breaks[cur]
        target = cur
        if state is True:
            i = keys.index(cur)
            target = self._pick_music_station(keys[i + 1:] + keys[:i]) or cur
        elif state is None and cur != keys[0]:
            target = self._pick_music_station(keys) or keys[0]
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
            return (
                int(props["cue_time_start"]), int(props["cue_time_duration"]),
                props.get("cue_title"), props.get("track_artist_name"),
            )
        return None

    async def _triton_in_break(self, key: str, mount: str) -> bool:
        track, ad = await asyncio.gather(self._latest_cue(mount, "track"), self._latest_cue(mount, "ad"))
        if track and track[2]:
            self._now[key] = (_pretty(track[3]), _pretty(track[2]))
            self._song_end[key] = (track[0] + track[1]) / 1000 if track[1] > 0 else None
            self._song_span[key] = (track[0] / 1000, (track[0] + track[1]) / 1000) if track[1] > 0 else None
        if not ad:
            return False
        if track and track[0] >= ad[0]:
            return False
        return time.time() * 1000 < ad[0] + ad[1] + TRITON_BREAK_GRACE_MS

    async def _station_in_break(self, st: dict) -> bool:
        if st["kind"] == "triton":
            return await self._triton_in_break(st["key"], st["mount"])
        return await self._rock_in_break()

    def _rock_icy_event(self, meta: str):
        """Fast signal: iHeart tags songs song_spot M/F (with a length) and commercials/sweepers T."""
        spot = re.search(r'song_spot="(\w)"', meta)
        if not spot:
            return
        if spot.group(1) == "T":
            self._rock_music_until = 0.0
            return
        song = re.match(r"(?:StreamTitle=')?(.*?) - text=\"(.*?)\" song_spot=", meta)
        if song:
            self._rock_icy_song = (song.group(1), song.group(2), time.time())
        else:
            song = re.search(r'title="(.*?)",artist="(.*?)"', meta)
            if song:
                self._rock_icy_song = (song.group(2), song.group(1), time.time())
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
        latest = tracks[0]
        song = (latest["artist"], latest["title"])
        icy = self._rock_icy_song
        if icy and icy[2] >= latest["startTime"] - 5:
            song = (icy[0], icy[1])
        self._now["rock"] = (_pretty(song[0]), _pretty(song[1]))
        if time.monotonic() < self._rock_music_until:
            self._song_end["rock"] = time.time() + (self._rock_music_until - time.monotonic() - 5)
            self._song_span["rock"] = None
            return False
        self._song_end["rock"] = latest["endTime"]
        self._song_span["rock"] = (latest["startTime"], latest["endTime"])
        return time.time() > latest["endTime"] + ROCK_BREAK_GRACE_S

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
        if not self._checked:
            self._checked = True
            first = self._pick_music_station([st["key"] for st in STATIONS])
            if first:
                self._current = first
            log.info("Starting on %s", STATION_BY_KEY[self._current]["name"])
        self._reevaluate()
        await self._update_presence()

    def _song_text(self, key: str) -> str:
        song = self._now.get(key)
        if not song:
            return ""
        artist, title = song
        return f"{artist} – {title}" if artist else title

    async def _update_presence(self):
        """Show the playing station and song as the bot's 'Listening to' status."""
        st = STATION_BY_KEY[self._current]
        if self._breaks[self._current] is True:
            text = f"Commercial break · {st['name']}"
        else:
            song = self._song_text(self._current)
            text = f"{song} · {st['name']}" if song else st["name"]
        text = text[:128]
        if text == self._last_presence:
            return
        self._last_presence = text
        log.info("Now playing: %s", text)
        try:
            await self.change_presence(
                activity=discord.Activity(type=discord.ActivityType.listening, name=text)
            )
        except Exception as e:
            log.warning("Could not update status: %s", e)

    async def _apply_branding(self):
        """Give the bot its icon and profile banner once, skipping anything Discord already has
        (avatar changes are rate-limited, so never re-upload on every restart)."""
        try:
            user = await self.fetch_user(self.user.id)
        except discord.HTTPException as e:
            log.warning("Branding skipped (could not read the bot profile): %s", e)
            return
        if self.user.name != BOT_NAME:
            try:
                await self.user.edit(username=BOT_NAME)
                log.info("Bot name set to %s", BOT_NAME)
            except discord.HTTPException as e:
                log.warning("Could not rename the bot to %s: %s", BOT_NAME, e)
        jobs = (
            ("avatar", "icon.png", user.avatar, 512, (48, 48)),
            ("banner", "banner.png", user.banner, 1024, (68, 24)),
        )
        for field, filename, asset, px, cmp_size in jobs:
            path = os.path.join(ASSET_DIR, filename)
            if not os.path.exists(path):
                continue
            if asset is not None:
                try:
                    current = await asset.replace(size=px, format="png").read()
                    if visuals.looks_like(current, path, cmp_size):
                        log.info("Bot %s already set", field)
                        continue
                except Exception as e:
                    log.warning("Could not compare the bot %s (will set it): %s", field, e)
            try:
                with open(path, "rb") as f:
                    await self.user.edit(**{field: f.read()})
                log.info("Bot %s updated", field)
            except discord.HTTPException as e:
                log.warning("Could not set the bot %s: %s", field, e)

    def _progress(self, key: str) -> float | None:
        span = self._song_span.get(key)
        if not span or span[1] <= span[0]:
            return None
        return min(1.0, max(0.0, (time.time() - span[0]) / (span[1] - span[0])))

    async def _fetch_art(self, artist: str, title: str) -> bytes | None:
        """Album cover from Apple's public search; only accepted if the artist really matches."""
        key = (artist.lower(), title.lower())
        if key in self._art_cache:
            return self._art_cache[key]
        art = None
        try:
            q = urllib.parse.urlencode({"term": f"{artist} {title}", "entity": "song", "limit": 5})
            timeout = aiohttp.ClientTimeout(total=8)
            async with self._http.get(f"https://itunes.apple.com/search?{q}", timeout=timeout) as resp:
                results = (await resp.json(content_type=None)).get("results") or []
            want = artist.lower()
            for r in results:
                got = (r.get("artistName") or "").lower()
                if got and (want in got or got in want):
                    url = r["artworkUrl100"].replace("100x100", "600x600")
                    async with self._http.get(url, timeout=timeout) as resp:
                        art = await resp.read()
                    break
        except Exception as e:
            log.warning("Album art lookup failed for %s – %s: %s", artist, title, e)
        if len(self._art_cache) >= 60:
            self._art_cache.pop(next(iter(self._art_cache)))
        self._art_cache[key] = art
        return art

    def _card_text(self, st: dict, song_line: str, commercial: bool) -> str:
        head = f"**Commercial break** · {st['name']}" if commercial else f"**{song_line}** · {st['name']}"
        others = []
        for o in STATIONS:
            if o["key"] == st["key"]:
                continue
            state = self._breaks[o["key"]]
            if state is True:
                others.append(f"{o['name']} 📢")
            elif state is False:
                s2 = self._song_text(o["key"])
                others.append(f"{o['name']} 🎵 {s2}" if s2 else f"{o['name']} 🎵")
        return head + ("\n" + "  ·  ".join(others) if others else "")

    async def _find_card(self, channel) -> discord.Message | None:
        """After a restart, reuse the card we already posted instead of adding a new one."""
        async for m in channel.history(limit=30):
            if m.author.id == self.user.id and m.embeds and (m.embeds[0].footer.text or "").startswith(("Crüe FM", "Crue FM")):
                return m
        return None

    async def _update_card(self):
        now = time.monotonic()
        if now < self._np_backoff or not self._checked:
            return
        cur = self._current
        st = STATION_BY_KEY[cur]
        commercial = self._breaks[cur] is True
        song = self._now.get(cur)
        key = (cur, song, commercial)
        if key == self._np_key and now - self._np_last < CARD_REFRESH_S:
            return
        channel = self.get_channel(int(NOW_PLAYING_CHANNEL_ID))
        if channel is None:
            self._np_backoff = now + 300
            log.warning("Now Playing channel %s not found", NOW_PLAYING_CHANNEL_ID)
            return

        if song and not commercial:
            self._np_art = await self._fetch_art(song[0], song[1]) if song[0] else None
        title = song[1] if song else st["name"]
        artist = (song[0] if song and song[0] else "Live radio")
        note = ""
        if commercial:
            alt = self._pick_music_station([k for k in self._breaks if k != cur])
            note = f"Switching to {STATION_BY_KEY[alt]['name']}…" if alt else "Every station is in a break — hang tight"
        spectrum = list(self._tap.history)[-30:] if self._tap else []
        gif, accent = await asyncio.to_thread(
            visuals.render_banner, self._np_art, st["name"], title, artist,
            None if commercial else self._progress(cur), spectrum, commercial, note,
        )
        embed = discord.Embed(
            description=self._card_text(st, self._song_text(cur) or st["name"], commercial),
            color=discord.Color.from_rgb(*accent),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_image(url="attachment://nowplaying.gif")
        embed.set_footer(text="Crüe FM · live")

        def new_file():
            return discord.File(io.BytesIO(gif), filename="nowplaying.gif")

        try:
            if self._np_message is None:
                self._np_message = await self._find_card(channel)
            if self._np_message is not None:
                try:
                    await self._np_message.edit(embed=embed, attachments=[new_file()])
                except discord.NotFound:
                    self._np_message = None
            if self._np_message is None:
                self._np_message = await channel.send(embed=embed, file=new_file())
            self._np_key, self._np_last = key, time.monotonic()
        except discord.Forbidden:
            self._np_backoff = time.monotonic() + 600
            log.warning(
                "Can't post the Now Playing card in #%s — the bot needs View Channel, Send Messages, "
                "Embed Links, Attach Files and Read Message History there. Retrying in 10 minutes.",
                getattr(channel, "name", NOW_PLAYING_CHANNEL_ID),
            )
        except discord.HTTPException as e:
            self._np_backoff = time.monotonic() + 60
            log.warning("Now Playing card update failed: %s", e)

    @tasks.loop(seconds=5)
    async def _card_loop(self):
        try:
            await self._update_card()
        except Exception as e:
            log.warning("Now Playing card error: %s", e)

    @_card_loop.before_loop
    async def _before_card_loop(self):
        await self.wait_until_ready()

    def _radio_embed(self) -> discord.Embed:
        cur = STATION_BY_KEY[self._current]
        song = self._song_text(self._current)
        if self._breaks[self._current] is True:
            now = "Commercial break"
        else:
            now = song or "Song info unavailable"
        embed = discord.Embed(
            title=f"📻 {cur['name']}", description=f"**{now}**", color=discord.Color.red()
        )
        for st in STATIONS:
            state = self._breaks[st["key"]]
            if state is True:
                value = "📢 Commercial break"
            elif state is False:
                value = f"🎵 {self._song_text(st['key']) or 'Music'}"
            else:
                value = "❔ Status unavailable"
            marker = "▶️ " if st["key"] == self._current else ""
            embed.add_field(name=f"{marker}{st['name']}", value=value, inline=False)
        embed.set_footer(text="Switches stations when the one playing hits a commercial")
        return embed

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

        if not self._checked:
            return
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
