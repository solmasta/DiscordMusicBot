import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("VOICE_CHANNEL_ID", "123")

import discord

import bot


class Stations(unittest.TestCase):
    def test_default_station_exists(self):
        self.assertIn(bot.STATION, bot.STATIONS)
        self.assertEqual(bot.STATION, next(iter(bot.STATIONS)))

    def test_station_table_fits_discord_limits(self):
        self.assertLessEqual(len(bot.STATIONS), 25)  # max slash-command choices
        labels = [label for label, _ in bot.STATIONS.values()]
        self.assertEqual(len(labels), len(set(labels)))
        for label, url in bot.STATIONS.values():
            self.assertTrue(0 < len(label) <= 100)
            self.assertRegex(url, r"^https?://")
            self.assertLessEqual(len(url), 100)  # also used as an autocomplete value

    def test_chicago_stations_present(self):
        self.assertIn("wkqx", bot.STATIONS)  # Q101

    def test_stream_presets_include_every_station(self):
        from cogs.stations import PRESETS
        for label, url in bot.STATIONS.values():
            self.assertEqual(PRESETS[label], url)


class Behaviour(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = bot.CrueFM()

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_commands_are_registered(self):
        bot.register(self.bot)
        await self.bot.setup_hook()
        names = {c.name for c in self.bot.tree.get_commands()}
        self.assertEqual(names, {"station", "nowplaying", "stream"})
        station = self.bot.tree.get_command("station")
        choices = station._params["name"].choices
        self.assertEqual([c.value for c in choices], list(bot.STATIONS))

    async def test_play_streams_the_current_station(self):
        vc = MagicMock()
        self.bot.station = "wkqx"
        with patch.object(discord.FFmpegOpusAudio, "from_probe", new=AsyncMock(return_value="src")) as probe:
            await self.bot._play(vc)
        self.assertEqual(probe.call_args.args[0], bot.STATIONS["wkqx"][1])
        vc.play.assert_called_once()
        self.assertEqual(vc.play.call_args.args[0], "src")

    async def test_guard_restarts_playback_when_idle(self):
        vc = MagicMock(is_playing=lambda: False, is_paused=lambda: False)
        channel = MagicMock(guild=MagicMock())
        self.bot._play = AsyncMock()
        with patch.object(self.bot, "get_channel", return_value=channel), \
             patch.object(discord.utils, "get", return_value=vc), \
             patch.object(type(self.bot), "voice_clients", new_callable=PropertyMock, return_value=[vc]):
            await self.bot.guard.coro(self.bot)
        self.bot._play.assert_awaited_once_with(vc)

    async def test_guard_leaves_playing_stream_alone(self):
        vc = MagicMock(is_playing=lambda: True, is_paused=lambda: False)
        channel = MagicMock(guild=MagicMock())
        self.bot._play = AsyncMock()
        with patch.object(self.bot, "get_channel", return_value=channel), \
             patch.object(discord.utils, "get", return_value=vc), \
             patch.object(type(self.bot), "voice_clients", new_callable=PropertyMock, return_value=[vc]):
            await self.bot.guard.coro(self.bot)
        self.bot._play.assert_not_awaited()

    async def test_bot_survives_a_broken_stream_cog(self):
        with patch.object(self.bot, "load_extension", side_effect=RuntimeError("boom")):
            await self.bot.setup_hook()  # must not raise


if __name__ == "__main__":
    unittest.main()
