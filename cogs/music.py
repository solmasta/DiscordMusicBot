import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("music")

# yt-dlp options — extract audio only, no playlist by default
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
    "cookiefile": None,
    "postprocessors": [],
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -filter:a 'volume=0.5'",
}

URL_RE = re.compile(r"https?://\S+")


@dataclass
class Track:
    url: str           # original user-supplied URL / search query
    stream_url: str    # direct audio stream URL from yt-dlp
    title: str
    duration: int      # seconds
    thumbnail: Optional[str]
    requester: discord.Member

    @property
    def duration_str(self) -> str:
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@dataclass
class GuildState:
    queue: deque = field(default_factory=deque)
    current: Optional[Track] = None
    loop: bool = False          # loop current track
    loop_queue: bool = False    # loop entire queue
    volume: float = 0.5
    text_channel: Optional[discord.TextChannel] = None


async def extract_info(url: str) -> dict:
    """Run yt-dlp in a thread pool to avoid blocking the event loop."""
    loop = asyncio.get_running_loop()
    opts = dict(YTDL_OPTIONS)
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
    # If a playlist was returned, grab first entry
    if "entries" in data:
        data = data["entries"][0]
    return data


def build_track(data: dict, requester: discord.Member, original_url: str) -> Track:
    formats = data.get("formats") or [{}]
    stream_url = data.get("url") or formats[0].get("url", "")
    return Track(
        url=original_url,
        stream_url=stream_url,
        title=data.get("title", "Unknown"),
        duration=data.get("duration", 0) or 0,
        thumbnail=data.get("thumbnail"),
        requester=requester,
    )


def make_ffmpeg_source(track: Track) -> discord.FFmpegPCMAudio:
    return discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTIONS)


def now_playing_embed(track: Track) -> discord.Embed:
    embed = discord.Embed(
        title="Now Playing",
        description=f"**[{track.title}]({track.url})**",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Duration", value=track.duration_str, inline=True)
    embed.add_field(name="Requested by", value=track.requester.mention, inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._states: dict[int, GuildState] = {}
        self._reconnect_task.start()

    def cog_unload(self):
        self._reconnect_task.cancel()

    def _get_state(self, guild_id: int) -> GuildState:
        if guild_id not in self._states:
            self._states[guild_id] = GuildState()
        return self._states[guild_id]

    # ------------------------------------------------------------------
    # Background task: keep voice connection alive (reconnect if dropped)
    # ------------------------------------------------------------------
    @tasks.loop(seconds=30)
    async def _reconnect_task(self):
        for guild in self.bot.guilds:
            vc = guild.voice_client
            state = self._states.get(guild.id)
            if not state or not state.current:
                continue
            if vc and not vc.is_connected():
                log.warning("Reconnecting to voice in %s", guild.name)
                try:
                    await vc.disconnect(force=True)
                    # The after-callback will handle restarting playback
                except Exception:
                    pass

    @_reconnect_task.before_loop
    async def _before_reconnect(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Internal playback helpers
    # ------------------------------------------------------------------
    async def _play_next(self, guild: discord.Guild):
        state = self._get_state(guild.id)
        vc = guild.voice_client

        if not vc or not vc.is_connected():
            state.current = None
            return

        if state.loop and state.current:
            # Re-fetch stream URL (they expire) and replay same track
            try:
                data = await extract_info(state.current.url)
                state.current = build_track(data, state.current.requester, state.current.url)
            except Exception as e:
                log.error("Failed to re-fetch stream for loop: %s", e)
                state.current = None
                return
        elif state.queue:
            state.current = state.queue.popleft()
            if state.loop_queue:
                state.queue.append(state.current)  # put it back at end
        else:
            state.current = None
            if state.text_channel:
                await state.text_channel.send("Queue finished. Add more songs with `/play`!")
            return

        try:
            source = make_ffmpeg_source(state.current)
        except Exception as e:
            log.error("FFmpeg source error: %s", e)
            await self._play_next(guild)
            return

        def after_play(error):
            if error:
                log.error("Playback error in %s: %s", guild.name, error)
            asyncio.run_coroutine_threadsafe(self._play_next(guild), self.bot.loop)

        vc.play(source, after=after_play)

        if state.text_channel and not state.loop:
            try:
                await state.text_channel.send(embed=now_playing_embed(state.current))
            except Exception:
                pass

    async def _ensure_voice(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        """Join the user's voice channel if not already connected."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("You need to be in a voice channel first.", ephemeral=True)
            return None

        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client

        if vc:
            if vc.channel != channel:
                await vc.move_to(channel)
        else:
            try:
                vc = await channel.connect(timeout=10.0, reconnect=True)
            except asyncio.TimeoutError:
                await interaction.followup.send("Couldn't connect to your voice channel. Try again.", ephemeral=True)
                return None

        return vc

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------
    @app_commands.command(name="play", description="Play a song from a URL or search YouTube")
    @app_commands.describe(query="YouTube/SoundCloud URL, direct audio URL, or search query")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        state = self._get_state(interaction.guild_id)
        state.text_channel = interaction.channel

        vc = await self._ensure_voice(interaction)
        if not vc:
            return

        await interaction.followup.send(f"Searching for `{query}`...")

        try:
            data = await extract_info(query)
        except yt_dlp.utils.DownloadError as e:
            await interaction.followup.send(f"Could not find or download: `{e}`")
            return
        except Exception as e:
            await interaction.followup.send(f"Error: `{e}`")
            return

        track = build_track(data, interaction.user, query)

        if vc.is_playing() or vc.is_paused():
            state.queue.append(track)
            embed = discord.Embed(
                title="Added to Queue",
                description=f"**[{track.title}]({track.url})**",
                color=discord.Color.green(),
            )
            embed.add_field(name="Duration", value=track.duration_str, inline=True)
            embed.add_field(name="Position", value=str(len(state.queue)), inline=True)
            embed.add_field(name="Requested by", value=interaction.user.mention, inline=True)
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)
            await interaction.followup.send(embed=embed)
        else:
            state.current = track
            source = make_ffmpeg_source(track)

            def after_play(error):
                if error:
                    log.error("Playback error: %s", error)
                asyncio.run_coroutine_threadsafe(self._play_next(interaction.guild), self.bot.loop)

            vc.play(source, after=after_play)
            await interaction.followup.send(embed=now_playing_embed(track))

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        vc.stop()  # triggers after_play → _play_next
        await interaction.response.send_message("Skipped.")

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            await interaction.response.send_message("Nothing to pause.", ephemeral=True)
            return
        vc.pause()
        await interaction.response.send_message("Paused.")

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_paused():
            await interaction.response.send_message("Not paused.", ephemeral=True)
            return
        vc.resume()
        await interaction.response.send_message("Resumed.")

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        state = self._get_state(interaction.guild_id)
        vc = interaction.guild.voice_client
        state.queue.clear()
        state.current = None
        state.loop = False
        state.loop_queue = False
        if vc and vc.is_playing():
            vc.stop()
        await interaction.response.send_message("Stopped and queue cleared.")

    @app_commands.command(name="queue", description="Show the current queue")
    async def queue_cmd(self, interaction: discord.Interaction):
        state = self._get_state(interaction.guild_id)
        embed = discord.Embed(title="Queue", color=discord.Color.blurple())

        if state.current:
            embed.add_field(
                name="Now Playing",
                value=f"**[{state.current.title}]({state.current.url})** `{state.current.duration_str}`",
                inline=False,
            )

        if state.queue:
            lines = []
            for i, track in enumerate(list(state.queue)[:15], start=1):
                lines.append(f"`{i}.` [{track.title}]({track.url}) `{track.duration_str}`")
            embed.add_field(name=f"Up Next ({len(state.queue)} tracks)", value="\n".join(lines), inline=False)
            if len(state.queue) > 15:
                embed.set_footer(text=f"...and {len(state.queue) - 15} more")
        elif not state.current:
            embed.description = "Queue is empty. Use `/play` to add songs!"

        flags = []
        if state.loop:
            flags.append("Loop: ON")
        if state.loop_queue:
            flags.append("Loop Queue: ON")
        if flags:
            embed.set_footer(text=" | ".join(flags))

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="Show the currently playing song")
    async def nowplaying(self, interaction: discord.Interaction):
        state = self._get_state(interaction.guild_id)
        if not state.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        await interaction.response.send_message(embed=now_playing_embed(state.current))

    @app_commands.command(name="volume", description="Set playback volume (1–100)")
    @app_commands.describe(level="Volume level between 1 and 100")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 100]):
        state = self._get_state(interaction.guild_id)
        state.volume = level / 100
        vc = interaction.guild.voice_client
        if vc and vc.source:
            # Wrap in PCMVolumeTransformer if not already
            if isinstance(vc.source, discord.PCMVolumeTransformer):
                vc.source.volume = state.volume
        await interaction.response.send_message(f"Volume set to **{level}%**.")

    @app_commands.command(name="loop", description="Toggle loop for the current track")
    async def loop(self, interaction: discord.Interaction):
        state = self._get_state(interaction.guild_id)
        state.loop = not state.loop
        if state.loop:
            state.loop_queue = False  # can't have both
        status = "enabled" if state.loop else "disabled"
        await interaction.response.send_message(f"Loop {status}.")

    @app_commands.command(name="loopqueue", description="Toggle loop for the entire queue")
    async def loopqueue(self, interaction: discord.Interaction):
        state = self._get_state(interaction.guild_id)
        state.loop_queue = not state.loop_queue
        if state.loop_queue:
            state.loop = False
        status = "enabled" if state.loop_queue else "disabled"
        await interaction.response.send_message(f"Queue loop {status}.")

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction):
        import random
        state = self._get_state(interaction.guild_id)
        if not state.queue:
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return
        q = list(state.queue)
        random.shuffle(q)
        state.queue = deque(q)
        await interaction.response.send_message("Queue shuffled.")

    @app_commands.command(name="remove", description="Remove a song from the queue by position")
    @app_commands.describe(position="Position in queue (1 = next up)")
    async def remove(self, interaction: discord.Interaction, position: int):
        state = self._get_state(interaction.guild_id)
        if position < 1 or position > len(state.queue):
            await interaction.response.send_message("Invalid position.", ephemeral=True)
            return
        q = list(state.queue)
        removed = q.pop(position - 1)
        state.queue = deque(q)
        await interaction.response.send_message(f"Removed **{removed.title}** from queue.")

    @app_commands.command(name="join", description="Join your voice channel")
    async def join(self, interaction: discord.Interaction):
        await interaction.response.defer()
        vc = await self._ensure_voice(interaction)
        if vc:
            await interaction.followup.send(f"Joined **{vc.channel.name}**.")

    @app_commands.command(name="leave", description="Leave the voice channel and clear queue")
    async def leave(self, interaction: discord.Interaction):
        state = self._get_state(interaction.guild_id)
        vc = interaction.guild.voice_client
        state.queue.clear()
        state.current = None
        if vc:
            await vc.disconnect()
            await interaction.response.send_message("Disconnected.")
        else:
            await interaction.response.send_message("Not in a voice channel.", ephemeral=True)

    @app_commands.command(name="move", description="Move a song to a different queue position")
    @app_commands.describe(from_pos="Current position", to_pos="Target position")
    async def move(self, interaction: discord.Interaction, from_pos: int, to_pos: int):
        state = self._get_state(interaction.guild_id)
        q = list(state.queue)
        if not (1 <= from_pos <= len(q)) or not (1 <= to_pos <= len(q)):
            await interaction.response.send_message("Invalid position(s).", ephemeral=True)
            return
        track = q.pop(from_pos - 1)
        q.insert(to_pos - 1, track)
        state.queue = deque(q)
        await interaction.response.send_message(f"Moved **{track.title}** to position {to_pos}.")

    @app_commands.command(name="clearqueue", description="Clear all songs from the queue (keeps current song playing)")
    async def clearqueue(self, interaction: discord.Interaction):
        state = self._get_state(interaction.guild_id)
        state.queue.clear()
        await interaction.response.send_message("Queue cleared.")

    @app_commands.command(name="help", description="Show all music bot commands")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📻 Crue FM — Commands",
            description="Motley Crue Inc's 24/7 radio bot. Play music from YouTube, SoundCloud, direct URLs and more.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="▶️ Playback",
            value=(
                "`/play <url or search>` — Play a song or add to queue\n"
                "`/pause` — Pause the current song\n"
                "`/resume` — Resume playback\n"
                "`/stop` — Stop and clear the queue\n"
                "`/skip` — Skip to the next song\n"
                "`/volume <1–100>` — Set the volume"
            ),
            inline=False,
        )
        embed.add_field(
            name="📋 Queue",
            value=(
                "`/queue` — Show the current queue\n"
                "`/nowplaying` — Show what's playing now\n"
                "`/shuffle` — Shuffle the queue\n"
                "`/remove <position>` — Remove a song from the queue\n"
                "`/move <from> <to>` — Reorder a song in the queue\n"
                "`/clearqueue` — Clear queue (keeps current song)"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔁 Loop",
            value=(
                "`/loop` — Loop the current track\n"
                "`/loopqueue` — Loop the entire queue"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔊 Voice",
            value=(
                "`/join` — Pull bot into your voice channel\n"
                "`/leave` — Disconnect bot and clear queue"
            ),
            inline=False,
        )
        embed.add_field(
            name="📻 Radio",
            value=(
                "`/nextstation` — Skip to the next radio station\n"
                "`/addstation <url>` — Add a TuneIn or stream URL to the rotation"
            ),
            inline=False,
        )
        embed.set_footer(text="Crue FM 📻 — Motley Crue Inc's station. /play works with YouTube, SoundCloud, or just a song name!")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
