import types
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

import library as L
import libpicker as LP
from cogs import library as CL
from tests.test_picker import valid
from tests.test_stations_cog import interaction


def make_cog(free_key="KEY", home=None):
    bot = MagicMock()
    bot.library = L.Library(client_id=free_key)
    bot.library.free = AsyncMock(return_value=[L.Song(str(i), f"Free {i}", "Indie Band", "Rock", L.FREE, url=f"https://s/{i}.mp3", duration=200) for i in range(25)])
    bot.public.play_songs = AsyncMock(return_value=(True, "Now playing **Free 0**"))
    bot.public.skip = MagicMock(return_value=True)
    bot.public.may_control = MagicMock(return_value=True)
    bot.public.players = {}
    bot.play_songs = AsyncMock(return_value=(True, "Now playing **X** by Y"))
    bot.skip_song = AsyncMock(return_value=(True, "Skipped."))
    CL.HOME_GUILD_ID = home
    return CL.SongLibrary(bot), bot


class Browsing(unittest.IsolatedAsyncioTestCase):
    async def test_home_starts_on_hits_and_can_switch_to_free(self):
        cog, _ = make_cog(home="5")
        v = LP.LibraryView(cog, 1, home=True)
        valid(v)
        self.assertEqual((v.source, v.screen), (L.HITS, "genres"))
        select = v.children[0]
        self.assertEqual(len(select.options), len(L.GENRES))
        i = interaction()
        await v._source_clicked(L.FREE)(i)
        self.assertEqual(v.source, L.FREE)
        valid(v)

    async def test_other_servers_only_get_free_music(self):
        cog, _ = make_cog()
        v = LP.LibraryView(cog, 1, home=False)
        self.assertEqual(v.sources, [L.FREE])
        self.assertTrue(all(c.label != "Popular hits" for c in v.children if isinstance(c, discord.ui.Button)))

    async def test_free_music_works_without_a_jamendo_key(self):
        cog, bot = make_cog(free_key="")
        i = interaction()
        await cog.open_library(i)
        kw = i.response.send_message.await_args.kwargs
        self.assertEqual(kw["view"].sources, [L.FREE], "ccMixter needs no key")
        self.assertNotIn("Metal", [o.value for o in kw["view"].children[0].options], "only genres that have songs are offered")
        home_view = LP.LibraryView(cog, 1, home=True)
        self.assertEqual(home_view.sources, [L.HITS, L.FREE])

    async def test_genre_shows_songs_with_a_valid_layout(self):
        cog, _ = make_cog(home="5")
        v = LP.LibraryView(cog, 1, home=True)
        i = interaction()
        i.data = {"values": ["Rock"]}
        i.response.defer = AsyncMock()
        i.edit_original_response = AsyncMock()
        await v._genre_picked(i)
        self.assertEqual((v.screen, v.genre, len(v.songs)), ("songs", "Rock", 12))
        valid(v)
        desc = v.embed().description
        self.assertIn("Foo Fighters", desc)
        self.assertNotIn("@everyone", desc)
        i.edit_original_response.assert_awaited_once()

    async def test_free_songs_page_forward_and_back(self):
        cog, bot = make_cog()
        v = LP.LibraryView(cog, 1, home=False)
        await v.show_genre("Rock")
        valid(v)
        self.assertTrue(v.more)
        buttons = {c.label: c for c in v.children if isinstance(c, discord.ui.Button)}
        self.assertTrue(buttons["◀ Prev"].disabled)
        self.assertFalse(buttons["Next ▶"].disabled)
        i = interaction()
        i.response.defer = AsyncMock()
        i.edit_original_response = AsyncMock()
        await v._next(i)
        bot.library.free.assert_awaited_with("Rock", 1)
        self.assertEqual(v.page, 1)
        await v._prev(i)
        self.assertEqual(v.page, 0)

    async def test_picking_a_song_plays_it_then_the_rest_of_the_list(self):
        cog, bot = make_cog()
        v = LP.LibraryView(cog, 1, home=False)
        await v.show_genre("Rock")
        i = interaction()
        i.data = {"values": ["3"]}
        await v._song_picked(i)
        songs = bot.public.play_songs.await_args.args[1]
        self.assertEqual([s.id for s in songs][:3], ["3", "4", "5"])
        self.assertEqual(len(songs), 25)
        self.assertEqual(songs[-1].id, "2", "wraps around to the songs before it")
        self.assertIn("rest of this list", i.followup.send.await_args.args[0])

    async def test_shuffle_plays_every_song_once(self):
        cog, bot = make_cog()
        v = LP.LibraryView(cog, 1, home=False)
        await v.show_genre("Rock")
        await v._shuffle(interaction())
        songs = bot.public.play_songs.await_args.args[1]
        self.assertEqual(sorted(s.id for s in songs), sorted(s.id for s in v.songs))

    async def test_a_refused_play_is_explained(self):
        cog, bot = make_cog()
        bot.public.play_songs.return_value = (False, "Join a voice channel first, then pick a song.")
        v = LP.LibraryView(cog, 1, home=False)
        await v.show_genre("Rock")
        i = interaction()
        i.data = {"values": ["0"]}
        await v._song_picked(i)
        self.assertIn("Join a voice channel", i.followup.send.await_args.args[0])

    async def test_someone_elses_menu_is_refused(self):
        cog, _ = make_cog()
        v = LP.LibraryView(cog, 1, home=False)
        i = interaction(uid=2)
        self.assertFalse(await v.interaction_check(i))


class Routing(unittest.IsolatedAsyncioTestCase):
    async def test_home_plays_through_the_home_radio(self):
        cog, bot = make_cog(home="5")
        songs = [L.Song("1", "A", "B", "Rock", L.HITS)]
        ok, _ = await cog.play(interaction(guild_id=5), songs)
        bot.play_songs.assert_awaited_once()
        bot.public.play_songs.assert_not_awaited()

    async def test_public_servers_never_receive_the_hits(self):
        cog, bot = make_cog(home="5")
        songs = [L.Song("1", "Hit", "B", "Rock", L.HITS), L.Song("2", "Free", "B", "Rock", L.FREE, url="https://s/2.mp3")]
        await cog.play(interaction(guild_id=9), songs)
        sent = bot.public.play_songs.await_args.args[1]
        self.assertEqual([s.title for s in sent], ["Free"], "hits are not licensed for a public bot")

    async def test_skip_routes_by_server_and_checks_control(self):
        cog, bot = make_cog(home="5")
        i = interaction(guild_id=5)
        await cog.skip(i)
        bot.skip_song.assert_awaited_once()
        bot.public.players = {9: types.SimpleNamespace(is_library=True)}
        i = interaction(guild_id=9)
        await cog.skip(i)
        bot.public.skip.assert_called_once_with(9)
        bot.public.may_control.return_value = False
        bot.public.skip.reset_mock()
        i = interaction(guild_id=9)
        await cog.skip(i)
        bot.public.skip.assert_not_called()
        bot.public.players = {}
        i = interaction(guild_id=9)
        await cog.skip(i)
        self.assertIn("No library songs", i.response.send_message.await_args.args[0])
