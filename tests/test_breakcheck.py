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


class StreamTitles(unittest.IsolatedAsyncioTestCase):
    """Real titles seen on 97.1 The Drive and Q101 while a break was starting."""

    def test_songs_versus_station_content(self):
        for song in ["POLICE - ROXANNE", "LED ZEPPELIN - THE OCEAN", "Marshmello, Bastille - Happier", "blink-182 - All The Small Things",
                     "Weezer - We Might As Well Be Strangers (feat  Wednesday)", "sombr - back to friends"]:
            self.assertTrue(B.looks_like_song(song), song)
        for other in ["VT 97.1 The Drive: 2026-10-06 10:33", "S&T HALLOWEEN HAUNTED CRUISE PROMO", "Dan Stone-Twofer Tues Swp#1",
                      "ROBERT PLANT KEYWORD (RECORD)", "PS - GVF 27-BP-OSTHURS-10052026==", "", "Q101"]:
            self.assertFalse(B.looks_like_song(other), other)

    async def asyncSetUp(self):
        self.bot = B.MusicBot()
        now = int(time.time() * 1000)
        self.cues = {"track": (now - 260000, 263000, "THE OCEAN", "LED ZEPPELIN"), "ad": (now - 900000, 15000, None, None)}
        self.bot._latest_cue = AsyncMock(side_effect=lambda mount, event: self.cues[event])

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_a_liner_flags_the_break_before_any_ad_cue_exists(self):
        self.assertFalse(await self.bot._triton_in_break("drive", "WDRVFM"), "no stream title yet: cues say music")
        self.bot._icy_title["drive"], self.bot._icy_seen["drive"] = "VT 97.1 The Drive: 2026-10-06 10:33", time.monotonic()
        self.assertTrue(await self.bot._triton_in_break("drive", "WDRVFM"))
        self.assertIn("not a song", self.bot._why["drive"])

    async def test_a_song_title_does_not_flag_a_break(self):
        self.bot._icy_title["drive"], self.bot._icy_seen["drive"] = "POLICE - ROXANNE", time.monotonic()
        self.assertFalse(await self.bot._triton_in_break("drive", "WDRVFM"))

    async def test_a_dead_title_reader_is_ignored(self):
        self.bot._icy_title["drive"], self.bot._icy_seen["drive"] = "S&T PROMO", time.monotonic() - 120
        self.assertFalse(await self.bot._triton_in_break("drive", "WDRVFM"), "old data must not trigger a switch")

    async def test_reader_parses_titles_from_a_real_shaped_stream(self):
        import asyncio
        meta = b"StreamTitle='POLICE - ROXANNE';"
        block = meta + b"\0" * (-len(meta) % 16)
        data = (b"\0" * 16 + bytes([len(block) // 16]) + block) + (b"\0" * 16 + b"\0") * 3

        class Content:
            def __init__(self): self.buf = data
            async def readexactly(self, n):
                if len(self.buf) < n:
                    raise asyncio.IncompleteReadError(b"", n)
                out, self.buf = self.buf[:n], self.buf[n:]
                return out

        class Resp:
            headers = {"icy-metaint": "16"}
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        self.bot._http = unittest.mock.MagicMock()
        self.bot._http.get = lambda *a, **k: Resp()
        task = asyncio.create_task(self.bot._icy_loop("drive", "http://x"))
        await asyncio.sleep(0.1)
        task.cancel()
        self.assertEqual(self.bot._icy_title["drive"], "POLICE - ROXANNE")
        self.assertNotIn("drive", self.bot._icy_seen, "a dropped stream stops counting as live data")
        self.bot._http = None


class Silent(B.discord.AudioSource):
    def __init__(self, url=None, **k):
        self.url, self.kwargs = url, k

    def read(self):
        return b"\0" * 3840


class HomeSongs(unittest.IsolatedAsyncioTestCase):
    """Library songs on the home radio: the rotation pauses, songs play in turn, then the radio resumes."""

    async def asyncSetUp(self):
        import asyncio
        import types
        import library as L
        self.L = L
        self.bot = B.MusicBot()
        self.bot.loop = asyncio.get_running_loop()
        self.channel = types.SimpleNamespace(id=7, mention="#radio", bitrate=64000)
        self.vc = FakeVC(self.channel)
        self.channel.guild = types.SimpleNamespace(voice_client=self.vc)
        self.bot.get_channel = lambda cid: self.channel
        B.RADIO_CHANNEL_ID = "7"
        self.member = types.SimpleNamespace(id=1, display_name="Pat", voice=types.SimpleNamespace(channel=self.channel))
        self.bot._update_presence = AsyncMock()
        self.songs = [L.Song(str(i), f"Song {i}", "Band", "Rock", L.FREE, url=f"https://s/{i}.mp3", duration=100) for i in range(3)]
        patcher = unittest.mock.patch.object(B.discord, "FFmpegPCMAudio", Silent)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.resolved = {s.id: (s.url, 100) for s in self.songs}
        self.bot._resolve_song = AsyncMock(side_effect=lambda s: self.resolved.get(s.id))
        self.bot._checked = True

    async def asyncTearDown(self):
        await self.bot.close()

    async def start(self, songs=None):
        return await self.bot.play_songs(self.member, songs or self.songs)

    async def test_songs_play_and_pause_the_rotation(self):
        ok, msg = await self.start()
        self.assertTrue(ok, msg)
        self.assertEqual(self.bot._songs[self.bot._song_i].title, "Song 0")
        keys = [s["key"] for s in B.STATIONS]
        self.bot._breaks = {k: True for k in keys}
        self.bot._current, self.bot._last_switch = keys[0], 0
        self.bot._reevaluate()
        self.assertEqual(self.bot._current, keys[0], "the rotation does not move while songs play")
        self.assertEqual(self.vc.opus["signal_type"], "music")

    async def test_each_song_end_moves_on_then_the_radio_returns(self):
        import asyncio
        await self.start()
        for expected in (1, 2):
            self.bot._song_ended(self.bot._song_gen, None)
            await asyncio.sleep(0.05)
            self.assertEqual(self.bot._song_i, expected)
        self.bot._song_ended(self.bot._song_gen, None)
        await asyncio.sleep(0.05)
        self.assertEqual(self.bot._songs, [], "list finished: back to the radio")

    async def test_a_replaced_source_cannot_skip_a_song(self):
        import asyncio
        await self.start()
        old_gen = self.bot._song_gen - 1
        self.bot._song_ended(old_gen, None)
        await asyncio.sleep(0.05)
        self.assertEqual(self.bot._song_i, 0)

    async def test_unfindable_songs_are_skipped_and_all_unfindable_is_refused(self):
        self.resolved.pop("0")
        ok, msg = await self.start()
        self.assertTrue(ok, msg)
        self.assertEqual(self.bot._song_i, 1, "started on the first song it could find")
        self.resolved.clear()
        await self.bot.back_to_rotation(self.member)
        self.bot._pick_cooldown.clear()
        ok, msg = await self.start()
        self.assertFalse(ok)
        self.assertEqual(self.bot._songs, [])

    async def test_a_song_that_will_not_start_restores_the_radio(self):
        class Dead(Silent):
            def read(self):
                return b""
        with unittest.mock.patch.object(B.discord, "FFmpegPCMAudio", Dead), unittest.mock.patch.object(B, "START_WAIT_S", 0.4):
            ok, msg = await self.start()
        self.assertFalse(ok)
        self.assertEqual(self.bot._songs, [])

    async def test_must_be_in_the_radio_channel_and_back_to_rotation_clears_songs(self):
        import types
        self.member.voice = types.SimpleNamespace(channel=types.SimpleNamespace(id=99))
        ok, msg = await self.start()
        self.assertFalse(ok)
        self.assertIn("#radio", msg)
        self.member.voice = types.SimpleNamespace(channel=self.channel)
        self.assertTrue((await self.start())[0])
        ok, msg = await self.bot.back_to_rotation(self.member)
        self.assertTrue(ok, msg)
        self.assertEqual(self.bot._songs, [])

    async def test_skip_song(self):
        ok, msg = await self.bot.skip_song(self.member)
        self.assertFalse(ok)
        await self.start()
        ok, _ = await self.bot.skip_song(self.member)
        self.assertTrue(ok)
        self.assertEqual(self.bot._song_i, 1)

    async def test_card_shows_the_song(self):
        await self.start()
        msg = unittest.mock.MagicMock()
        msg.edit = AsyncMock()
        self.bot._np_message = msg
        self.bot._fetch_art = AsyncMock(return_value=None)
        B.NOW_PLAYING_CHANNEL_ID = "7"
        await self.bot._update_card()
        desc = msg.edit.await_args.kwargs["embed"].description
        self.assertIn("Song 0", desc)
        self.assertIn("Picked by Pat", desc)
