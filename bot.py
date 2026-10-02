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
RADIO_URL = os.getenv("RADIO_URL")               # stream URL to auto-play 24/7
RADIO_CHANNEL_ID = os.getenv("RADIO_CHANNEL_ID") # voice channel ID to auto-join

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

    async def setup_hook(self):
        await self.load_extension("cogs.music")
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
            self._radio_keepalive.start()

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
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
        if "entries" in data:
            data = data["entries"][0]
        return data.get("url") or (data.get("formats") or [{}])[0].get("url", url)

    @tasks.loop(seconds=20)
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
            try:
                stream_url = await self._resolve_stream_url(RADIO_URL)
                source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_RADIO_OPTIONS)
                vc.play(source)
                log.info("Radio: started stream from %s", stream_url)
            except Exception as e:
                log.error("Radio: failed to start stream: %s", e)

    @_radio_keepalive.before_loop
    async def _before_keepalive(self):
        await self.wait_until_ready()


async def main():
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN is not set. Copy .env.example to .env and add your token.")
    bot = MusicBot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
