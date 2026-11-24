import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")

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


class HomePersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sub", "home.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_channels_survive_a_restart(self):
        h = bot.Home(self.path)
        h[1] = 10
        h[2] = 20
        h.pop(2)
        self.assertEqual(dict(bot.Home(self.path)), {1: 10})

    def test_missing_or_corrupt_file_starts_empty(self):
        self.assertEqual(dict(bot.Home(self.path)), {})
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(dict(bot.Home(self.path)), {})

    def test_unwritable_path_does_not_crash(self):
        blocker = os.path.join(self.tmp.name, "file")
        open(blocker, "w").close()
        h = bot.Home(os.path.join(blocker, "home.json"))  # parent is a file
        h[1] = 5  # must not raise
        self.assertEqual(h[1], 5)


class Behaviour(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "home.json")
        self.bot = bot.CrueFM(state_file=self.state)

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    async def test_commands_are_registered(self):
        bot.register(self.bot)
        await self.bot.setup_hook()
        names = {c.name for c in self.bot.tree.get_commands()}
        self.assertEqual(names, {"station", "nowplaying", "stream", "join", "leave"})
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

    def _guard_env(self, vc, channel):
        """Patch the bot so _keep sees `channel` and `vc` as the guild's voice state."""
        return (
            patch.object(self.bot, "get_channel", return_value=channel),
            patch.object(discord.utils, "get", return_value=vc),
            patch.object(type(self.bot), "voice_clients", new_callable=PropertyMock,
                         return_value=[vc] if vc else []),
        )

    async def _run_guard(self, vc, channel):
        p1, p2, p3 = self._guard_env(vc, channel)
        with p1, p2, p3:
            await self.bot.guard.coro(self.bot)

    async def test_guard_does_nothing_with_no_home_channel(self):
        self.bot._play = AsyncMock()
        await self.bot.guard.coro(self.bot)  # no /join yet: must not need any channel id
        self.bot._play.assert_not_awaited()

    async def test_guard_restarts_playback_when_idle(self):
        vc = MagicMock(is_playing=lambda: False, is_paused=lambda: False)
        vc.channel.id = 5
        self.bot.home[1] = 5
        self.bot._play = AsyncMock()
        await self._run_guard(vc, MagicMock(guild=MagicMock()))
        self.bot._play.assert_awaited_once_with(vc)

    async def test_guard_leaves_playing_stream_alone(self):
        vc = MagicMock(is_playing=lambda: True, is_paused=lambda: False)
        vc.channel.id = 5
        self.bot.home[1] = 5
        self.bot._play = AsyncMock()
        await self._run_guard(vc, MagicMock(guild=MagicMock()))
        self.bot._play.assert_not_awaited()

    async def test_guard_rejoins_when_disconnected(self):
        new_vc = MagicMock(is_playing=lambda: False, is_paused=lambda: False)
        channel = MagicMock(guild=MagicMock(), connect=AsyncMock(return_value=new_vc))
        self.bot.home[1] = 5
        self.bot._play = AsyncMock()
        self.bot._diagnose = AsyncMock()
        await self._run_guard(None, channel)
        channel.connect.assert_awaited_once()
        self.bot._play.assert_awaited_once_with(new_vc)

    async def test_guard_follows_a_moved_bot(self):
        vc = MagicMock(is_playing=lambda: True, is_paused=lambda: False)
        vc.channel.id = 9
        self.bot.home[1] = 5
        await self._run_guard(vc, MagicMock(guild=MagicMock()))
        self.assertEqual(self.bot.home[1], 9)

    async def test_bot_rejoins_saved_channel_after_restart(self):
        bot.Home(self.state)[1] = 5  # saved by a previous run
        restarted = bot.CrueFM(state_file=self.state)
        self.assertEqual(restarted.home[1], 5)
        vc = MagicMock(is_playing=lambda: False, is_paused=lambda: False)
        channel = MagicMock(guild=MagicMock(), connect=AsyncMock(return_value=vc))
        restarted._play = AsyncMock()
        restarted._diagnose = AsyncMock()
        with patch.object(restarted, "get_channel", return_value=channel), \
             patch.object(discord.utils, "get", return_value=None), \
             patch.object(type(restarted), "voice_clients", new_callable=PropertyMock, return_value=[]):
            await restarted.guard.coro(restarted)
        channel.connect.assert_awaited_once()
        restarted._play.assert_awaited_once_with(vc)
        await restarted.close()

    async def test_guard_forgets_a_deleted_channel(self):
        self.bot.home[1] = 5
        with patch.object(self.bot, "get_channel", return_value=None):
            await self.bot.guard.coro(self.bot)
        self.assertNotIn(1, self.bot.home)

    async def test_join_uses_the_callers_channel(self):
        bot.register(self.bot)
        ch = MagicMock(id=77)
        ch.permissions_for.return_value = MagicMock(view_channel=True, connect=True, speak=True)
        vc = MagicMock(is_playing=lambda: False, is_paused=lambda: False)
        ch.connect = AsyncMock(return_value=vc)
        inter = MagicMock(guild=MagicMock(id=1))
        inter.user.voice.channel = ch
        inter.response.defer = AsyncMock()
        inter.followup.send = AsyncMock()
        self.bot._play = AsyncMock()
        self.bot._diagnose = AsyncMock()
        with patch.object(discord.utils, "get", return_value=None):
            await self.bot.tree.get_command("join").callback(inter)
        self.assertEqual(self.bot.home[1], 77)
        self.bot._play.assert_awaited_once_with(vc)

    async def test_join_requires_a_voice_channel(self):
        bot.register(self.bot)
        inter = MagicMock(guild=MagicMock(id=1))
        inter.user.voice = None
        inter.response.send_message = AsyncMock()
        await self.bot.tree.get_command("join").callback(inter)
        self.assertNotIn(1, self.bot.home)
        inter.response.send_message.assert_awaited_once()

    async def test_leave_stops_the_guard_from_rejoining(self):
        bot.register(self.bot)
        vc = MagicMock(disconnect=AsyncMock())
        self.bot.home[1] = 5
        inter = MagicMock(guild=MagicMock(id=1))
        inter.response.send_message = AsyncMock()
        with patch.object(discord.utils, "get", return_value=vc):
            await self.bot.tree.get_command("leave").callback(inter)
        vc.disconnect.assert_awaited_once()
        self.assertNotIn(1, self.bot.home)

    async def test_bot_survives_a_broken_stream_cog(self):
        with patch.object(self.bot, "load_extension", side_effect=RuntimeError("boom")):
            await self.bot.setup_hook()  # must not raise


if __name__ == "__main__":
    unittest.main()
