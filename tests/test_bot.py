import os
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("VOICE_CHANNEL_ID", "123")

import bot


class Stations(unittest.TestCase):
    def test_list_is_sane(self):
        self.assertIn("q101", bot.STATIONS)
        self.assertLessEqual(len(bot.STATIONS), 25, "Discord allows 25 options in a drop-down")
        urls = [st["url"] for st in bot.STATIONS.values()]
        self.assertEqual(len(urls), len(set(urls)))
        for key, st in bot.STATIONS.items():
            self.assertTrue(st["url"].startswith("https://"), key)
            self.assertLessEqual(len(f"{st['name']} · {st['genre']}"), 100)
            self.assertLessEqual(len(st["detail"]), 100)
        genres = {st["genre"] for st in bot.STATIONS.values()}
        self.assertEqual(genres, {"Rock", "Hip hop / R&B"})

    def test_commands(self):
        radio = bot.RadioBot()
        bot.setup_commands(radio)
        self.assertEqual(sorted(c.name for c in radio.tree.get_commands()), ["nowplaying", "radio"])


class Dropdown(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.radio = bot.RadioBot()
        self.vc = MagicMock()
        self.vc.channel = MagicMock()
        self.vc.is_playing.return_value = False
        self.vc.is_paused.return_value = False
        self.radio.voice_client = lambda: self.vc
        self.radio.update_presence = AsyncMock()
        self.radio._start_stream = MagicMock()
        self.select = bot.StationSelect(self.radio)

    def interaction(self, in_channel=True):
        i = MagicMock()
        i.response.send_message = AsyncMock()
        i.user.voice.channel = self.vc.channel if in_channel else MagicMock()
        return i

    async def test_option_per_station(self):
        self.assertEqual([o.value for o in self.select.options], list(bot.STATIONS))

    async def test_listener_switches_station(self):
        self.select._values = ["wgci"]
        await self.select.callback(self.interaction())
        self.assertEqual(self.radio.current, "wgci")
        self.radio._start_stream.assert_called_once()

    async def test_outsider_cannot_switch(self):
        self.select._values = ["wgci"]
        i = self.interaction(in_channel=False)
        await self.select.callback(i)
        self.assertEqual(self.radio.current, bot.DEFAULT_STATION)
        self.assertTrue(i.response.send_message.call_args.kwargs.get("ephemeral"))
        self.radio._start_stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
