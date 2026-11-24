"""Custom stream URLs: play any http(s) audio URL (Icecast/Shoutcast/HLS/MP3/AAC/...)
into a voice channel, with auto-reconnect when the stream drops."""
import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from cogs.stations import PRESETS

log = logging.getLogger("radio")

# Live streams: reconnect aggressively, no video, no stdin interference.
FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 "
        "-reconnect_delay_max 5 -nostdin"
    ),
    "options": "-vn",
}

MAX_RECONNECTS = 5
PLAYLIST_RE = re.compile(r"\.(m3u|pls)(\?|$)", re.I)
HEADERS = {"User-Agent": "DiscordRadioBot/1.0"}


@dataclass
class Station:
    url: str            # resolved direct stream URL
    name: str
    requester: discord.abc.User


@dataclass
class RadioState:
    station: Optional[Station] = None
    volume: float = 0.5
    retries: int = 0
    text_channel: Optional[discord.abc.Messageable] = None


async def resolve_stream(session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
    """Validate the URL, unwrap .m3u/.pls playlists, return (stream_url, station_name)."""
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("URL must start with http:// or https://")

    if PLAYLIST_RE.search(url):
        async with session.get(url, headers=HEADERS) as r:
            r.raise_for_status()
            text = await r.text(errors="ignore")
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("file1="):
                line = line[6:]
            if re.match(r"^https?://", line, re.I):
                url = line
                break
        else:
            raise ValueError("No stream URL found in that playlist.")

    async with session.get(
        url, headers={**HEADERS, "Icy-MetaData": "1"},
        timeout=aiohttp.ClientTimeout(total=10, sock_read=10),
    ) as r:
        if r.status >= 400:
            raise ValueError(f"Stream returned HTTP {r.status}.")
        ctype = r.headers.get("Content-Type", "").lower()
        if "text/html" in ctype:
            raise ValueError("That URL is a web page, not an audio stream.")
        name = r.headers.get("icy-name") or ""
    return url, name.strip()


async def fetch_now_playing(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    """Read the ICY in-band metadata (current song title) from a Shoutcast/Icecast stream."""
    try:
        async with session.get(
            url, headers={**HEADERS, "Icy-MetaData": "1"},
            timeout=aiohttp.ClientTimeout(total=15, sock_read=10),
        ) as r:
            metaint = int(r.headers.get("icy-metaint", 0))
            if not metaint:
                return None
            await r.content.readexactly(metaint)
            length = (await r.content.readexactly(1))[0] * 16
            if not length:
                return None
            meta = (await r.content.readexactly(length)).decode("utf-8", "ignore")
            m = re.search(r"StreamTitle='(.*?)';", meta)
            return m.group(1).strip() or None if m else None
    except Exception:
        return None


class Radio(commands.Cog):
    stream = app_commands.Group(name="stream", description="Play any radio stream URL (/stream stop returns to the default station)", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._states: dict[int, RadioState] = {}
        self._session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        self._session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self._session:
            await self._session.close()

    def clear(self, guild_id: int):
        """Forget the custom stream so its end/reconnect logic stays out of the way."""
        self._state(guild_id).station = None

    def _state(self, guild_id: int) -> RadioState:
        return self._states.setdefault(guild_id, RadioState())

    # ------------------------------------------------------------------
    async def _ensure_voice(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        voice = getattr(interaction.user, "voice", None)
        if not voice or not voice.channel:
            await interaction.followup.send("Join a voice channel first.", ephemeral=True)
            return None
        vc = interaction.guild.voice_client
        if vc:
            if vc.channel != voice.channel:
                await vc.move_to(voice.channel)
        else:
            vc = await voice.channel.connect(self_deaf=True)
        return vc

    def _start(self, guild: discord.Guild, vc: discord.VoiceClient):
        state = self._state(guild.id)
        station = state.station
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(station.url, **FFMPEG_OPTIONS), volume=state.volume
        )

        def after(error):
            if error:
                log.error("Radio error in %s: %s", guild.name, error)
            asyncio.run_coroutine_threadsafe(self._on_end(guild, station), self.bot.loop)

        if vc.is_playing() or vc.is_paused():
            vc.stop()  # triggers a stale `after`; ignored because station differs/cleared
        vc.play(source, after=after)

    async def _on_end(self, guild: discord.Guild, ended: Station):
        state = self._state(guild.id)
        # Ignore if user stopped or switched station meanwhile.
        if state.station is not ended:
            return
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            state.station = None
            return
        if state.retries >= MAX_RECONNECTS:
            state.station = None
            if state.text_channel:
                await state.text_channel.send(f"Lost the stream **{ended.name}** and couldn't reconnect.")
            return
        state.retries += 1
        await asyncio.sleep(min(2 ** state.retries, 30))
        if state.station is ended and vc.is_connected():
            log.info("Reconnecting to %s (attempt %d)", ended.url, state.retries)
            self._start(guild, vc)

    # ------------------------------------------------------------------
    async def _station_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=n, value=u)
            for n, u in PRESETS.items() if current.lower() in n.lower()
        ][:25]

    @stream.command(name="play", description="Stream a radio URL (or pick a preset)")
    @app_commands.describe(url="Stream URL (mp3/aac/ogg/m3u/pls/HLS) or a preset")
    @app_commands.autocomplete(url=_station_autocomplete)
    async def play(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer()
        url = url.strip()
        vc = await self._ensure_voice(interaction)
        if not vc:
            return
        try:
            stream_url, name = await resolve_stream(self._session, url)
        except Exception as e:
            await interaction.followup.send(f"Couldn't open that stream: {e}", ephemeral=True)
            return

        preset_name = next((n for n, u in PRESETS.items() if u == url), None)
        state = self._state(interaction.guild.id)
        state.station = Station(stream_url, preset_name or name or stream_url, interaction.user)
        state.retries = 0
        state.text_channel = interaction.channel

        self._start(interaction.guild, vc)
        embed = discord.Embed(title="📻 Now streaming", description=f"**{state.station.name}**",
                              color=discord.Color.blurple())
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    @stream.command(name="stop", description="Stop the custom stream and go back to the default station")
    async def stop(self, interaction: discord.Interaction):
        state = self._state(interaction.guild.id)
        if not state.station:
            await interaction.response.send_message("No custom stream is playing.", ephemeral=True)
            return
        state.station = None
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        # The bot's guard loop restarts the default station within seconds.
        await interaction.response.send_message("⏹️ Custom stream stopped, back to the default station.")

    @stream.command(name="nowplaying", description="Show the station and current song")
    async def nowplaying(self, interaction: discord.Interaction):
        station = self._state(interaction.guild.id).station
        if not station:
            await interaction.response.send_message("No radio is playing.", ephemeral=True)
            return
        await interaction.response.defer()
        song = await fetch_now_playing(self._session, station.url)
        embed = discord.Embed(title="📻 " + station.name, color=discord.Color.blurple())
        embed.add_field(name="Song", value=song or "Not provided by this station", inline=False)
        embed.add_field(name="Stream", value=station.url[:1000], inline=False)
        await interaction.followup.send(embed=embed)

    @stream.command(name="volume", description="Set radio volume (1–100)")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 100]):
        state = self._state(interaction.guild.id)
        state.volume = level / 100
        vc = interaction.guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = state.volume
        await interaction.response.send_message(f"🔊 Volume set to {level}%")

    @stream.command(name="stations", description="List preset stations")
    async def stations(self, interaction: discord.Interaction):
        lines = [f"• **{n}**" for n in PRESETS]
        await interaction.response.send_message(
            "Presets (use `/stream play` and pick one, or paste any stream URL):\n" + "\n".join(lines),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Radio(bot))
