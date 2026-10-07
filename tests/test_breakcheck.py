import time
import unittest
from unittest.mock import AsyncMock

import bot as B


class BreakCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = B.MusicBot()

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_embed_lists_every_station_and_recent_decisions(self):
        self.bot._breaks = {"q101": True, "rock": False, "drive": None}
        self.bot._why["q101"] = "ad cue 4s ago (30s long), no newer song"
        self.bot._note("%s: %s", "Q101", "commercial")
        embed = self.bot._breakcheck_embed()
        names = [f.name for f in embed.fields]
        self.assertEqual(len(names), len(B.STATIONS) + 1)
        self.assertTrue(any("COMMERCIAL" in n for n in names))
        self.assertIn("ad cue 4s ago", embed.fields[0].value)
        self.assertIn("Q101: commercial", embed.fields[-1].value)
        self.assertTrue(all(len(f.value) <= 1024 for f in embed.fields))

    async def test_triton_reason_explains_the_verdict(self):
        now = int(time.time() * 1000)
        cues = {"ad": (now - 5000, 30000, "ad", ""), "track": (now - 60000, 200000, "Song", "Artist")}
        self.bot._latest_cue = AsyncMock(side_effect=lambda mount, event: cues[event])
        self.assertTrue(await self.bot._triton_in_break("q101", "WKQXFM"))
        self.assertIn("ad cue 5s ago", self.bot._why["q101"])
        cues["track"] = (now - 1000, 200000, "Next", "Artist")
        self.assertFalse(await self.bot._triton_in_break("q101", "WKQXFM"))
        self.assertIn("after the last ad cue", self.bot._why["q101"])
