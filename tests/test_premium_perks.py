import tempfile
import unittest
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import directory as D
import premium as PR
import publicradio as P
from store import Store
from tests.test_publicradio import make_radio, station


class Favorites(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.radio, self.bot = make_radio([])
        self.radio.store = Store(tempfile.mkdtemp())
        await self.radio.store.open()
        self.bot.premium = PR.Premium(None, sku_id="111")

    async def asyncTearDown(self):
        await self.radio.store.close()

    async def test_free_limit_then_premium_limit(self):
        for i in range(PR.FREE_FAVORITES):
            self.assertIsNone(await self.radio.add_favorite(1, station(f"S{i}", f"u{i}")))
        problem = await self.radio.add_favorite(1, station("One too many", "x"))
        self.assertIn("full", problem)
        self.assertIn("/premium", problem)
        self.bot.premium.guilds.add(1)
        self.assertIsNone(await self.radio.add_favorite(1, station("One too many", "x")))
        self.assertEqual(self.radio.favorite_limit(1), PR.PREMIUM_FAVORITES)

    async def test_duplicates_and_removal_and_persistence(self):
        self.assertIsNone(await self.radio.add_favorite(1, station("A", "a")))
        self.assertIn("already", await self.radio.add_favorite(1, station("A", "a")))
        self.assertIsNone(await self.radio.add_favorite(2, station("A", "a")), "favorites are per server")
        self.radio._favs = await self.radio.store.all_favorites()      # what a restart reloads
        self.assertEqual([s.name for s in self.radio.favorites_for(1)], ["A"])
        self.assertTrue(await self.radio.remove_favorite(1, "a"))
        self.assertFalse(await self.radio.remove_favorite(1, "a"))
        self.radio._favs = await self.radio.store.all_favorites()
        self.assertEqual(self.radio.favorites_for(1), [])
        self.assertEqual(len(self.radio.favorites_for(2)), 1)


class AlwaysOn(unittest.TestCase):
    def test_is_premium_helper_is_safe_with_a_plain_mock_bot(self):
        radio, _ = make_radio([])
        self.assertFalse(radio.is_premium(1))
        self.assertEqual(radio.favorite_limit(1), 3)


if __name__ == "__main__":
    unittest.main()


class RemovedFromServer(unittest.IsolatedAsyncioTestCase):
    async def test_everything_saved_for_a_server_is_deleted(self):
        import theme as T
        radio, bot = make_radio([])
        radio.store = Store(tempfile.mkdtemp())
        await radio.store.open()
        bot.premium = PR.Premium(None, sku_id="111")
        for gid in (1, 2):
            await radio.add_favorite(gid, station("A", "a"))
            await radio.save_theme(T.Theme(gid, "Name", 0xFF0000))
        await radio.forget_guild(1)
        radio._favs, radio._themes = await radio.store.all_favorites(), await radio.store.all_themes()
        self.assertEqual((list(radio._favs), list(radio._themes)), ([2], [2]))
        await radio.store.close()
