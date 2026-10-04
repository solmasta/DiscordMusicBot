import os
import asyncio
import logging
import discord
import yt_dlp
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
RADIO_CHANNEL_ID = os.getenv("RADIO_CHANNEL_ID") # voice channel ID to auto-join

# Stations to rotate through — add more TuneIn embed URLs here
RADIO_STATIONS = [
    os.getenv("RADIO_URL", "https://tunein.com/radio/s30358/"),  # Crue FM
    "https://tunein.com/radio/s23558/",   # WDRV 97.1 The Drive (Classic Rock, Chicago)
    "https://tunein.com/radio/s23617/",   # WLUP 97.9 The Loop (Rock, Chicago)
    "https://tunein.com/radio/s23682/",   # WXRT 93.1 (Alt/Rock, Chicago)
    "https://tunein.com/radio/s97268/",   # Radio Metal
]
RADIO_URL = RADIO_STATIONS[0]  # kept for backward-compat checks

FFMPEG_RADIO_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self._radio_channel: discord.VoiceChannel | None = None
        self._station_index = 0
        self._current_stream_url: str = ""
        self._blank_title_streak = 0
        self._icy_no_support: set[int] = set()
        self._stream_cache: dict[int, str] = {}  # pre-resolved stream URLs per station index

    async def setup_hook(self):
        await self.load_extension("cogs.music")

        bot_ref = self

        @self.tree.command(name="nextstation", description="Skip to the next radio station immediately")
        async def nextstation(interaction: discord.Interaction):
            if not RADIO_CHANNEL_ID:
                await interaction.response.send_message("Radio mode is not active.", ephemeral=True)
                return
            channel = bot_ref.get_channel(int(RADIO_CHANNEL_ID))
            if not channel:
                await interaction.response.send_message("Radio channel not found.", ephemeral=True)
                return
            vc = channel.guild.voice_client
            if not vc or not vc.is_connected():
                await interaction.response.send_message("Bot is not in a voice channel.", ephemeral=True)
                return
            await bot_ref._play_next_station(vc)
            idx = (bot_ref._station_index - 1) % len(RADIO_STATIONS)
            await interaction.response.send_message(
                f"Skipped! Now on station **{idx + 1}/{len(RADIO_STATIONS)}**"
            )

        @self.tree.command(name="addstation", description="Add a radio station to the rotation")
        @discord.app_commands.describe(url="TuneIn station URL or direct stream URL")
        async def addstation(interaction: discord.Interaction, url: str):
            RADIO_STATIONS.append(url)
            asyncio.create_task(bot_ref._prefetch_all_stations())
            await interaction.response.send_message(
                f"Added station **#{len(RADIO_STATIONS)}**: `{url}`\nTotal in rotation: {len(RADIO_STATIONS)}"
            )

        guild = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
        if guild:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (may take up to 1 hour)")

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        status = "Crue FM — Motley Crue Inc 📻" if RADIO_URL else "/play to add music"
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=status,
            )
        )
        if RADIO_URL and RADIO_CHANNEL_ID:
            asyncio.create_task(self._prefetch_all_stations())
            self._radio_keepalive.start()
            self._station_rotator.start()
            self._icy_monitor.start()

    async def on_voice_state_update(self, member, before, after):
        # Don't auto-disconnect when 24/7 radio mode is active
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

    async def _resolve_stream_url(self, url: str) -> str:
        """Resolve a URL to a direct audio stream.
        Handles TuneIn station IDs via the OPML API, PLS/M3U playlists, falls back to yt-dlp."""
        import re
        import aiohttp

        async def resolve_playlist(playlist_url: str, session: aiohttp.ClientSession) -> str:
            """Follow PLS or M3U playlist to the first direct stream URL."""
            async with session.get(playlist_url, allow_redirects=True) as resp:
                text = await resp.text()
            # PLS format: File1=http://...
            pls_match = re.search(r"^File\d+=(.+)$", text, re.MULTILINE | re.IGNORECASE)
            if pls_match:
                return pls_match.group(1).strip()
            # M3U format: first non-comment line starting with http
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and line.startswith("http"):
                    return line
            return playlist_url

        # TuneIn station: extract ID and use OPML API
        tunein_match = re.search(r"tunein\.com.*?/(s\d+)", url)
        if tunein_match:
            station_id = tunein_match.group(1)
            opml_url = f"https://opml.radiotime.com/Tune.ashx?id={station_id}&render=json"
            async with aiohttp.ClientSession() as session:
                async with session.get(opml_url) as resp:
                    data = await resp.json(content_type=None)
                # body contains list of stream options; pick first playable one
                for item in data.get("body", []):
                    stream = item.get("url", "")
                    if stream and not stream.startswith("http://opml"):
                        # If it's a playlist file, resolve it further
                        if any(stream.lower().endswith(ext) or f".{ext}?" in stream.lower()
                               for ext in ("pls", "m3u", "m3u8")):
                            log.info("Radio: resolving playlist %s", stream)
                            stream = await resolve_playlist(stream, session)
                        log.info("Radio: resolved TuneIn %s → %s", station_id, stream)
                        return stream

        # Fallback: use yt-dlp
        opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True, "noplaylist": True}
        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
        if "entries" in data:
            data = data["entries"][0]
        return data.get("url") or (data.get("formats") or [{}])[0].get("url", url)

    async def _prefetch_all_stations(self):
        """Resolve and cache stream URLs for all stations in the background."""
        for i, station_url in enumerate(RADIO_STATIONS):
            if i not in self._stream_cache:
                try:
                    url = await self._resolve_stream_url(station_url)
                    self._stream_cache[i] = url
                    log.info("Prefetch: station %d cached → %s", i, station_url)
                except Exception as e:
                    log.warning("Prefetch: station %d failed: %s", i, e)

    async def _play_next_station(self, vc: discord.VoiceClient):
        """Stop current stream and start the next station using pre-cached URL."""
        idx = self._station_index % len(RADIO_STATIONS)
        self._station_index += 1

        # Use cached URL for instant switch, fall back to live resolve if missing
        stream_url = self._stream_cache.get(idx)
        if not stream_url:
            try:
                stream_url = await self._resolve_stream_url(RADIO_STATIONS[idx])
            except Exception as e:
                log.error("Radio: failed to resolve station %d: %s", idx, e)
                return

        if vc.is_playing() or vc.is_paused():
            vc.stop()

        try:
            self._current_stream_url = stream_url
            self._blank_title_streak = 0
            source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_RADIO_OPTIONS)
            vc.play(source)
            log.info("Radio: switched to station %d (cached=%s)", idx, idx in self._stream_cache)
        except Exception as e:
            log.error("Radio: failed to play station %d: %s", idx, e)
            return

        # Refresh this station's cache in the background for next rotation
        asyncio.create_task(self._refresh_cache(idx))

    async def _refresh_cache(self, idx: int):
        """Re-resolve and update cache for one station after it's been played."""
        try:
            url = await self._resolve_stream_url(RADIO_STATIONS[idx])
            self._stream_cache[idx] = url
            log.info("Cache refreshed: station %d", idx)
        except Exception as e:
            self._stream_cache.pop(idx, None)
            log.warning("Cache refresh failed for station %d: %s", idx, e)

    @tasks.loop(seconds=5)
    async def _radio_keepalive(self):
        """Keep the radio stream alive 24/7."""
        channel = self.get_channel(int(RADIO_CHANNEL_ID))
        if not channel or not isinstance(channel, discord.VoiceChannel):
            log.warning("RADIO_CHANNEL_ID %s not found or not a voice channel", RADIO_CHANNEL_ID)
            return

        vc = channel.guild.voice_client

        # Connect if not connected
        if not vc or not vc.is_connected():
            try:
                vc = await channel.connect(timeout=10.0, reconnect=True)
                log.info("Radio: connected to %s", channel.name)
            except Exception as e:
                log.error("Radio: failed to connect: %s", e)
                return

        # Move to correct channel if somehow in wrong one
        if vc.channel != channel:
            await vc.move_to(channel)

        # Start playing if not already
        if not vc.is_playing() and not vc.is_paused():
            await self._play_next_station(vc)

    @tasks.loop(minutes=30)
    async def _station_rotator(self):
        """Fallback: rotate station every 30 min for ICY-incompatible stations."""
        channel = self.get_channel(int(RADIO_CHANNEL_ID))
        if not channel:
            return
        vc = channel.guild.voice_client
        if vc and vc.is_connected():
            log.info("Radio: 30-min fallback rotation")
            await self._play_next_station(vc)

    @_station_rotator.before_loop
    async def _before_rotator(self):
        await self.wait_until_ready()
        await asyncio.sleep(30 * 60)  # fallback: first rotation after 30 min

    async def _fetch_icy_title(self, stream_url: str) -> str | None:
        """Return the current StreamTitle from an ICY stream, or None if unsupported."""
        import re
        import aiohttp
        headers = {"Icy-MetaData": "1", "User-Agent": "Mozilla/5.0"}
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession() as session:
                async with session.get(stream_url, headers=headers, timeout=timeout) as resp:
                    metaint = int(resp.headers.get("icy-metaint", 0))
                    if not metaint:
                        return None  # stream doesn't support ICY metadata
                    await resp.content.readexactly(metaint)
                    length_byte = await resp.content.readexactly(1)
                    meta_length = length_byte[0] * 16
                    if meta_length == 0:
                        return ""
                    meta_bytes = await resp.content.readexactly(meta_length)
                    meta_str = meta_bytes.decode("utf-8", errors="ignore").rstrip("\x00")
                    match = re.search(r"StreamTitle='([^']*)'", meta_str)
                    return match.group(1).strip() if match else ""
        except Exception:
            return None

    @tasks.loop(seconds=10)
    async def _icy_monitor(self):
        """Rotate station when ICY StreamTitle is blank (commercial break)."""
        if not self._current_stream_url:
            return
        channel = self.get_channel(int(RADIO_CHANNEL_ID))
        if not channel:
            return
        vc = channel.guild.voice_client
        if not vc or not vc.is_playing():
            return

        # Skip check if current station is known to not support ICY
        current_idx = (self._station_index - 1) % len(RADIO_STATIONS)
        if current_idx in self._icy_no_support:
            return

        title = await self._fetch_icy_title(self._current_stream_url)
        if title is None:
            # Mark this station as ICY-incompatible so we stop polling it
            self._icy_no_support.add(current_idx)
            log.info("ICY: station %d has no ICY support — using time-based rotation", current_idx)
            return

        if title == "":
            self._blank_title_streak += 1
            log.info("ICY: blank title streak=%d", self._blank_title_streak)
            if self._blank_title_streak >= 2:  # 2 × 10s = 20s blank = commercial
                log.info("ICY: commercial detected — rotating station immediately")
                await self._play_next_station(vc)
        else:
            self._blank_title_streak = 0
            log.info("ICY: now playing — %s", title)

    @_icy_monitor.before_loop
    async def _before_icy_monitor(self):
        await self.wait_until_ready()
        await asyncio.sleep(60)  # give the stream 60s to settle before monitoring

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


async def health_server():
    """Minimal HTTP server so Render's free web service tier stays alive."""
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health server listening on port %d", port)


async def main():
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN is not set. Copy .env.example to .env and add your token.")
    bot = MusicBot()
    async with bot:
        await asyncio.gather(
            bot.start(TOKEN),
            health_server(),
        )


if __name__ == "__main__":
    asyncio.run(main())
