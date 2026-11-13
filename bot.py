import os
import asyncio
import logging
import discord
from discord.ext import commands, tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("radiobot")

TOKEN = os.getenv("DISCORD_TOKEN")
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID", "0"))

# Free public streams of Chicago rock and hip-hop/R&B stations (each checked for audio when added; stations can
# change or retire their streams, so if one goes silent, update its url here).
STATIONS = {
    "q101": {"name": "Q101", "emoji": "🎶", "genre": "Rock", "detail": "WKQX 101.1 · Alternative rock",
             "url": "https://playerservices.streamtheworld.com/api/livestream-redirect/WKQXFM.mp3"},
    "drive": {"name": "97.1 The Drive", "emoji": "🚗", "genre": "Rock", "detail": "WDRV · Classic rock",
              "url": "https://playerservices.streamtheworld.com/api/livestream-redirect/WDRVFM.mp3"},
    "loop": {"name": "97.9 The Loop", "emoji": "🔁", "genre": "Rock", "detail": "WLUP · Classic rock",
             "url": "https://playerservices.streamtheworld.com/api/livestream-redirect/WLUPFM.mp3"},
    "rock955": {"name": "Rock 95.5", "emoji": "🎸", "genre": "Rock", "detail": "WCHI · Rock",
                "url": "https://stream.revma.ihrhls.com/zc857"},
    "wxrt": {"name": "93.1 WXRT", "emoji": "📻", "genre": "Rock", "detail": "WXRT · Alternative / indie rock",
             "url": "https://prod-98-94-96-30.amperwave.net/audacy-wxrtfmaac-hlsc.m3u8?source=TuneIn&lat=41.8874&long=-87.6318"},
    "wgci": {"name": "107.5 WGCI", "emoji": "🎤", "genre": "Hip hop / R&B", "detail": "WGCI · Hip hop and R&B",
             "url": "https://stream.revma.ihrhls.com/zc841"},
    "v103": {"name": "V103", "emoji": "🔥", "genre": "Hip hop / R&B", "detail": "WVAZ 102.7 · R&B and hip hop",
             "url": "https://stream.revma.ihrhls.com/zc865"},
    "wsrb": {"name": "WSRB 106.3", "emoji": "🎧", "genre": "Hip hop / R&B", "detail": "WSRB · R&B, soul and hip hop",
             "url": "https://ais-sa8.cdnstream1.com/phvh2qaosfo/c2ebyqqdqjx"},
}
DEFAULT_STATION = os.getenv("DEFAULT_STATION", "q101")
if DEFAULT_STATION not in STATIONS:
    DEFAULT_STATION = "q101"

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class RadioBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.current = DEFAULT_STATION

    async def setup_hook(self):
        self.add_view(StationView(self))     # the dropdown keeps working after a restart

    def voice_client(self):
        return discord.utils.get(self.voice_clients)

    async def update_presence(self):
        st = STATIONS[self.current]
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening, name=f"{st['emoji']} {st['name']}"))

    async def switch(self, key: str) -> bool:
        vc = self.voice_client()
        if key not in STATIONS or vc is None:
            return False
        self.current = key
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        self._start_stream(vc)
        await self.update_presence()
        return True

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        await self.update_presence()
        if not self.watchdog.is_running():
            self.watchdog.start()
        await self.tree.sync()

    @tasks.loop(seconds=30)
    async def watchdog(self):
        try:
            channel = self.get_channel(VOICE_CHANNEL_ID) or await self.fetch_channel(VOICE_CHANNEL_ID)
            vc = discord.utils.get(self.voice_clients, guild=channel.guild)
            if vc is None:
                vc = await channel.connect()
                log.info("Joined %s", channel.name)
            if not vc.is_playing() and not vc.is_paused():
                self._start_stream(vc)
        except Exception as e:
            log.error("Watchdog error: %s", e)

    @watchdog.before_loop
    async def before_watchdog(self):
        await self.wait_until_ready()

    def _start_stream(self, vc: discord.VoiceClient):
        st = STATIONS[self.current]
        log.info("Streaming %s (%s)", st["name"], st["url"])
        source = discord.FFmpegPCMAudio(st["url"], **FFMPEG_OPTIONS)
        vc.play(
            source,
            after=lambda e: log.error("Stream error: %s", e) if e else None,
        )


class StationSelect(discord.ui.Select):
    def __init__(self, bot: "RadioBot"):
        self.bot = bot
        super().__init__(
            custom_id="station_select",
            placeholder="Pick a station…",
            options=[discord.SelectOption(label=f"{st['name']} · {st['genre']}", value=key, emoji=st["emoji"],
                                          description=st["detail"], default=(key == bot.current))
                     for key, st in STATIONS.items()],
        )

    async def callback(self, interaction: discord.Interaction):
        vc = self.bot.voice_client()
        member = interaction.user
        if vc is None:
            await interaction.response.send_message("The radio isn't in a voice channel right now.", ephemeral=True)
            return
        if not getattr(member, "voice", None) or member.voice.channel != vc.channel:
            await interaction.response.send_message(f"Join {vc.channel.mention} to change the station.", ephemeral=True)
            return
        key = self.values[0]
        await self.bot.switch(key)
        st = STATIONS[key]
        await interaction.response.send_message(f"{st['emoji']} **{st['name']}** · {st['detail']} (picked by {member.mention})")


class StationView(discord.ui.View):
    def __init__(self, bot: "RadioBot"):
        super().__init__(timeout=None)
        self.add_item(StationSelect(bot))


def setup_commands(bot: RadioBot):
    @bot.tree.command(name="radio", description="Pick a Chicago rock or hip-hop station from a drop-down")
    async def radio_cmd(interaction: discord.Interaction):
        st = STATIONS[bot.current]
        embed = discord.Embed(title="📻 Chicago radio", color=0xCC0000,
                              description=f"Now playing: {st['emoji']} **{st['name']}** · {st['detail']}\n\nPick a station below "
                                          "(you need to be in the radio's voice channel).")
        await interaction.response.send_message(embed=embed, view=StationView(bot))

    @bot.tree.command(name="nowplaying", description="Show the current station")
    async def nowplaying_cmd(interaction: discord.Interaction):
        st = STATIONS[bot.current]
        embed = discord.Embed(title=f"{st['emoji']} {st['name']}", description=f"{st['detail']}\nStreaming live 24/7", color=0xCC0000)
        await interaction.response.send_message(embed=embed)


async def main():
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN is not set")
    if not VOICE_CHANNEL_ID:
        raise ValueError("VOICE_CHANNEL_ID is not set")
    bot = RadioBot()
    setup_commands(bot)
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
