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
RADIO_CHANNEL_ID = os.getenv("RADIO_CHANNEL_ID")
RADIO_URL = os.getenv("RADIO_URL", "https://tunein.com/radio/s30358/")  # Q101 / Crue FM

FFMPEG_RADIO_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self._resolved_stream_url: str = ""

    async def setup_hook(self):
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
            log.warning(
                "Could not sync slash commands (Missing Access). "
                "Re-invite the bot with the applications.commands scope: "
                "https://discord.com/api/oauth2/authorize"
                "?client_id=%s&permissions=3148800&scope=bot+applications.commands",
                self.application_id,
            )
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
            self._radio_keepalive.start()

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

    async def _resolve_stream_url(self, url: str) -> str:
        """Resolve a TuneIn page or playlist URL to a direct audio stream URL."""
        import re
        import aiohttp

        async def resolve_playlist(playlist_url: str, session: aiohttp.ClientSession) -> str:
            async with session.get(playlist_url, allow_redirects=True) as resp:
                text = await resp.text()
            pls_match = re.search(r"^File\d+=(.+)$", text, re.MULTILINE | re.IGNORECASE)
            if pls_match:
                return pls_match.group(1).strip()
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and line.startswith("http"):
                    return line
            return playlist_url

        tunein_match = re.search(r"tunein\.com.*?/(s\d+)", url)
        if tunein_match:
            station_id = tunein_match.group(1)
            opml_url = f"https://opml.radiotime.com/Tune.ashx?id={station_id}&render=json"
            async with aiohttp.ClientSession() as session:
                async with session.get(opml_url) as resp:
                    data = await resp.json(content_type=None)
                for item in data.get("body", []):
                    stream = item.get("url", "")
                    if stream and not stream.startswith("http://opml"):
                        if any(stream.lower().endswith(ext) or f".{ext}?" in stream.lower()
                               for ext in ("pls", "m3u", "m3u8")):
                            log.info("Radio: resolving playlist %s", stream)
                            stream = await resolve_playlist(stream, session)
                        log.info("Radio: resolved TuneIn %s → %s", station_id, stream)
                        return stream

        opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": True, "noplaylist": True}
        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
        if "entries" in data:
            entries = [e for e in (data.get("entries") or []) if e]
            if not entries:
                raise ValueError("No stream found for this URL")
            data = entries[0]
        return data.get("url") or (data.get("formats") or [{}])[0].get("url", url)

    async def _start_radio(self, vc: discord.VoiceClient):
        """Resolve Q101 stream URL and start playback."""
        if not self._resolved_stream_url:
            try:
                self._resolved_stream_url = await self._resolve_stream_url(RADIO_URL)
                log.info("Radio: resolved stream → %s", self._resolved_stream_url)
            except Exception as e:
                log.error("Radio: failed to resolve stream URL: %s", e)
                return

        if vc.is_playing() or vc.is_paused():
            vc.stop()

        try:
            source = discord.FFmpegPCMAudio(self._resolved_stream_url, **FFMPEG_RADIO_OPTIONS)
            vc.play(source, after=lambda err: self._on_stream_end(err))
            log.info("Radio: playing Q101")
        except Exception as e:
            log.error("Radio: failed to start playback: %s", e)
            self._resolved_stream_url = ""  # force re-resolve next time

    def _on_stream_end(self, error):
        if error:
            log.warning("Radio: stream ended with error: %s — will reconnect", error)
            self._resolved_stream_url = ""  # force fresh URL on next keepalive tick

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
            await self._start_radio(vc)

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
