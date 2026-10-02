import os
import asyncio
import logging
import discord
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
        status = RADIO_URL if RADIO_URL else "/play to add music"
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
                source = discord.FFmpegPCMAudio(RADIO_URL, **FFMPEG_RADIO_OPTIONS)
                vc.play(source)
                log.info("Radio: started stream from %s", RADIO_URL)
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
