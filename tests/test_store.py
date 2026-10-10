import asyncio
import os
import shutil
import sqlite3
import tempfile
import unittest

import directory as D
import store as S


def station(uuid="u1", name="WXYZ 95.5"):
    return D.Station(uuid=uuid, name=name, url="http://93.184.216.34/live", state="IL", city="Chicago", market="Chicago",
                     tags=("rock", "80s"), genres=frozenset({"Rock", "Oldies"}), bitrate=128, codec="MP3",
                     homepage="https://wxyz.example", favicon="", votes=42, clicks=7, local=True)


def saved(gid=1, **kw):
    base = dict(guild_id=gid, station=station(), url="http://93.184.216.34/live", voice_channel_id=10,
                text_channel_id=20, started_by=30, volume=0.6)
    base.update(kw)
    return S.SavedRadio(**base)


class PanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_panels_are_saved_replaced_and_deleted(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = S.Store(tmp)
        await store.open()
        await store.save_panel(1, 10, 100)
        await store.save_panel(1, 11, 101)
        await store.save_panel(2, 20, 200)
        self.assertEqual(await store.all_panels(), {1: (11, 101), 2: (20, 200)})
        await store.delete_panel(1)
        self.assertEqual(await store.all_panels(), {2: (20, 200)})
        await store.close()
        again = S.Store(tmp)
        await again.open()
        self.assertEqual(await again.all_panels(), {2: (20, 200)}, "survives a restart")
        await again.close()


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = S.Store(self.tmp)
        await self.store.open()
        self.addAsyncCleanup(self.store.close)

    async def test_empty_store(self):
        self.assertEqual(await self.store.all(), [])

    async def test_round_trip_keeps_every_station_field(self):
        await self.store.save(saved())
        [row] = await self.store.all()
        self.assertEqual(row, saved())
        self.assertIsInstance(row.station.genres, frozenset)
        self.assertIsInstance(row.station.tags, tuple)

    async def test_save_replaces_the_same_server(self):
        await self.store.save(saved())
        await self.store.save(saved(station=station("u2", "KABC"), volume=0.3, voice_channel_id=11))
        rows = await self.store.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].station.uuid, rows[0].volume, rows[0].voice_channel_id), ("u2", 0.3, 11))

    async def test_update_volume_and_delete(self):
        await self.store.save(saved(1))
        await self.store.save(saved(2))
        await self.store.update_volume(1, 0.25)
        await self.store.delete(2)
        rows = {r.guild_id: r for r in await self.store.all()}
        self.assertEqual(set(rows), {1})
        self.assertEqual(rows[1].volume, 0.25)
        await self.store.delete(999)          # deleting something that isn't there is fine
        await self.store.update_volume(999, 0.5)

    async def test_survives_a_restart(self):
        await self.store.save(saved(7))
        await self.store.close()
        again = S.Store(self.tmp)
        await again.open()
        self.addAsyncCleanup(again.close)
        self.assertEqual([r.guild_id for r in await again.all()], [7])

    async def test_many_concurrent_writes(self):
        await asyncio.gather(*(self.store.save(saved(i)) for i in range(60)))
        await asyncio.gather(*(self.store.update_volume(i, 0.1 + i / 1000) for i in range(60)))
        rows = await self.store.all()
        self.assertEqual(len(rows), 60)

    async def test_unreadable_record_is_skipped_not_fatal(self):
        await self.store.save(saved(1))
        self.store._run("INSERT INTO guild_radio VALUES (2, 'not json', 'u', 1, NULL, 1, 0.5, 0)")
        self.assertEqual([r.guild_id for r in await self.store.all()], [1])

    async def test_tolerates_station_fields_added_or_removed_later(self):
        text = S.station_to_json(station())
        import json
        data = json.loads(text)
        data["new_field_from_the_future"] = 1
        del data["favicon"]
        back = S.station_from_json(json.dumps(data))
        self.assertEqual(back.uuid, "u1")

    async def test_closed_store_raises_instead_of_hanging(self):
        await self.store.close()
        with self.assertRaises(RuntimeError):
            await self.store.save(saved())
        await self.store.open()   # reopen for cleanup


class StoreEdgeCases(unittest.IsolatedAsyncioTestCase):
    async def test_corrupt_database_is_moved_aside_and_replaced(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with open(os.path.join(tmp, "crue.db"), "wb") as f:
            f.write(b"this is definitely not a sqlite database" * 50)
        store = S.Store(tmp)
        await store.open()
        self.addAsyncCleanup(store.close)
        self.assertTrue(any(n.startswith("crue.db.corrupt-") for n in os.listdir(tmp)))
        await store.save(saved())
        self.assertEqual(len(await store.all()), 1)

    async def test_unwritable_directory_falls_back_to_local_disk(self):
        blocker = tempfile.NamedTemporaryFile(delete=False)   # a *file* where the directory should be
        blocker.close()
        self.addCleanup(os.remove, blocker.name)
        store = S.Store(os.path.join(blocker.name, "sub"))
        await store.open()
        self.addAsyncCleanup(store.close)
        self.addCleanup(shutil.rmtree, store.dir, ignore_errors=True)
        self.assertFalse(store.persistent)
        await store.save(saved())
        self.assertEqual(len(await store.all()), 1)

    async def test_plain_directory_is_reported_as_not_persistent(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = S.Store(tmp)
        with self.assertLogs("store", level="WARNING") as logs:
            await store.open()
        self.addAsyncCleanup(store.close)
        self.assertIn("No persistent volume", logs.output[0])

    async def test_database_uses_wal_mode(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = S.Store(tmp)
        await store.open()
        self.addAsyncCleanup(store.close)
        mode = sqlite3.connect(store.path).execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")


if __name__ == "__main__":
    unittest.main()
