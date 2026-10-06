"""Radio players for public servers: tune to a directory station, keep it playing, and leave once
everyone has gone. The home server keeps its own station rotation (bot.py)."""
import asyncio
import ipaddress
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import discord
from discord.ext import tasks

log = logging.getLogger("public")

# Simultaneous servers. Each stream costs roughly 4-5% of a core (decoding + loudness leveling), and
# a shared-cpu-1x Fly machine only guarantees 6.25% of a core, so raise this together with the machine size.
MAX_STREAMS = int(os.getenv("MAX_PUBLIC_STREAMS", "3"))
IDLE_LEAVE_S = int(os.getenv("IDLE_LEAVE_SECONDS", "300"))     # leave after this long with no listeners
RESTART_LIMIT, RESTART_WINDOW_S = 4, 120                       # give up if a stream keeps dying
DEFAULT_VOLUME = float(os.getenv("PUBLIC_DEFAULT_VOLUME", "0.8"))
START_WAIT_S = 12   # how long a new station gets to produce audio before we call it dead

# Stations are third-party URLs, so ffmpeg is restricted to plain web protocols (no file:, no
# concat:, nothing that can read local files) and given timeouts so a dead stream can't hang.
FFMPEG_BEFORE = (
    "-protocol_whitelist http,https,tcp,tls,crypto "
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-rw_timeout 15000000 -user_agent CrueFM/1.0"
)
FFMPEG_OPTIONS = "-vn -af loudnorm=I=-20:TP=-2:LRA=9"   # level unknown stations to a comfortable volume


async def check_stream_url(url: str) -> str | None:
    """Return a user-facing problem with a stream address, or None if it is safe to open.
    Blocks anything that resolves to a private, loopback, link-local or otherwise non-public address
    so a station entry can never point the bot at internal services."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return "That isn't a valid stream address."
    if parts.scheme not in ("http", "https") or not host:
        return "That isn't a web stream address."
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal", ".lan")):
        return "That stream address isn't allowed."
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return "I couldn't reach that stream's server."
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            return "That stream address isn't allowed."
    return None


def listener_ids(channel: discord.VoiceChannel, me_id: int) -> set[int]:
    """Humans in a voice channel. Uses the channel's voice states rather than cached Member objects,
    which don't exist for people who were already connected before the bot arrived."""
    ids = set()
    for uid in channel.voice_states:
        if uid == me_id:
            continue
        member = channel.guild.get_member(uid)
        if member is not None and member.bot:
            continue
        ids.add(uid)
    return ids


class _Probe(discord.AudioSource):
    """Passes audio through and reports when the first real audio arrives."""

    def __init__(self, inner: discord.AudioSource, on_first):
        self.inner, self.on_first, self._seen = inner, on_first, False

    def read(self) -> bytes:
        data = self.inner.read()
        if data and not self._seen:
            self._seen = True
            self.on_first()
        return data

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        self.inner.cleanup()


@dataclass
class PublicPlayer:
    guild_id: int
    channel_id: int
    text_channel_id: int | None
    station: object
    url: str
    started_by: int
    volume: float = DEFAULT_VOLUME
    started_at: float = field(default_factory=time.time)
    ended: bool = False
    started: bool = False
    idle_since: float | None = None
    restarts: list = field(default_factory=list)


class PublicRadio:
    def __init__(self, bot):
        self.bot = bot
        self.players: dict[int, PublicPlayer] = {}
        self._cooldown: dict[int, float] = {}

    # ---- permissions
    def may_control(self, member: discord.Member, player: PublicPlayer | None) -> bool:
        """Anyone can start the radio when it's idle. Once it's playing, only whoever started it, a
        server manager, a lone listener, or anyone once the starter has left can change it."""
        if player is None:
            return True
        perms = member.guild_permissions
        if member.id == player.started_by or perms.manage_guild or perms.manage_channels:
            return True
        vc = member.guild.voice_client
        if not vc or not member.voice or member.voice.channel != vc.channel:
            return False
        humans = listener_ids(vc.channel, member.guild.me.id)
        return len(humans) == 1 or player.started_by not in humans

    # ---- tuning
    async def tune(self, member: discord.Member, station, text_channel_id: int | None = None) -> tuple[bool, str]:
        guild = member.guild
        voice = member.voice.channel if member.voice else None
        if voice is None:
            return False, "Join a voice channel first, then pick a station."
        now = time.monotonic()
        if now - self._cooldown.get(member.id, 0) < 4:
            return False, "Easy there, give it a few seconds before changing again."
        existing = self.players.get(guild.id)
        if existing is None and len(self.players) >= MAX_STREAMS:
            return False, "I'm busy playing in a lot of servers right now. Please try again in a few minutes."
        if not self.may_control(member, existing):
            return False, "Someone else is controlling the radio here. Join their voice channel, or ask a server manager."
        perms = voice.permissions_for(guild.me)
        if not (perms.connect and perms.speak):
            return False, f"I need **Connect** and **Speak** permission in {voice.mention}."
        self._cooldown[member.id] = now

        url = await self.bot.directory.resolve_url(station)
        problem = await check_stream_url(url)
        if problem:
            return False, problem
        vc = guild.voice_client
        try:
            if vc is None or not vc.is_connected():
                vc = await voice.connect(timeout=15, self_deaf=True)
            elif vc.channel != voice:
                await vc.move_to(voice)
        except (asyncio.TimeoutError, discord.ClientException, discord.HTTPException):
            return False, f"I couldn't join {voice.mention}. Check my permissions and try again."

        player = PublicPlayer(guild.id, voice.id, text_channel_id, station, url, member.id,
                              volume=existing.volume if existing else DEFAULT_VOLUME)
        self.players[guild.id] = player
        self._play(vc, player)
        if not await self._wait_started(player):
            # Dead link: put back what was playing before, or leave if nothing was.
            log.info("Station %s (%s) did not start in %s", station.name, url, guild.name)
            if existing:
                self.players[guild.id] = existing
                self._play(vc, existing)
                return False, f"**{station.name}** isn't responding right now, so I kept playing **{existing.station.name}**."
            await self.stop(guild, "station not responding")
            return False, f"**{station.name}** isn't responding right now. Please try another station."
        log.info("Tuned %s to %s (%s)", guild.name, station.name, station.place)
        return True, f"Now playing **{station.name}**"

    @staticmethod
    async def _wait_started(player: PublicPlayer) -> bool:
        deadline = time.monotonic() + START_WAIT_S
        while time.monotonic() < deadline:
            if player.started:
                return True
            if player.ended:
                return False
            await asyncio.sleep(0.2)
        return False

    def _play(self, vc: discord.VoiceClient, player: PublicPlayer):
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        player.ended = player.started = False
        raw = discord.FFmpegPCMAudio(player.url, before_options=FFMPEG_BEFORE, options=FFMPEG_OPTIONS)
        source = discord.PCMVolumeTransformer(
            _Probe(raw, lambda: setattr(player, "started", True)), volume=player.volume
        )
        vc.play(source, after=lambda err: self._on_end(player, err))

    @staticmethod
    def _on_end(player: PublicPlayer, error):
        if error:
            log.warning("Public stream error in guild %s: %s", player.guild_id, error)
        player.ended = True

    def set_volume(self, guild_id: int, volume: float):
        player = self.players.get(guild_id)
        if not player:
            return
        player.volume = volume
        guild = self.bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = volume

    async def stop(self, guild: discord.Guild, reason: str = "stopped"):
        self.players.pop(guild.id, None)
        vc = guild.voice_client
        if vc:
            try:
                vc.stop()
                await vc.disconnect(force=True)
            except Exception as e:
                log.warning("Error leaving voice in %s: %s", guild.name, e)
        log.info("Stopped radio in %s (%s)", guild.name, reason)

    async def shutdown(self):
        for gid in list(self.players):
            guild = self.bot.get_guild(gid)
            if guild:
                await self.stop(guild, "shutting down")

    # ---- upkeep
    async def _notify(self, player: PublicPlayer, text: str):
        channel = self.bot.get_channel(player.text_channel_id) if player.text_channel_id else None
        if channel:
            try:
                await channel.send(text)
            except discord.HTTPException:
                pass

    @tasks.loop(seconds=10)
    async def monitor(self):
        now = time.monotonic()
        for gid, player in list(self.players.items()):
            guild = self.bot.get_guild(gid)
            vc = guild.voice_client if guild else None
            if guild is None or vc is None or not vc.is_connected():
                self.players.pop(gid, None)   # kicked, disconnected, or removed from the server
                continue
            humans = listener_ids(vc.channel, guild.me.id)
            if not humans:
                player.idle_since = player.idle_since or now
                if now - player.idle_since >= IDLE_LEAVE_S:
                    await self.stop(guild, "nobody listening")
                    await self._notify(player, "👋 Everyone left, so I've stopped the radio. Use `/stations browse` to start again.")
                continue
            player.idle_since = None
            if player.ended:
                wall = time.time()
                player.restarts = [t for t in player.restarts if wall - t < RESTART_WINDOW_S]
                if len(player.restarts) >= RESTART_LIMIT:
                    await self.stop(guild, "stream keeps failing")
                    await self._notify(player, f"⚠️ **{player.station.name}** keeps dropping, so I've stopped. Try another station with `/stations browse`.")
                    continue
                player.restarts.append(wall)
                self._play(vc, player)

    @monitor.before_loop
    async def _before_monitor(self):
        await self.bot.wait_until_ready()
