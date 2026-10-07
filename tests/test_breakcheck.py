import time
import unittest
import unittest.mock
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


class FakeVC:
    def __init__(self, channel):
        self.channel, self.playing = channel, False
        self.opus = None

    def is_connected(self): return True
    def is_playing(self): return self.playing
    def is_paused(self): return False
    def stop(self): self.playing = False

    def play(self, source, after=None, **opus):
        self.playing, self.opus = True, opus
        source.read()      # first audio arrives, like a healthy stream


class HomePick(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import types
        from unittest.mock import MagicMock
        self.bot = B.MusicBot()
        self.channel = types.SimpleNamespace(id=7, mention="#radio", bitrate=64000)
        self.vc = FakeVC(self.channel)
        self.channel.guild = types.SimpleNamespace(voice_client=self.vc)
        self.bot.get_channel = lambda cid: self.channel
        B.RADIO_CHANNEL_ID = "7"
        self.station = types.SimpleNamespace(uuid="u1", name="WLS 94.7", place="Chicago, IL", genres=frozenset({"Talk"}))
        self.bot.directory.resolve_url = AsyncMock(return_value="http://93.184.216.34/live")
        self.member = types.SimpleNamespace(id=1, display_name="Pat", voice=types.SimpleNamespace(channel=self.channel))
        self.bot._update_presence = AsyncMock()
        self.started = []

        def fake_start(vc):
            self.started.append(self.bot._pick)
            self.bot._pick_started = getattr(self, "audio_ok", True)
        self.bot._start_playing = fake_start

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_a_picked_station_pauses_the_rotation(self):
        with unittest.mock.patch.object(B, "check_stream_url", AsyncMock(return_value=None)):
            ok, msg = await self.bot.pick_station(self.member, self.station)
        self.assertTrue(ok, msg)
        self.assertIs(self.bot._pick, self.station)
        keys = [s["key"] for s in B.STATIONS]
        self.bot._breaks = {k: True for k in keys}
        self.bot._current = keys[0]
        self.bot._last_switch = 0
        self.bot._reevaluate()
        self.assertEqual(self.bot._current, keys[0], "no hopping to another station while one is picked")

    async def test_must_be_in_the_radio_channel(self):
        import types
        self.member.voice = types.SimpleNamespace(channel=types.SimpleNamespace(id=99))
        ok, msg = await self.bot.pick_station(self.member, self.station)
        self.assertFalse(ok)
        self.assertIn("#radio", msg)
        self.assertIsNone(self.bot._pick)
        self.member.voice = None
        self.assertFalse((await self.bot.pick_station(self.member, self.station))[0])

    async def test_dead_station_restores_what_was_playing(self):
        self.audio_ok = False
        with unittest.mock.patch.object(B, "check_stream_url", AsyncMock(return_value=None)), \
                unittest.mock.patch.object(B, "START_WAIT_S", 0.4):
            ok, msg = await self.bot.pick_station(self.member, self.station)
        self.assertFalse(ok)
        self.assertIn("isn't responding", msg)
        self.assertIsNone(self.bot._pick, "back to the rotation")
        self.assertEqual(len(self.started), 2, "tried the pick, then restarted what was there")

    async def test_unsafe_url_is_refused(self):
        with unittest.mock.patch.object(B, "check_stream_url", AsyncMock(return_value="That address isn't allowed")):
            ok, msg = await self.bot.pick_station(self.member, self.station)
        self.assertFalse(ok)
        self.assertIsNone(self.bot._pick)

    async def test_back_to_rotation(self):
        self.assertFalse((await self.bot.back_to_rotation(self.member))[0], "nothing to undo")
        self.bot._pick, self.bot._pick_by = self.station, "Pat"
        ok, msg = await self.bot.back_to_rotation(self.member)
        self.assertTrue(ok, msg)
        self.assertIsNone(self.bot._pick)

    async def test_picks_are_rate_limited_per_person(self):
        with unittest.mock.patch.object(B, "check_stream_url", AsyncMock(return_value=None)):
            self.assertTrue((await self.bot.pick_station(self.member, self.station))[0])
            ok, msg = await self.bot.pick_station(self.member, self.station)
        self.assertFalse(ok)
        self.assertIn("Easy there", msg)
