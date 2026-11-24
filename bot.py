import os
import json
import asyncio
import logging
import discord
from discord.ext import commands, tasks
from aiohttp import web

from cogs.stations import DEFAULT_STATION, STATIONS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

TOKEN            = os.environ["DISCORD_TOKEN"]

STATION = DEFAULT_STATION

# Where each server's voice channel is remembered so the bot rejoins after a restart.
# Point this at a persistent disk (a Docker/Fly volume); if it can't be written the bot still runs,
# it just forgets the channel on restart.
STATE_FILE = os.getenv("STATE_FILE", "home.json")

FF_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class Home(dict):
    """guild id -> voice channel id, saved to a JSON file on every change."""

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        try:
            with open(path) as f:
                self.update({int(g): int(c) for g, c in json.load(f).items()})
            log.info("Loaded %d saved channel(s) from %s", len(self), path)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Could not read %s; starting with no saved channels", path)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({str(g): c for g, c in self.items()}, f)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not save %s; the channel won't survive a restart", self.path)

    def __setitem__(self, guild_id, channel_id):
        changed = self.get(guild_id) != channel_id
        super().__setitem__(guild_id, channel_id)
        if changed:
            self._save()

    def pop(self, guild_id, *default):
        had = guild_id in self
        value = super().pop(guild_id, *default)
        if had:
            self._save()
        return value


class CrueFM(commands.Bot):
    def __init__(self, state_file: str | None = None):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.station = STATION
        self.home = Home(state_file or STATE_FILE)  # guild id -> channel the radio stays in

    async def setup_hook(self):
        try:
            await self.load_extension("cogs.radio")
        except Exception:
            # The default station must keep playing even if the /stream add-on can't load.
            log.exception("Could not load cogs.radio; /stream is unavailable")

    async def on_ready(self):
        log.info("Ready: %s | discord.py %s", self.user, discord.__version__)
        log.info("Invite: %s", discord.utils.oauth_url(
            self.user.id,
            permissions=discord.Permissions(view_channel=True, connect=True, speak=True),
            scopes=("bot", "applications.commands"),
        ))
        await self._sync_commands()
        await self._set_presence()
        if not self.guard.is_running():
            self.guard.start()

    async def _sync_commands(self):
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash command(s)", len(synced))
        except Exception as e:
            log.error("Sync error: %s", e)

    async def _set_presence(self):
        name, _ = STATIONS[self.station]
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name=name)
        )

    @tasks.loop(seconds=20)
    async def guard(self):
        """Keep the radio in each server's chosen channel: rejoin and restart if it drops."""
        for guild_id, channel_id in list(self.home.items()):
            try:
                await self._keep(guild_id, channel_id)
            except Exception:
                log.exception("guard error in guild %s", guild_id)

    async def _keep(self, guild_id: int, channel_id: int):
        ch = self.get_channel(channel_id)
        if ch is None:  # channel deleted, or the bot was removed from the server
            log.warning("Channel %s is gone; no longer keeping the radio there", channel_id)
            self.home.pop(guild_id, None)
            return
        vc = discord.utils.get(self.voice_clients, guild=ch.guild)

        if vc is None:
            vc = await ch.connect(self_deaf=True)
            log.info("Rejoined %s", ch.name)
            await self._diagnose(ch)
        elif vc.channel and vc.channel.id != channel_id:
            self.home[guild_id] = vc.channel.id  # someone moved the bot; stay where it was put

        if not vc.is_playing() and not vc.is_paused():
            await self._play(vc)

    @guard.before_loop
    async def _wait(self):
        await self.wait_until_ready()

    async def _diagnose(self, ch):
        me = ch.guild.me
        perms = ch.permissions_for(me)
        vs = me.voice
        log.info("DIAG channel type=%s connect=%s speak=%s | server_mute=%s self_mute=%s suppress=%s",
                 type(ch).__name__, perms.connect, perms.speak,
                 vs and vs.mute, vs and vs.self_mute, vs and vs.suppress)

        if not perms.speak:
            log.warning("DIAG bot has NO SPEAK permission in %s", ch.name)

        if vs and vs.mute:
            try:
                await me.edit(mute=False)
                log.info("DIAG removed own server mute")
            except discord.Forbidden:
                log.warning("DIAG bot is SERVER MUTED and cannot unmute itself")

        if isinstance(ch, discord.StageChannel) and vs and vs.suppress:
            try:
                await me.edit(suppress=False)
                log.info("DIAG stage channel: now a speaker")
            except discord.Forbidden:
                await me.request_to_speak()
                log.warning("DIAG stage channel: requested to speak, a moderator must accept")

    async def _play(self, vc: discord.VoiceClient):
        name, url = STATIONS[self.station]
        log.info("Playing %s  %s", name, url)
        src = await discord.FFmpegOpusAudio.from_probe(url, **FF_OPTS)
        vc.play(src, after=lambda e: log.error("after: %s", e) if e else None)


def register(bot: CrueFM):
    @bot.tree.command(name="join", description="Join your voice channel and start the radio")
    @discord.app_commands.guild_only()
    async def join(interaction: discord.Interaction):
        voice = getattr(interaction.user, "voice", None)
        if not voice or not voice.channel:
            await interaction.response.send_message(
                "Join a voice channel first, then run `/join`.", ephemeral=True)
            return
        ch = voice.channel
        perms = ch.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.connect and perms.speak):
            await interaction.response.send_message(
                f"I need **View Channel**, **Connect** and **Speak** in {ch.mention}.", ephemeral=True)
            return
        await interaction.response.defer()
        vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        if vc:
            if vc.channel != ch:
                await vc.move_to(ch)
        else:
            vc = await ch.connect(self_deaf=True)
            await bot._diagnose(ch)
        bot.home[interaction.guild.id] = ch.id
        if not vc.is_playing() and not vc.is_paused():
            await bot._play(vc)
        label, _ = STATIONS[bot.station]
        await interaction.followup.send(f"Joined {ch.mention}, playing **{label}**.")

    @bot.tree.command(name="leave", description="Stop the radio and leave the voice channel")
    @discord.app_commands.guild_only()
    async def leave(interaction: discord.Interaction):
        bot.home.pop(interaction.guild.id, None)  # so the guard doesn't bring it back
        radio = bot.get_cog("Radio")
        if radio:
            radio.clear(interaction.guild.id)
        vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        if not vc:
            await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
            return
        await vc.disconnect()
        await interaction.response.send_message("Left the voice channel. Use `/join` to bring me back.")

    @bot.tree.command(name="station", description="Switch radio station")
    @discord.app_commands.choices(name=[
        discord.app_commands.Choice(name=label, value=key)
        for key, (label, _) in STATIONS.items()
    ])
    async def station(interaction: discord.Interaction,
                      name: discord.app_commands.Choice[str]):
        vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        if not vc:
            await interaction.response.send_message(
                "I'm not in a voice channel yet. Join one and use `/join` first.", ephemeral=True)
            return
        await interaction.response.defer()
        bot.station = name.value
        radio = bot.get_cog("Radio")
        if radio:
            radio.clear(interaction.guild.id)  # drop any custom /stream first
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await bot._play(vc)
        await bot._set_presence()
        label, _ = STATIONS[name.value]
        await interaction.followup.send(f"Now playing **{label}**")

    @bot.tree.command(name="nowplaying", description="What's on?")
    async def nowplaying(interaction: discord.Interaction):
        label, url = STATIONS[bot.station]
        embed = discord.Embed(title=label, description=f"`{url}`", color=0xCC0000)
        await interaction.response.send_message(embed=embed)


async def health():
    app = web.Application()
    app.router.add_get("/",       lambda _: web.Response(text="ok"))
    app.router.add_get("/health", lambda _: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()


async def main():
    await health()
    if os.getenv("PAUSED"):
        log.info("PAUSED is set; not connecting to Discord")
        await asyncio.Event().wait()
    bot = CrueFM()
    register(bot)
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
