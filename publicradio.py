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

import directory as D
from library import ffmpeg_before
from store import SavedRadio, Store
from theme import Theme

log = logging.getLogger("public")

# Simultaneous servers. Each stream costs roughly 4-5% of a core (decoding + loudness leveling), and
# a shared-cpu-1x Fly machine only guarantees 6.25% of a core, so raise this together with the machine size.
MAX_STREAMS = int(os.getenv("MAX_PUBLIC_STREAMS", "3"))
# Free servers can keep the radio on this long per session; premium servers run 24/7. 0 turns the limit off.
FREE_SESSION_S = int(float(os.getenv("FREE_SESSION_HOURS", "4")) * 3600)
SESSION_WARN_S = 600   # warn this long before a free session ends
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
# Leveling for unknown stations: a good resampler (many streams are 22/44 kHz), then a smooth
# dynamic leveler that evens out loud and quiet stations without the pumping and the heavy CPU of
# loudnorm (about 6x lighter on our shared core), -3.5 dB to land near -20 LUFS like before, and a
# limiter so peaks can never clip.
LEVEL_FILTER = ("aresample=48000:resampler=soxr:precision=28,"
                "dynaudnorm=f=1000:g=21:p=0.4:m=5:s=8,volume=-3.5dB,alimiter=limit=0.85")
FFMPEG_OPTIONS = f"-vn -af {LEVEL_FILTER}"


def opus_settings(channel) -> dict:
    """How the voice connection encodes: tuned for music, at least 128 kbps and up to the channel's
    own bitrate (boosted servers allow more), with light error-correction so it doesn't eat bitrate."""
    kbps = int(getattr(channel, "bitrate", 0) or 0) // 1000
    return {"bitrate": max(128, min(kbps, 384)), "signal_type": "music", "bandwidth": "full",
            "fec": True, "expected_packet_loss": 0.05}


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
    session_start: float = field(default_factory=time.monotonic)   # when this server's radio session began (kept across station changes)
    session_warned: bool = False
    ended: bool = False
    started: bool = False
    lost_since: float | None = None
    idle_since: float | None = None
    restarts: list = field(default_factory=list)
    tracks: list = field(default_factory=list)      # songs queued behind the current one (library mode)
    track_index: int = 0
    gen: int = 0                                    # bumps on every (re)start so a stopped source's callback is ignored

    @property
    def is_library(self) -> bool:
        return bool(self.tracks)


class PublicRadio:
    def __init__(self, bot, store: Store | None = None):
        self.bot = bot
        self.store = store or Store()
        self.players: dict[int, PublicPlayer] = {}
        self._cooldown: dict[int, float] = {}
        self._saved: dict[int, SavedRadio] = {}     # what is stored, so joins don't need a database read
        self._areas: dict[int, tuple[str, str | None]] = {}   # listener -> (state, city) they last picked from
        self._themes: dict[int, Theme] = {}                   # server -> its premium theme (applied only while premium)
        self._favs: dict[int, dict[str, D.Station]] = {}      # server -> saved favorite stations
        self._panels: dict[int, tuple[int, int]] = {}         # server -> (channel, message) of its control panel
        self._resuming: set[int] = set()
        self._resumed = False
        self._tasks: set[asyncio.Task] = set()

    def slot_limit(self, guild_id: int | None) -> int:
        """How many servers may be playing when this one asks: premium servers get a few reserved extras."""
        prem = getattr(self.bot, "premium", None)
        extra = prem.extra_slots(guild_id) if prem is not None else 0
        return MAX_STREAMS + (extra if isinstance(extra, int) else 0)

    # ---- saved settings
    async def start(self):
        """Open the settings store and load what each server had playing. Never blocks the radio:
        if storage fails, everything still works, it just isn't remembered."""
        try:
            await self.store.open()
            self._saved = {row.guild_id: row for row in await self.store.all()}
            self._areas = await self.store.all_areas()
            self._panels = await self.store.all_panels()
            self._favs = await self.store.all_favorites()
            self._themes = await self.store.all_themes()
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

    def panel_for(self, guild_id: int) -> tuple[int, int] | None:
        return self._panels.get(guild_id)

    async def set_panel(self, guild_id: int, channel_id: int, message_id: int):
        self._panels[guild_id] = (channel_id, message_id)
        try:
            await self.store.save_panel(guild_id, channel_id, message_id)
        except Exception as e:
            log.warning("Could not save the control panel for server %s: %s", guild_id, e)

    async def drop_panel(self, guild_id: int):
        self._panels.pop(guild_id, None)
        try:
            await self.store.delete_panel(guild_id)
        except Exception as e:
            log.warning("Could not delete the control panel for server %s: %s", guild_id, e)

    def changed(self, guild_id: int):
        """Something about this server's radio changed: let the live panel (if it has one) redraw."""
        cog = self.bot.get_cog("Stations")
        if cog is not None and guild_id in self._panels:
            self._spawn(cog.update_panel(guild_id))

    # ---- premium themes
    def theme_for(self, guild_id: int | None) -> Theme | None:
        """The server's theme, only while it has premium (the saved theme is kept if premium lapses)."""
        theme = self._themes.get(guild_id)
        return theme if theme is not None and self.is_premium(guild_id) else None

    def saved_theme(self, guild_id: int) -> Theme:
        return self._themes.get(guild_id) or Theme(guild_id)

    async def save_theme(self, theme: Theme):
        if theme.is_empty:
            self._themes.pop(theme.guild_id, None)
        else:
            self._themes[theme.guild_id] = theme
        try:
            await (self.store.delete_theme(theme.guild_id) if theme.is_empty else self.store.save_theme(theme))
        except Exception as e:
            log.warning("Could not save a server theme: %s", e)
        self.changed(theme.guild_id)         # redraw the live panel with the new look

    # ---- favorites (3 free, many more with premium)
    def is_premium(self, guild_id: int | None) -> bool:
        prem = getattr(self.bot, "premium", None)
        return bool(prem is not None and prem.is_premium(guild_id) is True)

    def favorites_for(self, guild_id: int) -> list:
        return list(self._favs.get(guild_id, {}).values())

    def favorite_limit(self, guild_id: int) -> int:
        prem = getattr(self.bot, "premium", None)
        limit = prem.favorite_limit(guild_id) if prem is not None else None
        return limit if isinstance(limit, int) else 3

    async def add_favorite(self, guild_id: int, station) -> str | None:
        """Save a station. Returns None on success, or why it couldn't be saved."""
        favs = self._favs.setdefault(guild_id, {})
        if station.uuid in favs:
            return "That station is already in this server's favorites."
        if len(favs) >= self.favorite_limit(guild_id):
            upsell = " Premium servers can save more: see `/premium`." if getattr(getattr(self.bot, "premium", None), "enabled", False) is True else ""
            return f"This server's favorites are full ({self.favorite_limit(guild_id)}). Remove one with `/stations unfavorite`." + upsell
        favs[station.uuid] = station
        try:
            await self.store.add_favorite(guild_id, station)
        except Exception as e:
            log.warning("Could not save a favorite: %s", e)
        return None

    async def remove_favorite(self, guild_id: int, uuid: str) -> bool:
        had = self._favs.get(guild_id, {}).pop(uuid, None) is not None
        try:
            await self.store.remove_favorite(guild_id, uuid)
        except Exception as e:
            log.warning("Could not remove a favorite: %s", e)
        return had

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
    def _check_member(self, member: discord.Member, what: str):
        """Shared rules for starting anything: (voice channel, current player, None) or (None, None, why not)."""
        guild = member.guild
        voice = member.voice.channel if member.voice else None
        if voice is None:
            return None, None, f"Join a voice channel first, then pick a {what}."
        if isinstance(voice, discord.StageChannel):
            return None, None, "I can't play in Stage channels yet. Please use a regular voice channel."
        now = time.monotonic()
        if now - self._cooldown.get(member.id, 0) < 4:
            return None, None, "Easy there, give it a few seconds before changing again."
        existing = self.players.get(guild.id)
        if existing is None and len(self.players) >= self.slot_limit(guild.id):
            hint = " Premium servers get priority slots: see `/premium`." if getattr(getattr(self.bot, "premium", None), "enabled", False) is True else ""
            return None, None, "I'm busy playing in a lot of servers right now. Please try again in a few minutes." + hint
        if not self.may_control(member, existing):
            return None, None, "Someone else is controlling the radio here. Join their voice channel, or ask a server manager."
        perms = voice.permissions_for(guild.me)
        if not (perms.connect and perms.speak):
            return None, None, f"I need **Connect** and **Speak** permission in {voice.mention}."
        self._cooldown[member.id] = now
        return voice, existing, None

    async def _connect(self, guild: discord.Guild, voice):
        vc = guild.voice_client
        try:
            if vc is None or not vc.is_connected():
                vc = await voice.connect(timeout=15, self_deaf=True)
            elif vc.channel != voice:
                await vc.move_to(voice)
        except (asyncio.TimeoutError, discord.ClientException, discord.HTTPException):
            return None
        return vc

    async def tune(self, member: discord.Member, station, text_channel_id: int | None = None,
                   dead: list | None = None) -> tuple[bool, str]:
        guild = member.guild
        voice, existing, problem = self._check_member(member, "station")
        if problem:
            return False, problem

        url = await self.bot.directory.resolve_url(station)
        problem = await check_stream_url(url)
        if problem:
            if dead is not None:
                dead.append(station)
            return False, problem
        vc = await self._connect(guild, voice)
        if vc is None:
            return False, f"I couldn't join {voice.mention}. Check my permissions and try again."

        player = PublicPlayer(guild.id, voice.id, text_channel_id, station, url, member.id,
                              volume=existing.volume if existing else DEFAULT_VOLUME)
        if existing:
            player.session_start, player.session_warned = existing.session_start, existing.session_warned
        self.players[guild.id] = player
        self._play(vc, player)
        if not await self._wait_started(player):
            # Dead link: put back what was playing before, or leave if nothing was.
            log.info("Station %s (%s) did not start in %s", station.name, url, guild.name)
            if dead is not None:
                dead.append(station)
            if existing:
                self.players[guild.id] = existing
                self._play(vc, existing)
                return False, f"**{station.name}** isn't responding right now, so I kept playing **{existing.station.name}**."
            await self.stop(guild, "station not responding")
            return False, f"**{station.name}** isn't responding right now. Please try another station."
        log.info("Tuned %s to %s (%s)", guild.name, station.name, station.place)
        await self._persist(player)
        self.changed(guild.id)
        return True, f"Now playing **{station.name}**"

    async def tune_first_working(self, member: discord.Member, stations: list, text_channel_id: int | None = None,
                                 tries: int = 4) -> tuple[bool, str, object | None]:
        """Try the best stations in order until one actually plays. Only dead or unreachable stations
        are skipped; any other problem (permissions, busy, someone else in control) stops at once."""
        message = "No stations to try."
        for station in stations[:tries]:
            dead: list = []
            ok, message = await self.tune(member, station, text_channel_id, dead=dead)
            if ok:
                return True, message, station
            if not dead:
                return False, message, None
            self._cooldown.pop(member.id, None)      # our own retry shouldn't trip the "easy there" timer
        return False, "None of the top stations there are responding right now. Try another place or genre.", None

    async def play_songs(self, member: discord.Member, songs: list, text_channel_id: int | None = None) -> tuple[bool, str]:
        """Play free-licensed songs one after another, starting with the first. Not saved across a
        restart: songs are a session, unlike a station which is the server's standing radio."""
        guild = member.guild
        if not songs:
            return False, "There are no songs to play."
        voice, existing, problem = self._check_member(member, "song")
        if problem:
            return False, problem
        problem = await check_stream_url(songs[0].url)
        if problem:
            return False, problem
        vc = await self._connect(guild, voice)
        if vc is None:
            return False, f"I couldn't join {voice.mention}. Check my permissions and try again."
        player = PublicPlayer(guild.id, voice.id, text_channel_id, songs[0], songs[0].url, member.id,
                              volume=existing.volume if existing else DEFAULT_VOLUME, tracks=list(songs))
        if existing:
            player.session_start, player.session_warned = existing.session_start, existing.session_warned
        self.players[guild.id] = player
        self._play(vc, player)
        if not await self._wait_started(player):
            log.info("Song %s did not start in %s", songs[0].title, guild.name)
            if existing:
                self.players[guild.id] = existing
                self._play(vc, existing)
                return False, f"**{songs[0].title}** isn't available right now, so I kept the previous music playing."
            await self.stop(guild, "song not available")
            return False, f"**{songs[0].title}** isn't available right now. Please pick another song."
        await self.forget(guild.id)         # songs are not resumed after a restart
        log.info("Playing %d songs in %s starting with %s", len(songs), guild.name, songs[0].title)
        self.changed(guild.id)
        return True, f"Now playing **{songs[0].title}**"

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
        player.gen += 1            # before stopping, so the old source's end callback is recognised as stale
        gen = player.gen
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        player.ended = player.started = False
        before = ffmpeg_before(FFMPEG_BEFORE, player.station)
        raw = discord.FFmpegPCMAudio(player.url, before_options=before, options=FFMPEG_OPTIONS)
        source = discord.PCMVolumeTransformer(
            _Probe(raw, lambda: setattr(player, "started", True)), volume=player.volume
        )
        vc.play(source, after=lambda err: self._on_end(player, err, gen), **opus_settings(vc.channel))

    def _on_end(self, player: PublicPlayer, error, gen: int | None = None):
        """Runs on discord.py's player thread when a source stops. A source we replaced ourselves
        (a station change or a skip) reports too, so those are ignored."""
        if gen is not None and gen != player.gen:
            return
        if error:
            log.warning("Public stream error in guild %s: %s", player.guild_id, error)
        player.ended = True
        if player.is_library:
            try:
                self.bot.loop.call_soon_threadsafe(self._advance, player)
            except RuntimeError:
                pass        # the bot is shutting down

    # ---- song library: a queue of free-licensed songs played one after another
    def _advance(self, player: PublicPlayer, skipped: bool = False):
        """Move on to the next song (wrapping around). A song that never started counts as a failure."""
        if self.players.get(player.guild_id) is not player or not player.is_library:
            return
        guild = self.bot.get_guild(player.guild_id)
        vc = guild.voice_client if guild else None
        if vc is None or not vc.is_connected():
            return
        if not player.started and not skipped:
            wall = time.time()
            player.restarts = [t for t in player.restarts if wall - t < RESTART_WINDOW_S]
            player.restarts.append(wall)
            if len(player.restarts) >= RESTART_LIMIT:
                self._spawn(self.stop(guild, "songs keep failing"))
                self._spawn(self._notify(player, "⚠️ I couldn't play those songs, so I've stopped. Try another genre with `/library`."))
                return
        player.track_index = (player.track_index + 1) % len(player.tracks)
        song = player.tracks[player.track_index]
        player.station, player.url = song, song.url
        self._play(vc, player)
        self.changed(player.guild_id)

    def skip(self, guild_id: int) -> bool:
        """Skip to the next song. False if the radio isn't playing songs."""
        player = self.players.get(guild_id)
        if not player or not player.is_library:
            return False
        self._advance(player, skipped=True)
        return True

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
        if forget:
            self.changed(guild.id)

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
        if len(self.players) >= self.slot_limit(guild.id):
            log.info("Not resuming %s yet: at the %d-server limit", guild.name, self.slot_limit(guild.id))
            return False
        if not listener_ids(channel, guild.me.id) and not self.is_premium(gid):
            return False                      # nobody there; resume when someone joins (premium resumes anyway)
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
                if self.is_premium(gid):
                    continue                    # premium servers get true 24/7: stay on air with nobody listening
                player.idle_since = player.idle_since or now
                if now - player.idle_since >= IDLE_LEAVE_S:
                    await self.stop(guild, "nobody listening")
                    await self._notify(player, "👋 Everyone left, so I've stopped the radio. Use `/stations browse` to start again.")
                continue
            player.idle_since = None
            if FREE_SESSION_S and not self.is_premium(gid):
                left = player.session_start + FREE_SESSION_S - now
                if left <= 0:
                    await self.stop(guild, "free session limit")
                    await self._notify(player, f"⏱ The free plan runs the radio for {FREE_SESSION_S // 3600 or 1} hours at a time, so I've stopped. "
                                               "Start it again any time with `/stations play`, or get **Premium** for true 24/7: `/premium`.")
                    continue
                if left <= SESSION_WARN_S and not player.session_warned:
                    player.session_warned = True
                    await self._notify(player, f"⏱ Heads up: the free plan's radio session ends in about {max(1, int(left // 60))} minutes. "
                                               "**Premium** keeps it on 24/7: `/premium`.")
            if player.ended and player.is_library:
                self._advance(player)          # normally the end-of-song callback already did this
                continue
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
