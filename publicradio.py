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

from store import SavedRadio, Store

log = logging.getLogger("public")

# Simultaneous servers. Each stream costs roughly 4-5% of a core (decoding + loudness leveling), and
# a shared-cpu-1x Fly machine only guarantees 6.25% of a core, so raise this together with the machine size.
MAX_STREAMS = int(os.getenv("MAX_PUBLIC_STREAMS", "3"))
IDLE_LEAVE_S = int(os.getenv("IDLE_LEAVE_SECONDS", "300"))     # leave after this long with no listeners
RESTART_LIMIT, RESTART_WINDOW_S = 4, 120                       # give up if a stream keeps dying
# New servers start quiet; each listener can raise it for themselves (right-click the bot > User Volume,
# up to 200%), and /stations volume sets it for everyone. Stations are already leveled to about -20 LUFS.
DEFAULT_VOLUME = float(os.getenv("PUBLIC_DEFAULT_VOLUME", "0.4"))
RESUME_GAP_S = 1    # pause between servers when resuming, to be gentle on Discord's voice servers
LOST_GRACE_S = 30   # how long a voice connection may stay down (Discord reconnects) before we give up
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
    lost_since: float | None = None
    idle_since: float | None = None
    restarts: list = field(default_factory=list)


class PublicRadio:
    def __init__(self, bot, store: Store | None = None):
        self.bot = bot
        self.store = store or Store()
        self.players: dict[int, PublicPlayer] = {}
        self._cooldown: dict[int, float] = {}
        self._saved: dict[int, SavedRadio] = {}     # what is stored, so joins don't need a database read
        self._areas: dict[int, tuple[str, str | None]] = {}   # listener -> (state, city) they last picked from
        self._resuming: set[int] = set()
        self._resumed = False
        self._tasks: set[asyncio.Task] = set()

    # ---- saved settings
    async def start(self):
        """Open the settings store and load what each server had playing. Never blocks the radio:
        if storage fails, everything still works, it just isn't remembered."""
        try:
            await self.store.open()
            self._saved = {row.guild_id: row for row in await self.store.all()}
            self._areas = await self.store.all_areas()
            log.info("Loaded saved radio settings for %d server(s) and %d listener area(s)", len(self._saved), len(self._areas))
        except Exception as e:
            log.error("Saved settings are unavailable (%s); the radio will work but won't be remembered", e)

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _persist(self, player: PublicPlayer):
        saved = SavedRadio(player.guild_id, player.station, player.url, player.channel_id,
                           player.text_channel_id, player.started_by, player.volume)
        self._saved[player.guild_id] = saved
        try:
            await self.store.save(saved)
        except Exception as e:
            log.warning("Could not save radio settings for server %s: %s", player.guild_id, e)

    async def _save_volume(self, guild_id: int, volume: float):
        row = self._saved.get(guild_id)
        if row:
            row.volume = volume
        try:
            await self.store.update_volume(guild_id, volume)
        except Exception as e:
            log.warning("Could not save the volume for server %s: %s", guild_id, e)

    def area_for(self, user_id: int) -> tuple[str, str | None] | None:
        return self._areas.get(user_id)

    async def remember_area(self, user_id: int, state: str, city: str | None):
        if self._areas.get(user_id) == (state, city):
            return
        self._areas[user_id] = (state, city)
        try:
            await self.store.save_area(user_id, state, city)
        except Exception as e:
            log.warning("Could not save a listener's area: %s", e)

    async def forget_area(self, user_id: int) -> bool:
        had = self._areas.pop(user_id, None) is not None
        try:
            await self.store.delete_area(user_id)
        except Exception as e:
            log.warning("Could not delete a listener's area: %s", e)
        return had

    async def forget(self, guild_id: int):
        """Drop a server's saved radio (it was stopped on purpose, the bot was removed, or it can't resume)."""
        self._saved.pop(guild_id, None)
        try:
            await self.store.delete(guild_id)
        except Exception as e:
            log.warning("Could not delete saved settings for server %s: %s", guild_id, e)

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
        if isinstance(voice, discord.StageChannel):
            return False, "I can't play in Stage channels yet. Please use a regular voice channel."
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
        await self._persist(player)
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
        self._spawn(self._save_volume(guild_id, volume))

    async def stop(self, guild: discord.Guild, reason: str = "stopped", forget: bool = True):
        """Stop and leave. By default the saved settings are dropped too, so a stopped radio stays
        stopped; a restart passes forget=False so the radio comes back."""
        self.players.pop(guild.id, None)
        if forget:
            await self.forget(guild.id)
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
                await self.stop(guild, "shutting down", forget=False)

    # ---- resuming after a restart
    async def resume_all(self):
        """Pick each server's radio back up where it left off. Servers whose channel is empty wait
        until someone joins (see on_join) rather than the bot sitting alone in a channel."""
        if self._resumed:
            return
        self._resumed = True
        for saved in list(self._saved.values()):
            try:
                await self._resume(saved)
            except Exception as e:
                log.warning("Could not resume server %s: %s", saved.guild_id, e)
            await asyncio.sleep(RESUME_GAP_S)

    async def on_join(self, member: discord.Member, channel: discord.VoiceChannel):
        saved = self._saved.get(channel.guild.id)
        if saved and saved.voice_channel_id == channel.id and channel.guild.id not in self.players:
            await self._resume(saved)

    async def _resume(self, saved: SavedRadio) -> bool:
        gid = saved.guild_id
        if gid in self.players or gid in self._resuming:
            return gid in self.players
        self._resuming.add(gid)
        try:
            return await self._resume_inner(saved)
        finally:
            self._resuming.discard(gid)

    async def _resume_inner(self, saved: SavedRadio) -> bool:
        gid = saved.guild_id
        guild = self.bot.get_guild(gid)
        if guild is None:
            if self.bot.is_ready():
                await self.forget(gid)        # the bot is no longer in that server
            return False
        channel = guild.get_channel(saved.voice_channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            await self.forget(gid)            # the channel was deleted
            return False
        if len(self.players) >= MAX_STREAMS:
            log.info("Not resuming %s yet: at the %d-server limit", guild.name, MAX_STREAMS)
            return False
        if not listener_ids(channel, guild.me.id):
            return False                      # nobody there; resume when someone joins
        perms = channel.permissions_for(guild.me)
        if not (perms.connect and perms.speak):
            log.info("Not resuming %s: missing Connect/Speak in the saved channel", guild.name)
            return False
        if await check_stream_url(saved.url):
            await self.forget(gid)
            return False
        vc = guild.voice_client
        try:
            if vc is None or not vc.is_connected():
                vc = await channel.connect(timeout=15, self_deaf=True)
            elif vc.channel != channel:
                await vc.move_to(channel)
        except (asyncio.TimeoutError, discord.ClientException, discord.HTTPException) as e:
            log.warning("Could not rejoin %s to resume: %s", guild.name, e)
            return False
        player = PublicPlayer(gid, channel.id, saved.text_channel_id, saved.station, saved.url,
                              saved.started_by, volume=saved.volume)
        self.players[gid] = player
        self._play(vc, player)
        if not await self._wait_started(player):
            fresh = await self.bot.directory.resolve_url(saved.station)   # the station may have moved its stream
            if fresh != saved.url and not await check_stream_url(fresh):
                player.url = fresh
                self._play(vc, player)
                if await self._wait_started(player):
                    await self._persist(player)
                    log.info("Resumed %s with %s (new stream address)", guild.name, saved.station.name)
                    return True
            await self.stop(guild, "could not resume")
            await self._notify(player, f"⚠️ I couldn't resume **{saved.station.name}** after restarting. Use `/stations browse` to pick a station.")
            return False
        log.info("Resumed %s with %s", guild.name, saved.station.name)
        return True

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
            if guild is None or vc is None:
                self.players.pop(gid, None)   # kicked from voice, or removed from the server: stay stopped
                await self.forget(gid)
                continue
            if not vc.is_connected():
                # Discord reconnects voice on its own after a blip; only give up if it stays down.
                player.lost_since = player.lost_since or now
                if now - player.lost_since >= LOST_GRACE_S:
                    await self.stop(guild, "voice connection lost")
                continue
            player.lost_since = None
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
