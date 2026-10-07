import asyncio
import socket
import time
import types
import unittest
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import discord

import directory as D
import publicradio as P


class Silent(discord.AudioSource):
    def __init__(self, url=None, **k):
        self.url, self.kwargs = url, k

    def read(self):
        return b"\0" * 3840


class FakeVC:
    def __init__(self, channel):
        self.channel = channel
        self.connected = True
        self.playing = False
        self.source = None
        self.plays = 0
        self.guild = channel.guild

    def is_connected(self): return self.connected
    def is_playing(self): return self.playing
    def is_paused(self): return False

    def stop(self):
        self.playing = False
        self.source = None

    dead_urls: set = set()

    def play(self, source, after=None, **opus):
        self.source, self.playing, self.after, self.opus = source, True, after, opus
        self.plays += 1
        if getattr(source.original.inner, "url", None) in FakeVC.dead_urls:
            self.playing = False
            after(RuntimeError("ffmpeg exited"))      # what discord.py does when the stream ends
        else:
            source.read()                             # first audio arrives

    async def move_to(self, channel): self.channel = channel

    async def disconnect(self, force=False):
        self.connected = False
        self.guild.voice_client = None


def make_guild(gid=1, me_id=999):
    guild = MagicMock()
    guild.id, guild.name = gid, f"guild{gid}"
    guild.me = types.SimpleNamespace(id=me_id)
    guild.voice_client = None
    guild.get_member = lambda uid: None
    return guild


def make_channel(guild, cid=10, users=(), allow=True):
    ch = MagicMock()
    ch.id, ch.guild, ch.mention = cid, guild, f"<#{cid}>"
    ch.voice_states = {u: object() for u in users}
    ch.permissions_for = lambda me: types.SimpleNamespace(connect=allow, speak=allow)
    return ch


def make_member(guild, uid, channel=None, manage=False):
    m = MagicMock()
    m.id, m.guild, m.bot = uid, guild, False
    m.voice = types.SimpleNamespace(channel=channel) if channel else None
    m.guild_permissions = types.SimpleNamespace(manage_guild=manage, manage_channels=False)
    return m


def station(name="WXYZ 95.5", uuid="s1"):
    return D.Station(uuid=uuid, name=name, url="http://93.184.216.34/live", state="IL", city="Chicago", market="Chicago")


def make_radio(guilds=()):
    bot = MagicMock()
    bot.directory.resolve_url = AsyncMock(return_value="http://93.184.216.34/live")
    table = {g.id: g for g in guilds}
    bot.get_guild = lambda gid: table.get(gid)
    bot.get_channel = lambda cid: None
    return P.PublicRadio(bot), bot


class StreamUrlSafety(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_dangerous_addresses(self):
        for url in ["file:///etc/passwd", "ftp://example.com/x", "gopher://x/", "http://localhost/x", "http://127.0.0.1:8000/",
                    "http://10.0.0.5/s", "http://192.168.1.1/", "http://172.16.4.4/", "http://169.254.169.254/latest/meta-data",
                    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[fdaa::1]/", "http://foo.internal/s", "http://x.local/s",
                    "http://0.0.0.0/", "", "http:///nohost", "not a url", "http://user@127.0.0.1/"]:
            self.assertIsNotNone(await P.check_stream_url(url), url)

    async def test_allows_public_addresses(self):
        for url in ["http://93.184.216.34/stream", "https://93.184.216.34:8443/x.mp3"]:
            self.assertIsNone(await P.check_stream_url(url), url)

    async def test_hostname_resolving_to_private_is_blocked(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 80))]
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
        with mock.patch("socket.getaddrinfo", return_value=private):
            self.assertIsNotNone(await P.check_stream_url("http://sneaky.example.com/s"))
        with mock.patch("socket.getaddrinfo", return_value=public + private):
            self.assertIsNotNone(await P.check_stream_url("http://mixed.example.com/s"), "any private answer blocks it")
        with mock.patch("socket.getaddrinfo", return_value=public):
            self.assertIsNone(await P.check_stream_url("http://fine.example.com/s"))
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror):
            self.assertIn("couldn't reach", await P.check_stream_url("http://nope.example.com/s"))

    def test_ffmpeg_is_restricted_to_web_protocols(self):
        self.assertIn("-protocol_whitelist http,https,tcp,tls,crypto", P.FFMPEG_BEFORE)
        self.assertIn("-rw_timeout", P.FFMPEG_BEFORE)
        self.assertNotIn("file", P.FFMPEG_BEFORE.split("-protocol_whitelist")[1].split()[0])


class Defaults(unittest.TestCase):
    def test_new_servers_start_quiet(self):
        self.assertLessEqual(P.DEFAULT_VOLUME, 0.5, "listeners raise it for themselves; it must never start loud")
        self.assertGreater(P.DEFAULT_VOLUME, 0.1, "but not so low it sounds broken")


class Control(unittest.TestCase):
    def setUp(self):
        self.g = make_guild()
        self.ch = make_channel(self.g, users=(1, 2, 3))
        self.g.voice_client = FakeVC(self.ch)
        self.radio, _ = make_radio([self.g])
        self.player = P.PublicPlayer(1, 10, None, station(), "http://x", started_by=1)

    def test_idle_radio_can_be_started_by_anyone(self):
        self.assertTrue(self.radio.may_control(make_member(self.g, 5), None))

    def test_starter_and_managers_always_can(self):
        self.assertTrue(self.radio.may_control(make_member(self.g, 1, self.ch), self.player))
        self.assertTrue(self.radio.may_control(make_member(self.g, 50, None, manage=True), self.player))

    def test_strangers_cannot_hijack(self):
        self.assertFalse(self.radio.may_control(make_member(self.g, 3, self.ch), self.player))
        elsewhere = make_channel(self.g, cid=11, users=(7,))
        self.assertFalse(self.radio.may_control(make_member(self.g, 7, elsewhere), self.player))
        self.assertFalse(self.radio.may_control(make_member(self.g, 8, None), self.player))

    def test_lone_listener_or_absent_starter_can_take_over(self):
        self.ch.voice_states = {3: object()}
        self.assertTrue(self.radio.may_control(make_member(self.g, 3, self.ch), self.player), "lone listener")
        self.ch.voice_states = {2: object(), 3: object()}
        self.assertTrue(self.radio.may_control(make_member(self.g, 3, self.ch), self.player), "starter has left")

    def test_listener_count_ignores_the_bot_and_other_bots(self):
        bot_member = MagicMock(); bot_member.bot = True
        self.g.get_member = lambda uid: bot_member if uid == 77 else None
        self.ch.voice_states = {999: object(), 77: object(), 1: object(), 2: object()}
        self.assertEqual(P.listener_ids(self.ch, 999), {1, 2})
        self.assertEqual(P.listener_ids(self.ch, 999), {1, 2}, "members who were never cached still count as people")


class Tuning(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.object(P.discord, "FFmpegPCMAudio", Silent)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.g = make_guild()
        self.ch = make_channel(self.g, users=(1,))
        self.ch.connect = AsyncMock(side_effect=lambda **k: self._connect(**k))
        self.radio, self.bot = make_radio([self.g])

    def _connect(self, **kw):
        self.connect_kwargs = kw
        vc = FakeVC(self.ch)
        self.g.voice_client = vc
        return vc

    async def test_must_be_in_a_voice_channel(self):
        ok, msg = await self.radio.tune(make_member(self.g, 1), station())
        self.assertFalse(ok)
        self.assertIn("voice channel", msg)

    async def test_successful_tune(self):
        ok, msg = await self.radio.tune(make_member(self.g, 1, self.ch), station(), text_channel_id=55)
        self.assertTrue(ok, msg)
        self.assertTrue(self.connect_kwargs["self_deaf"])
        vc = self.g.voice_client
        self.assertTrue(vc.playing)
        self.assertIsInstance(vc.source, discord.PCMVolumeTransformer)
        self.assertEqual(vc.source.original.inner.kwargs["before_options"], P.FFMPEG_BEFORE)
        self.assertEqual(vc.opus["signal_type"], "music", "voice is encoded for music")
        player = self.radio.players[1]
        self.assertEqual((player.started_by, player.text_channel_id, player.volume), (1, 55, P.DEFAULT_VOLUME))
        self.bot.directory.resolve_url.assert_awaited()

    async def test_retune_keeps_volume_and_cooldown_applies(self):
        m = make_member(self.g, 1, self.ch)
        await self.radio.tune(m, station())
        self.radio.set_volume(1, 0.3)
        self.assertEqual(self.g.voice_client.source.volume, 0.3)
        ok, msg = await self.radio.tune(m, station("Other", "s2"))
        self.assertFalse(ok)
        self.assertIn("few seconds", msg)
        self.radio._cooldown.clear()
        ok, _ = await self.radio.tune(m, station("Other", "s2"))
        self.assertTrue(ok)
        self.assertEqual(self.radio.players[1].volume, 0.3, "volume survives changing station")

    async def test_unsafe_url_is_refused_before_joining(self):
        self.bot.directory.resolve_url = AsyncMock(return_value="http://169.254.169.254/latest/meta-data")
        ok, msg = await self.radio.tune(make_member(self.g, 1, self.ch), station())
        self.assertFalse(ok)
        self.ch.connect.assert_not_called()
        self.assertNotIn(1, self.radio.players)

    async def test_missing_permissions_and_connect_failure(self):
        self.ch.permissions_for = lambda me: types.SimpleNamespace(connect=False, speak=True)
        ok, msg = await self.radio.tune(make_member(self.g, 1, self.ch), station())
        self.assertFalse(ok)
        self.assertIn("Connect", msg)
        self.ch.permissions_for = lambda me: types.SimpleNamespace(connect=True, speak=True)
        self.ch.connect = AsyncMock(side_effect=asyncio.TimeoutError)
        ok, msg = await self.radio.tune(make_member(self.g, 2, self.ch), station())
        self.assertFalse(ok)
        self.assertNotIn(1, self.radio.players)

    async def test_dead_station_is_reported_and_nothing_is_left_behind(self):
        FakeVC.dead_urls = {"http://93.184.216.34/live"}
        self.addCleanup(FakeVC.dead_urls.clear)
        ok, msg = await self.radio.tune(make_member(self.g, 1, self.ch), station("Dead FM"))
        self.assertFalse(ok)
        self.assertIn("isn't responding", msg)
        self.assertNotIn(1, self.radio.players)
        self.assertFalse(self.connect_kwargs is None or self.g.voice_client is not None, "left the voice channel again")

    async def test_dead_station_keeps_the_previous_one_playing(self):
        m = make_member(self.g, 1, self.ch)
        await self.radio.tune(m, station("Good FM", "good"))
        FakeVC.dead_urls = {"http://dead.example/x"}
        self.addCleanup(FakeVC.dead_urls.clear)
        self.bot.directory.resolve_url = AsyncMock(return_value="http://93.184.216.34/live")
        self.radio._cooldown.clear()
        with mock.patch.object(P.PublicRadio, "_wait_started", AsyncMock(return_value=False)):
            ok, msg = await self.radio.tune(m, station("Dead FM", "dead"))
        self.assertFalse(ok)
        self.assertIn("kept playing **Good FM**", msg)
        self.assertEqual(self.radio.players[1].station.uuid, "good")
        self.assertTrue(self.g.voice_client.playing)

    async def test_slow_station_times_out(self):
        with mock.patch.object(P, "START_WAIT_S", 0.3), mock.patch.object(FakeVC, "play", lambda self, source, after=None, **opus: setattr(self, "playing", True)):
            ok, msg = await self.radio.tune(make_member(self.g, 1, self.ch), station("Silent FM"))
        self.assertFalse(ok)
        self.assertNotIn(1, self.radio.players)

    async def test_global_capacity_cap(self):
        with mock.patch.object(P, "MAX_STREAMS", 1):
            self.radio.players[500] = MagicMock()
            ok, msg = await self.radio.tune(make_member(self.g, 1, self.ch), station())
            self.assertFalse(ok)
            self.assertIn("busy", msg)
            self.radio.players[1] = P.PublicPlayer(1, 10, None, station(), "u", 1)
            self.g.voice_client = FakeVC(self.ch)
            ok, _ = await self.radio.tune(make_member(self.g, 1, self.ch), station("Same server can still change", "s9"))
            self.assertTrue(ok, "a server that is already playing is never refused by the cap")

    async def test_hijack_is_refused(self):
        await self.radio.tune(make_member(self.g, 1, self.ch), station())
        self.ch.voice_states = {1: object(), 2: object()}
        ok, msg = await self.radio.tune(make_member(self.g, 2, self.ch), station("Other", "s2"))
        self.assertFalse(ok)
        self.assertIn("controlling", msg)
        self.assertEqual(self.radio.players[1].station.uuid, "s1")


class Upkeep(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.object(P.discord, "FFmpegPCMAudio", Silent)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.g = make_guild()
        self.ch = make_channel(self.g, users=(1,))
        self.vc = FakeVC(self.ch)
        self.g.voice_client = self.vc
        self.radio, self.bot = make_radio([self.g])
        self.player = P.PublicPlayer(1, 10, 55, station(), "http://93.184.216.34/live", 1)
        self.radio.players[1] = self.player
        self.sent = []
        text = MagicMock()
        text.send = AsyncMock(side_effect=lambda t: self.sent.append(t))
        self.bot.get_channel = lambda cid: text if cid == 55 else None

    async def tick(self):
        await P.PublicRadio.monitor.coro(self.radio)

    async def test_kicked_or_disconnected_player_is_forgotten(self):
        self.g.voice_client = None
        await self.tick()
        self.assertNotIn(1, self.radio.players)

    async def test_leaves_after_everyone_has_gone(self):
        self.ch.voice_states = {}
        with mock.patch.object(P, "IDLE_LEAVE_S", 0):
            await self.tick()
        self.assertNotIn(1, self.radio.players)
        self.assertFalse(self.vc.connected)
        self.assertTrue(any("Everyone left" in t for t in self.sent))

    async def test_waits_before_leaving_and_listeners_reset_the_timer(self):
        self.ch.voice_states = {}
        await self.tick()
        self.assertIn(1, self.radio.players, "default grace period is minutes, not seconds")
        self.assertIsNotNone(self.player.idle_since)
        self.ch.voice_states = {1: object()}
        await self.tick()
        self.assertIsNone(self.player.idle_since)

    async def test_dropped_stream_is_restarted_then_given_up_on(self):
        self.vc.playing = True
        plays = self.vc.plays
        self.player.ended = True
        await self.tick()
        self.assertEqual(self.vc.plays, plays + 1, "restarted")
        self.assertFalse(self.player.ended)
        for _ in range(3):
            self.player.ended = True
            await self.tick()
        self.player.ended = True
        await self.tick()
        self.assertNotIn(1, self.radio.players, "gave up after repeated failures")
        self.assertTrue(any("keeps dropping" in t for t in self.sent))

    async def test_no_restart_while_nobody_listens(self):
        self.ch.voice_states = {}
        self.player.ended = True
        plays = self.vc.plays
        await self.tick()
        self.assertEqual(self.vc.plays, plays)

    async def test_stop_and_shutdown_disconnect(self):
        await self.radio.shutdown()
        self.assertFalse(self.vc.connected)
        self.assertEqual(self.radio.players, {})


if __name__ == "__main__":
    unittest.main()


class SoundQuality(unittest.TestCase):
    def test_opus_settings_follow_the_channel_bitrate(self):
        import types
        ch = lambda bps: types.SimpleNamespace(bitrate=bps)
        self.assertEqual(P.opus_settings(ch(64000))["bitrate"], 128, "never below the old default")
        self.assertEqual(P.opus_settings(ch(256000))["bitrate"], 256)
        self.assertEqual(P.opus_settings(ch(512000))["bitrate"], 384, "capped")
        self.assertEqual(P.opus_settings(object())["bitrate"], 128)
        s = P.opus_settings(ch(96000))
        self.assertEqual((s["signal_type"], s["bandwidth"]), ("music", "full"))

    def test_leveling_filter_runs_in_real_ffmpeg_and_lands_near_target(self):
        import shutil
        import subprocess
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg not installed")
        base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i"]
        for level in ("0.02", "0.9"):   # a very quiet and a very loud station
            src = f"sine=frequency=440:duration=6:sample_rate=22050,volume={level}"
            out = subprocess.run(base + [src, "-vn", "-af", P.LEVEL_FILTER, "-f", "s16le", "-ar", "48000", "-ac", "2", "-"],
                                 capture_output=True, check=True).stdout
            self.assertGreater(len(out), 48000 * 4 * 5, "audio comes out")
            import array
            samples = array.array("h", out[:len(out) // 2 * 2])
            self.assertLess(max(abs(x) for x in samples), 32768 * 0.86, "the limiter keeps peaks below clipping")


class SongQueue(unittest.IsolatedAsyncioTestCase):
    """Free-licensed songs play one after another on a public server."""

    def setUp(self):
        import library as L
        self.L = L
        self.songs = [L.Song(str(i), f"Song {i}", "Band", "Rock", L.FREE, url=f"http://93.184.216.34/s{i}.mp3") for i in range(3)]
        self.g = make_guild()
        self.ch = make_channel(self.g, users=(1,))
        self.g.voice_client = None
        bot = MagicMock()
        bot.get_guild = lambda gid: self.g
        bot.loop = asyncio.get_event_loop_policy().get_event_loop() if False else None
        self.bot = bot
        self.radio = P.PublicRadio(bot, store=MagicMock(delete=AsyncMock(), save=AsyncMock(), update_volume=AsyncMock()))
        self.radio.changed = MagicMock()
        patcher = mock.patch.object(P.discord, "FFmpegPCMAudio", Silent)
        patcher.start()
        self.addCleanup(patcher.stop)

        async def connect(**kw):
            vc = FakeVC(self.ch)
            self.g.voice_client = vc
            return vc
        self.ch.connect = connect

    async def asyncSetUp(self):
        self.bot.loop = asyncio.get_running_loop()

    async def start(self):
        with mock.patch.object(P, "check_stream_url", AsyncMock(return_value=None)):
            return await self.radio.play_songs(make_member(self.g, 1, self.ch), self.songs)

    async def test_playing_songs_starts_the_first_and_is_not_saved(self):
        ok, msg = await self.start()
        self.assertTrue(ok, msg)
        player = self.radio.players[1]
        self.assertTrue(player.is_library)
        self.assertEqual(player.station.title, "Song 0")
        self.radio.store.delete.assert_awaited()           # a restart must not resurrect a song session
        self.radio.store.save.assert_not_awaited()

    async def test_a_finished_song_moves_on_and_wraps_around(self):
        await self.start()
        player, vc = self.radio.players[1], self.g.voice_client
        for expected in ("Song 1", "Song 2", "Song 0"):
            vc.after(None)                                   # discord.py reports the song ended
            await asyncio.sleep(0.02)
            self.assertEqual(player.station.title, expected)
            self.assertEqual(vc.source.original.inner.kwargs.get("before_options"), P.FFMPEG_BEFORE)

    async def test_a_source_we_replaced_cannot_trigger_a_skip(self):
        await self.start()
        player, vc = self.radio.players[1], self.g.voice_client
        stale = vc.after                                     # callback of song 0
        self.assertTrue(self.radio.skip(1))
        self.assertEqual(player.station.title, "Song 1")
        stale(None)                                          # the replaced source reports in late
        await asyncio.sleep(0.02)
        self.assertEqual(player.station.title, "Song 1", "a stale end must not skip another song")

    async def test_skip_only_applies_to_songs(self):
        self.assertFalse(self.radio.skip(1))
        await self.start()
        self.assertTrue(self.radio.skip(1))

    async def test_songs_that_keep_failing_stop_the_session(self):
        await self.start()
        player, vc = self.radio.players[1], self.g.voice_client
        player.started = False
        stop = AsyncMock()
        self.radio.stop = stop
        self.radio._notify = AsyncMock()
        for _ in range(P.RESTART_LIMIT):
            player.started = False
            self.radio._advance(player)
        await asyncio.sleep(0.02)
        stop.assert_awaited()

    async def test_unsafe_song_url_is_refused(self):
        with mock.patch.object(P, "check_stream_url", AsyncMock(return_value="That address isn't allowed")):
            ok, msg = await self.radio.play_songs(make_member(self.g, 1, self.ch), self.songs)
        self.assertFalse(ok)
        self.assertNotIn(1, self.radio.players)
