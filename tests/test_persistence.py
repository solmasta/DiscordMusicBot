import asyncio
import shutil
import tempfile
import types
import unittest
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import discord

import publicradio as P
import store as S
from tests.test_publicradio import FakeVC, Silent, make_channel, make_guild, make_member, station


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = mock.patch.object(P.discord, "FFmpegPCMAudio", Silent)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = S.Store(self.tmp)
        await self.store.open()
        self.addAsyncCleanup(self.store.close)
        self.g = make_guild()
        self.ch = self.make_voice_channel(self.g, 10, users=(1,))
        self.g.get_channel = lambda cid: self.ch if cid == 10 else None
        self.sent = []
        text = MagicMock()
        text.send = AsyncMock(side_effect=lambda t: self.sent.append(t))
        self.bot = MagicMock()
        self.bot.is_ready = lambda: True
        self.bot.directory.resolve_url = AsyncMock(return_value="http://93.184.216.34/live")
        self.bot.get_guild = lambda gid: self.g if gid == 1 else None
        self.bot.get_channel = lambda cid: text if cid == 20 else None
        self.radio = P.PublicRadio(self.bot, store=self.store)
        gap = mock.patch.object(P, "RESUME_GAP_S", 0)
        gap.start()
        self.addCleanup(gap.stop)

    def make_voice_channel(self, guild, cid, users=(), allow=True):
        ch = MagicMock(spec=discord.VoiceChannel)
        ch.id, ch.guild, ch.mention, ch.connects = cid, guild, f"<#{cid}>", 0
        ch.voice_states = {u: object() for u in users}
        ch.permissions_for = lambda me: types.SimpleNamespace(connect=allow, speak=allow)

        async def connect(**kw):
            ch.connects += 1
            vc = FakeVC(ch)
            guild.voice_client = vc
            return vc
        ch.connect = connect
        return ch

    def saved(self, **kw):
        base = dict(guild_id=1, station=station("Good FM", "good"), url="http://93.184.216.34/live",
                    voice_channel_id=10, text_channel_id=20, started_by=1, volume=0.55)
        base.update(kw)
        return S.SavedRadio(**base)

    async def stored(self):
        return {r.guild_id: r for r in await self.store.all()}

    async def settle(self):
        await asyncio.gather(*list(self.radio._tasks))


class Saving(Base):
    async def test_a_successful_tune_is_saved(self):
        m = make_member(self.g, 1, self.ch)
        ok, _ = await self.radio.tune(m, station("Good FM", "good"), text_channel_id=20)
        self.assertTrue(ok)
        row = (await self.stored())[1]
        self.assertEqual((row.station.name, row.voice_channel_id, row.text_channel_id, row.started_by), ("Good FM", 10, 20, 1))
        self.assertEqual(row.volume, P.DEFAULT_VOLUME)

    async def test_failed_tunes_are_not_saved(self):
        FakeVC.dead_urls = {"http://93.184.216.34/live"}
        self.addCleanup(FakeVC.dead_urls.clear)
        ok, _ = await self.radio.tune(make_member(self.g, 1, self.ch), station("Dead", "dead"))
        self.assertFalse(ok)
        self.assertEqual(await self.stored(), {})

    async def test_volume_changes_are_saved(self):
        await self.radio.tune(make_member(self.g, 1, self.ch), station("Good FM", "good"))
        self.radio.set_volume(1, 0.2)
        await self.settle()
        self.assertEqual((await self.stored())[1].volume, 0.2)
        self.assertEqual(self.radio._saved[1].volume, 0.2)

    async def test_a_broken_store_never_breaks_playback(self):
        self.store.save = AsyncMock(side_effect=RuntimeError("disk full"))
        ok, _ = await self.radio.tune(make_member(self.g, 1, self.ch), station("Good FM", "good"))
        self.assertTrue(ok)
        self.assertTrue(self.g.voice_client.playing)

    async def test_start_loads_saved_servers_and_survives_a_dead_store(self):
        await self.store.save(self.saved())
        radio = P.PublicRadio(self.bot, store=self.store)
        await radio.start()
        self.assertEqual(list(radio._saved), [1])
        broken = MagicMock()
        broken.open = AsyncMock(side_effect=OSError("no disk"))
        radio2 = P.PublicRadio(self.bot, store=broken)
        await radio2.start()                       # logs, does not raise
        self.assertEqual(radio2._saved, {})


class Forgetting(Base):
    async def start_playing(self):
        await self.radio.tune(make_member(self.g, 1, self.ch), station("Good FM", "good"), 20)
        self.assertIn(1, await self.stored())

    async def test_stopping_on_purpose_stays_stopped(self):
        await self.start_playing()
        await self.radio.stop(self.g, "stopped by a user")
        self.assertEqual(await self.stored(), {})
        self.assertNotIn(1, self.radio._saved)

    async def test_idle_leave_and_repeated_failure_forget(self):
        await self.start_playing()
        self.ch.voice_states = {}
        with mock.patch.object(P, "IDLE_LEAVE_S", 0):
            await P.PublicRadio.monitor.coro(self.radio)
        self.assertEqual(await self.stored(), {})

    async def test_restart_keeps_the_record(self):
        await self.start_playing()
        await self.radio.shutdown()
        self.assertIn(1, await self.stored(), "a deploy must not erase what was playing")

    async def test_being_kicked_or_removed_forgets(self):
        await self.start_playing()
        self.g.voice_client = None
        await P.PublicRadio.monitor.coro(self.radio)
        self.assertEqual(await self.stored(), {})

    async def test_bot_removed_from_a_server_deletes_its_data(self):
        await self.start_playing()
        import bot as B
        b = B.MusicBot.__new__(B.MusicBot)
        b.public = self.radio
        await B.MusicBot.on_guild_remove(b, self.g)
        self.assertEqual(await self.stored(), {})
        self.assertEqual(self.radio.players, {})

    async def test_voice_blip_is_tolerated_but_a_long_outage_is_not(self):
        await self.start_playing()
        self.g.voice_client.connected = False
        await P.PublicRadio.monitor.coro(self.radio)
        self.assertIn(1, self.radio.players, "a brief reconnect must not forget the radio")
        self.assertIn(1, await self.stored())
        self.g.voice_client.connected = True
        await P.PublicRadio.monitor.coro(self.radio)
        self.assertIsNone(self.radio.players[1].lost_since, "recovered")
        self.g.voice_client.connected = False
        with mock.patch.object(P, "LOST_GRACE_S", 0):
            await P.PublicRadio.monitor.coro(self.radio)       # starts the clock
            await P.PublicRadio.monitor.coro(self.radio)
        self.assertNotIn(1, self.radio.players)
        self.assertEqual(await self.stored(), {})

    async def test_stage_channels_are_refused(self):
        stage = MagicMock(spec=discord.StageChannel)
        ok, msg = await self.radio.tune(make_member(self.g, 1, stage), station())
        self.assertFalse(ok)
        self.assertIn("Stage", msg)


class Resuming(Base):
    async def with_saved(self, **kw):
        row = self.saved(**kw)
        await self.store.save(row)
        await self.radio.start()
        return row

    async def test_resumes_where_it_left_off_when_people_are_there(self):
        await self.with_saved()
        await self.radio.resume_all()
        player = self.radio.players[1]
        self.assertEqual((player.station.name, player.volume, player.text_channel_id, player.started_by), ("Good FM", 0.55, 20, 1))
        self.assertTrue(self.g.voice_client.playing)
        self.assertEqual(self.g.voice_client.source.volume, 0.55)
        self.assertIn(1, await self.stored())

    async def test_empty_channel_waits_for_someone_to_join(self):
        await self.with_saved()
        self.ch.voice_states = {}
        await self.radio.resume_all()
        self.assertEqual(self.ch.connects, 0, "the bot does not sit alone in an empty channel")
        self.assertIn(1, await self.stored())
        self.ch.voice_states = {5: object()}
        await self.radio.on_join(make_member(self.g, 5, self.ch), self.ch)
        self.assertIn(1, self.radio.players)
        self.assertEqual(self.ch.connects, 1)

    async def test_joining_a_different_channel_or_when_already_playing_does_nothing(self):
        await self.with_saved()
        self.ch.voice_states = {}
        await self.radio.resume_all()
        other = self.make_voice_channel(self.g, 11, users=(5,))
        await self.radio.on_join(make_member(self.g, 5, other), other)
        self.assertEqual(self.radio.players, {})
        self.ch.voice_states = {5: object()}
        await self.radio.on_join(make_member(self.g, 5, self.ch), self.ch)
        await self.radio.on_join(make_member(self.g, 6, self.ch), self.ch)
        self.assertEqual(self.ch.connects, 1, "a second join does not reconnect")

    async def test_simultaneous_joins_resume_only_once(self):
        await self.with_saved()
        self.ch.voice_states = {}
        await self.radio.resume_all()
        self.ch.voice_states = {5: object(), 6: object()}
        await asyncio.gather(self.radio.on_join(make_member(self.g, 5, self.ch), self.ch),
                             self.radio.on_join(make_member(self.g, 6, self.ch), self.ch))
        self.assertEqual(self.ch.connects, 1)

    async def test_deleted_channel_or_removed_server_is_forgotten(self):
        await self.with_saved()
        self.g.get_channel = lambda cid: None
        await self.radio.resume_all()
        self.assertEqual(await self.stored(), {})
        # a server the bot is no longer in
        self.radio._resumed = False
        await self.store.save(self.saved(guild_id=2))
        await self.radio.start()
        await self.radio.resume_all()
        self.assertNotIn(2, await self.stored())

    async def test_server_not_loaded_yet_is_kept(self):
        await self.with_saved()
        self.bot.is_ready = lambda: False
        self.bot.get_guild = lambda gid: None
        await self.radio.resume_all()
        self.assertIn(1, await self.stored())

    async def test_capacity_and_permissions_keep_the_record(self):
        await self.with_saved()
        with mock.patch.object(P, "MAX_STREAMS", 1):
            self.radio.players[500] = MagicMock()
            await self.radio.resume_all()
        self.assertNotIn(1, self.radio.players)
        self.assertIn(1, await self.stored())
        self.radio.players.pop(500)
        self.radio._resumed = False
        self.ch.permissions_for = lambda me: types.SimpleNamespace(connect=False, speak=True)
        await self.radio.resume_all()
        self.assertNotIn(1, self.radio.players)
        self.assertIn(1, await self.stored(), "kept in case permissions are fixed")

    async def test_unsafe_saved_address_is_dropped(self):
        await self.with_saved(url="http://169.254.169.254/latest/meta-data")
        await self.radio.resume_all()
        self.assertEqual(self.ch.connects, 0)
        self.assertEqual(await self.stored(), {})

    async def test_moved_station_is_found_again(self):
        await self.with_saved(url="http://93.184.216.34/old")
        FakeVC.dead_urls = {"http://93.184.216.34/old"}
        self.addCleanup(FakeVC.dead_urls.clear)
        self.bot.directory.resolve_url = AsyncMock(return_value="http://93.184.216.34/new")
        await self.radio.resume_all()
        self.assertEqual(self.radio.players[1].url, "http://93.184.216.34/new")
        self.assertEqual((await self.stored())[1].url, "http://93.184.216.34/new", "the new address is saved")

    async def test_station_that_is_gone_is_dropped_with_a_note(self):
        await self.with_saved()
        FakeVC.dead_urls = {"http://93.184.216.34/live"}
        self.addCleanup(FakeVC.dead_urls.clear)
        await self.radio.resume_all()
        self.assertNotIn(1, self.radio.players)
        self.assertEqual(await self.stored(), {})
        self.assertTrue(any("couldn't resume" in t for t in self.sent))
        self.assertFalse(self.g.voice_client is not None and self.g.voice_client.connected)

    async def test_every_server_resumes_not_just_the_first(self):
        g2 = make_guild(gid=2)
        ch2 = self.make_voice_channel(g2, 12, users=(8,))
        g2.get_channel = lambda cid: ch2 if cid == 12 else None
        table = {1: self.g, 2: g2}
        self.bot.get_guild = lambda gid: table.get(gid)
        await self.store.save(self.saved())
        await self.store.save(self.saved(guild_id=2, voice_channel_id=12, station=station("Two FM", "two")))
        await self.radio.start()
        await self.radio.resume_all()
        self.assertEqual({gid: p.station.name for gid, p in self.radio.players.items()}, {1: "Good FM", 2: "Two FM"})

    async def test_resume_runs_only_once(self):
        await self.with_saved()
        await self.radio.resume_all()
        await self.radio.stop(self.g, "x", forget=False)
        await self.radio.resume_all()
        self.assertEqual(self.radio.players, {})


if __name__ == "__main__":
    unittest.main()
