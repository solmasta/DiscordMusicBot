import asyncio
import io
import logging
import os
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
GUILD_ID = os.getenv("GUILD_ID")     # optional: the music commands (/play etc.) only work on this server
# The voice channel the radio lives in (VOICE_CHANNEL_ID is accepted too)
RADIO_CHANNEL_ID = os.getenv("RADIO_CHANNEL_ID") or os.getenv("VOICE_CHANNEL_ID")

# One station: Q101. FFmpeg follows the streamtheworld redirect to the live stream.
STATION_NAME = "Q101"
RADIO_URL = os.getenv(
    "RADIO_URL",
    "https://playerservices.streamtheworld.com/api/livestream-redirect/WKQXFM.mp3",
)
TRITON_MOUNT = "WKQXFM"   # where the song titles for the card come from
TRITON_URL = "https://np.tritondigital.com/public/nowplaying?mountName={mount}&numberToFetch=1&eventType={event}"
STALE_SONG_S = 90         # a song whose cue ended this long ago is stale (a commercial break): show just the station
GAIN_DB = -3.6            # brings Q101 to about -20 LUFS, comfortable for a voice channel

FFMPEG_RADIO_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": f"-vn -af aresample=48000,volume={GAIN_DB}dB",
}


def opus_settings(channel) -> dict:
    """How the voice connection encodes: tuned for music, at least 128 kbps and up to the channel's own bitrate."""
    kbps = int(getattr(channel, "bitrate", 0) or 0) // 1000
    return {"bitrate": max(128, min(kbps, 384)), "signal_type": "music", "bandwidth": "full",
            "fec": True, "expected_packet_loss": 0.05}


# Branding is off by default: it renames the bot and changes its icon and banner. Set BRANDING=1 to turn it on.
BRANDING = os.getenv("BRANDING", "0") == "1"
BOT_NAME = "Crüe FM"
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
NOW_PLAYING = os.getenv("NOW_PLAYING", "1") != "0"
# Where the live Now Playing card lives: a text channel ID, or (default) the radio channel's own chat.
NOW_PLAYING_CHANNEL_ID = os.getenv("NOW_PLAYING_CHANNEL_ID") or RADIO_CHANNEL_ID
CARD_REFRESH_S = 20        # re-render the card at least this often so the equalizer stays fresh
PROMPT_COOLDOWN_S = 1800   # don't re-prompt the same person more often than this
JOIN_MUTE = os.getenv("JOIN_MUTE", "1") != "0"   # silence the radio while a joiner decides
MUTE_TIMEOUT_S = int(os.getenv("MUTE_TIMEOUT_S", "30"))  # ...but never longer than this


def clean(text: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text or ""))


def _pretty(text: str) -> str:
    """Some feeds send ALL CAPS names; make them readable."""
    text = (text or "").strip()
    if text.isupper() and (" " in text or len(text) > 5):
        text = string.capwords(text)
    return text


class JoinPrompt(discord.ui.View):
    """Per-person prompt shown when someone joins the radio channel. While it's open the radio is
    silent for everyone; either button brings it back."""

    def __init__(self, bot: "RadioBot", member: discord.Member):
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
                    f"right-click **{self.bot.user.name}** in the voice channel → **User Volume** or **Mute**.",
            embed=None, view=None,
        )
        self.stop()
        await self.bot._release(self.member, "approved")

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Okay — the radio is back on for everyone else. Discord doesn't let a bot mute "
                    "itself for just one person, so to keep it off for you: right-click "
                    f"**{self.bot.user.name}** in the voice channel → **Mute** (takes 2 seconds).",
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


class RadioBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self._http: aiohttp.ClientSession | None = None
        self._now: tuple[str, str] | None = None            # (artist, title) of the song on Q101
        self._song_span: tuple[float, float] | None = None  # epoch seconds the song started and ends
        self.radio_volume = float(os.getenv("RADIO_VOLUME", "1.0"))  # master level, set by /volume
        self._prompted: dict[int, float] = {}               # user id -> last time they got the join prompt
        self._pending: dict[int, asyncio.Task] = {}         # people deciding on the prompt -> timeout timer
        self._branding_done = False
        self._tap: visuals.SpectrumTap | None = None
        self._np_message: discord.Message | None = None
        self._np_key = None
        self._np_last = 0.0
        self._np_backoff = 0.0
        self._np_art: bytes | None = None
        self._art_cache: dict[tuple[str, str], bytes | None] = {}
        self._meta_task: asyncio.Task | None = None
        self._last_presence = ""

    async def setup_hook(self):
        self._http = aiohttp.ClientSession()
        await self.load_extension("cogs.music")

        @self.tree.command(name="radio", description="Show what Q101 is playing now")
        @discord.app_commands.guild_only()
        async def radio(interaction: discord.Interaction):
            await interaction.response.send_message(embed=self._radio_embed())

        try:
            if GUILD_ID:
                home = discord.Object(id=int(GUILD_ID))
                # The music commands (/play and friends) are for the home server only; /radio is everywhere.
                music = self.get_cog("Music")
                for cmd in (music.get_app_commands() if music else []):
                    self.tree.remove_command(cmd.name)
                    self.tree.add_command(cmd, guild=home)
                await self.tree.sync(guild=home)
            await self.tree.sync()
            log.info("Slash commands synced")
        except Exception as e:
            log.warning("Slash command sync failed (non-fatal): %s", e)

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        await self._update_presence()
        if RADIO_CHANNEL_ID:
            if not self._radio_keepalive.is_running():
                self._radio_keepalive.start()
            if self._meta_task is None or self._meta_task.done():
                self._meta_task = asyncio.create_task(self._meta_loop())
            if NOW_PLAYING and NOW_PLAYING_CHANNEL_ID and not self._card_loop.is_running():
                self._card_loop.start()
        else:
            log.warning("RADIO_CHANNEL_ID (or VOICE_CHANNEL_ID) is not set, so the radio won't join a channel")
        if BRANDING and not self._branding_done:
            self._branding_done = True
            asyncio.create_task(self._apply_branding())

    async def close(self):
        if self._meta_task:
            self._meta_task.cancel()
        if self._http:
            await self._http.close()
        await super().close()

    # ---- join prompt
    def _join_prompt_embed(self, channel_name: str, muted: bool) -> discord.Embed:
        song = self._song_text()
        now = f"{song} · {STATION_NAME}" if song else STATION_NAME
        if muted:
            body = (
                f"**Now playing:** {now}\n\n"
                "🔇 I've muted the radio for a moment so it doesn't blast you. Talking isn't "
                "affected.\n"
                f"**Approve** → radio back on for everyone. **Dismiss** → back on for everyone "
                f"else (then right-click **{self.user.name}** → **Mute** to keep it off just for you). "
                f"It also comes back on by itself in {MUTE_TIMEOUT_S} seconds."
            )
        else:
            body = (
                f"**Now playing:** {now}\n\n"
                f"Too loud for you? **Right-click {self.user.name}** in the voice channel and drag "
                "**User Volume** down, or choose **Mute**. That only changes what *you* hear — "
                "nobody else is affected."
            )
        return discord.Embed(
            title=f"📻 {STATION_NAME} is playing in {channel_name}", description=body, color=discord.Color.red()
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
        if not RADIO_CHANNEL_ID:
            return
        radio_id = int(RADIO_CHANNEL_ID)
        if member.id in self._pending and (after.channel is None or after.channel.id != radio_id):
            await self._release(member, "left the channel")
        if (
            not member.bot
            and after.channel
            and after.channel.id == radio_id
            and (before.channel is None or before.channel.id != after.channel.id)
        ):
            asyncio.create_task(self._send_join_prompt(member, after.channel))

    # ---- playing
    def _start_playing(self, vc: discord.VoiceClient):
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        self._tap = visuals.SpectrumTap(discord.FFmpegPCMAudio(RADIO_URL, **FFMPEG_RADIO_OPTIONS))
        source = discord.PCMVolumeTransformer(self._tap, volume=self._effective_volume())
        vc.play(source, after=lambda err: log.warning("Stream ended: %s", err) if err else None, **opus_settings(vc.channel))
        log.info("Radio: playing %s → %s", STATION_NAME, RADIO_URL)

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

    # ---- what's playing (song titles for the card)
    async def _latest_cue(self, mount: str, event: str):
        """Return (start_ms, duration_ms, title, artist) of the newest Triton cue of this type, or None."""
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

    async def _meta_loop(self):
        await self.wait_until_ready()
        while True:
            try:
                track = await self._latest_cue(TRITON_MOUNT, "track")
                if track and track[2]:
                    self._now = (_pretty(track[3]), _pretty(track[2]))
                    self._song_span = (track[0] / 1000, (track[0] + track[1]) / 1000) if track[1] > 0 else None
                if self._song_span and time.time() > self._song_span[1] + STALE_SONG_S:
                    self._now, self._song_span = None, None     # long past the song's end: a break, show just the station
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Song title lookup failed: %s", e)
            await asyncio.sleep(10)

    def _song_text(self) -> str:
        if not self._now:
            return ""
        artist, title = self._now
        return f"{artist} – {title}" if artist else title

    def _progress(self) -> float | None:
        span = self._song_span
        if not span or span[1] <= span[0]:
            return None
        return min(1.0, max(0.0, (time.time() - span[0]) / (span[1] - span[0])))

    async def _update_presence(self):
        """Show the station and song as the bot's 'Listening to' status."""
        song = self._song_text()
        text = (f"{song} · {STATION_NAME}" if song else STATION_NAME)[:128]
        if text == self._last_presence:
            return
        self._last_presence = text
        log.info("Now playing: %s", text)
        try:
            await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=text))
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

    # ---- the live Now Playing card
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

    async def _find_card(self, channel) -> discord.Message | None:
        """After a restart, reuse the card we already posted instead of adding a new one."""
        async for m in channel.history(limit=30):
            if m.author.id == self.user.id and m.embeds and (m.embeds[0].footer.text or "").startswith(f"{STATION_NAME} · live"):
                return m
        return None

    async def _update_card(self):
        now = time.monotonic()
        if now < self._np_backoff:
            return
        await self._update_presence()
        song = self._now
        key = song
        if key == self._np_key and now - self._np_last < CARD_REFRESH_S:
            return
        channel = self.get_channel(int(NOW_PLAYING_CHANNEL_ID))
        if channel is None:
            self._np_backoff = now + 300
            log.warning("Now Playing channel %s not found", NOW_PLAYING_CHANNEL_ID)
            return

        if song:
            self._np_art = await self._fetch_art(song[0], song[1]) if song[0] else None
        else:
            self._np_art = None
        title = song[1] if song else STATION_NAME
        artist = song[0] if song and song[0] else "Live radio"
        description = f"**{clean(self._song_text() or STATION_NAME)}** · {STATION_NAME}"
        spectrum = list(self._tap.history)[-30:] if self._tap else []
        gif, accent = await asyncio.to_thread(
            visuals.render_banner, self._np_art, STATION_NAME, title, artist, self._progress(), spectrum, False, "",
        )
        embed = discord.Embed(
            description=description,
            color=discord.Color.from_rgb(*accent),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_image(url="attachment://nowplaying.gif")
        embed.set_footer(text=f"{STATION_NAME} · live")

        def new_file():
            return discord.File(io.BytesIO(gif), filename="nowplaying.gif")

        try:
            if self._np_message is None:
                self._np_message = await self._find_card(channel)
            if self._np_message is not None:
                try:
                    await self._np_message.edit(embed=embed, attachments=[new_file()], view=None)
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
        song = self._song_text()
        return discord.Embed(
            title=f"📻 {STATION_NAME}", description=f"**{clean(song) if song else 'Song info unavailable'}**",
            color=discord.Color.red(),
        )

    async def on_app_command_error(self, interaction: discord.Interaction, error: Exception):
        msg = "Something went wrong on my end. Please try again in a moment."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass
        log.error("App command error in %s: %s", interaction.command, error, exc_info=error)


async def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set (put it in .env or set it in your shell)")
    while True:
        try:
            bot = RadioBot()
            async with bot:
                await bot.start(TOKEN)
        except discord.LoginFailure as e:
            log.error("Invalid Discord token — check DISCORD_TOKEN: %s", e)
            await asyncio.sleep(60)
        except Exception as e:
            log.error("Bot crashed, restarting in 30s: %s", e, exc_info=True)
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
