import os
import unittest

os.environ.setdefault("VOICE_CHANNEL_ID", "123")

import bot


class OneStation(unittest.TestCase):
    def test_single_q101_station(self):
        self.assertEqual(bot.STATION["name"], "Q101")
        self.assertTrue(bot.STATION["url"].startswith("https://"))

    def test_only_nowplaying_command(self):
        radio = bot.RadioBot()
        bot.setup_commands(radio)
        self.assertEqual([c.name for c in radio.tree.get_commands()], ["nowplaying"])


if __name__ == "__main__":
    unittest.main()
