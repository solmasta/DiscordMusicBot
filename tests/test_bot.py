import asyncio
import os
import time
import unittest
from unittest.mock import MagicMock

os.environ["RADIO_CHANNEL_ID"] = "123"

import bot


class Card(unittest.TestCase):
    def test_pretty_fixes_shouting(self):
        self.assertEqual(bot._pretty("FOO FIGHTERS"), "Foo Fighters")
        self.assertEqual(bot._pretty("Foo Fighters"), "Foo Fighters")
        self.assertEqual(bot._pretty("ACDC"), "ACDC")

    def test_song_text_and_progress(self):
        b = bot.RadioBot()
        self.assertEqual(b._song_text(), "")
        self.assertIsNone(b._progress())
        b._now = ("Queen", "Under Pressure")
        self.assertEqual(b._song_text(), "Queen – Under Pressure")
        b._song_span = (time.time() - 50, time.time() + 50)
        self.assertAlmostEqual(b._progress(), 0.5, delta=0.05)

    def test_volume_is_muted_while_someone_decides(self):
        b = bot.RadioBot()
        self.assertEqual(b._effective_volume(), b.radio_volume)
        b._pending[1] = MagicMock()
        self.assertEqual(b._effective_volume(), 0.0)

    def test_station_is_q101_with_one_stream(self):
        self.assertEqual(bot.STATION_NAME, "Q101")
        self.assertTrue(bot.RADIO_URL.startswith("https://"))
        self.assertFalse(hasattr(bot, "STATIONS"), "no rotation")


class Setup(unittest.IsolatedAsyncioTestCase):
    async def test_commands_load(self):
        b = bot.RadioBot()
        await b.load_extension("cogs.music")
        names = {c.name for c in b.get_cog("Music").get_app_commands()}
        self.assertTrue({"play", "skip", "queue"} <= names)
        self.assertNotIn("stations", names)
        await b.close()


if __name__ == "__main__":
    unittest.main()
