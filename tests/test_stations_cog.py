import types
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

import directory as D
from cogs import stations as S


def rec(i, name=None, state="Illinois", city="Chicago", tags="rock", votes=None):
    return {"stationuuid": f"u{i}", "name": name or f"WA{i:02d} {90 + i % 10}.{i % 10} {city}, IL",
            "url": f"http://93.184.216.34/{i}", "url_resolved": f"http://93.184.216.34/{i}", "tags": tags,
            "state": f"{city}, IL" if city else state, "votes": votes if votes is not None else 100 - i,
            "clickcount": 0, "codec": "MP3", "bitrate": 128, "lastcheckok": 1}


def make_cog(records, home=None):
    bot = MagicMock()
    bot.directory = D.Directory()
    bot.directory.load_records(records)
    bot.public.tune = AsyncMock(return_value=(True, "ok"))
    bot.public.players = {}
    S.HOME_GUILD_ID = home
    return S.Stations(bot), bot


def interaction(uid=1, guild_id=5, namespace=None):
    i = MagicMock()
    i.user = types.SimpleNamespace(id=uid, display_name="Pat")
    i.guild_id = guild_id
    i.namespace = types.SimpleNamespace(**(namespace or {}))
    i.response.send_message = AsyncMock()
    i.response.edit_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    i.channel.send = AsyncMock()
    return i


class Autocomplete(unittest.IsolatedAsyncioTestCase):
    async def test_states(self):
        records = [rec(i) for i in range(30)] + [dict(rec(100 + i, state="Texas", city=None), name=f"KT{i} Austin", tags="country") for i in range(5)]
        records.append(dict(rec(200, state="", city=None), name="Online Only Radio", state=""))
        cog, _ = make_cog(records)
        shown = await cog.state_autocomplete(interaction(), "")
        self.assertLessEqual(len(shown), 25)
        self.assertEqual(shown[0].value, S.ONLINE, "online-only option is offered first")
        self.assertEqual(shown[1].value, "IL", "then the states with the most stations")
        self.assertEqual([c.value for c in await cog.state_autocomplete(interaction(), "tex")], ["TX"])
        self.assertEqual([c.value for c in await cog.state_autocomplete(interaction(), "il")], ["IL"])
        self.assertEqual([c.value for c in await cog.state_autocomplete(interaction(), "online")], [S.ONLINE])
        self.assertEqual(await cog.state_autocomplete(interaction(), "zzz"), [])

    async def test_cities(self):
        cog, _ = make_cog([rec(i) for i in range(6)] + [rec(50 + i, city="Rockford") for i in range(3)])
        got = await cog.city_autocomplete(interaction(namespace={"state": "IL"}), "")
        self.assertEqual([c.value for c in got], ["Chicago", "Rockford"])
        self.assertEqual([c.value for c in await cog.city_autocomplete(interaction(namespace={"state": "IL"}), "rock")], ["Rockford"])
        self.assertEqual(await cog.city_autocomplete(interaction(namespace={}), ""), [])
        self.assertEqual(await cog.city_autocomplete(interaction(namespace={"state": S.ONLINE}), ""), [])


class Commands(unittest.IsolatedAsyncioTestCase):
    async def test_home_server_is_told_it_has_the_rotation(self):
        cog, _ = make_cog([rec(1)], home="5")
        i = interaction(guild_id=5)
        await cog.browse.callback(cog, i, state="IL")
        self.assertIn("rotation", i.response.send_message.await_args.args[0])
        self.assertTrue(i.response.send_message.await_args.kwargs["ephemeral"])

    async def test_directory_still_loading(self):
        cog, _ = make_cog([])
        i = interaction()
        await cog.search.callback(cog, i, query="wls")
        self.assertIn("loading", i.response.send_message.await_args.args[0])

    async def test_browse_results_are_private_and_paged(self):
        cog, _ = make_cog([rec(i) for i in range(60)])
        i = interaction()
        await cog.browse.callback(cog, i, state="IL", city="Chicago")
        kw = i.response.send_message.await_args.kwargs
        self.assertTrue(kw["ephemeral"])
        self.assertIsInstance(kw["view"], S.StationPicker)
        self.assertIn("Chicago, IL", kw["embed"].title)

    async def test_bad_state_and_empty_results(self):
        cog, _ = make_cog([rec(i) for i in range(3)])
        i = interaction()
        await cog.browse.callback(cog, i, state="Narnia")
        self.assertIn("Pick a state", i.response.send_message.await_args.args[0])
        i = interaction()
        await cog.browse.callback(cog, i, state="IL", city="Nowhere")
        self.assertIn("No stations found", i.response.send_message.await_args.args[0])
        i = interaction()
        await cog.search.callback(cog, i, query="zzzzqq")
        self.assertIn("No stations matched", i.response.send_message.await_args.args[0])

    async def test_search_finds_by_call_letters_and_frequency(self):
        cog, _ = make_cog([rec(i) for i in range(20)])
        i = interaction()
        await cog.search.callback(cog, i, query="WA07")
        self.assertIsInstance(i.response.send_message.await_args.kwargs["view"], S.StationPicker)

    async def test_stop_volume_and_now_respect_control(self):
        cog, bot = make_cog([rec(1)])
        i = interaction()
        await cog.now.callback(cog, i)
        self.assertIn("Nothing is playing", i.response.send_message.await_args.args[0])
        bot.public.players = {5: types.SimpleNamespace(station=D.Station("u", "WXYZ", "http://x", "IL", "Chicago", "Chicago"))}
        bot.public.may_control = MagicMock(return_value=False)
        bot.public.stop = AsyncMock()
        i = interaction()
        await cog.stop.callback(cog, i)
        bot.public.stop.assert_not_awaited()
        bot.public.may_control = MagicMock(return_value=True)
        i = interaction(); i.guild = MagicMock()
        await cog.stop.callback(cog, i)
        bot.public.stop.assert_awaited()
        bot.public.set_volume = MagicMock()
        i = interaction()
        await cog.volume.callback(cog, i, level=40)
        bot.public.set_volume.assert_called_once_with(5, 0.4)


class Picker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog, self.bot = make_cog([rec(i) for i in range(60)])
        self.stations = self.cog.bot.directory.browse("IL", "Chicago")

    def view(self):
        return S.StationPicker(self.cog, 1, self.stations, "Chicago, IL")

    async def test_paging(self):
        v = self.view()
        self.assertEqual((v.pages, len(v.current())), (3, 25))
        select, prev_b, next_b, close_b = v.children
        self.assertLessEqual(len(select.options), 25)
        self.assertTrue(prev_b.disabled)
        self.assertFalse(next_b.disabled)
        await v._next(interaction())
        self.assertEqual(v.page, 1)
        await v._next(interaction()); await v._next(interaction()); await v._next(interaction())
        self.assertEqual(v.page, 2, "cannot page past the end")
        self.assertTrue(v.children[2].disabled)
        await v._prev(interaction())
        self.assertEqual(v.page, 1)
        self.assertIn("page 2/3", v.embed().footer.text)

    async def test_select_options_are_valid_for_discord(self):
        v = self.view()
        for page in range(3):
            v.page = page
            v._render()
            for o in v.children[0].options:
                self.assertTrue(0 < len(o.label) <= 100 and 0 < len(o.value) <= 100)
                self.assertTrue(o.description is None or len(o.description) <= 100)

    async def test_only_the_owner_can_use_it(self):
        v = self.view()
        i = interaction(uid=99)
        self.assertFalse(await v.interaction_check(i))
        self.assertTrue(i.response.send_message.await_args.kwargs["ephemeral"])
        self.assertTrue(await v.interaction_check(interaction(uid=1)))

    async def test_picking_a_station_tunes_and_announces(self):
        v = self.view()
        i = interaction()
        i.data = {"values": [self.stations[3].uuid]}
        await v._picked(i)
        self.bot.public.tune.assert_awaited_once()
        self.assertEqual(self.bot.public.tune.await_args.args[1].uuid, self.stations[3].uuid)
        self.assertEqual(i.channel.send.await_args.kwargs["embed"].author.name, "Tuned by Pat")
        self.assertTrue(i.followup.send.await_args.kwargs["ephemeral"])

    async def test_failures_are_shown_privately(self):
        self.bot.public.tune = AsyncMock(return_value=(False, "Join a voice channel first, then pick a station."))
        v = self.view()
        i = interaction(); i.data = {"values": [self.stations[0].uuid]}
        await v._picked(i)
        i.channel.send.assert_not_awaited()
        self.assertIn("Join a voice channel", i.followup.send.await_args.args[0])
        i = interaction(); i.data = {"values": ["gone"]}
        await v._picked(i)
        self.assertIn("no longer in the list", i.followup.send.await_args.args[0])

    async def test_announcement_falls_back_when_the_bot_cannot_post(self):
        v = self.view()
        i = interaction(); i.data = {"values": [self.stations[0].uuid]}
        i.channel.send = AsyncMock(side_effect=discord.HTTPException(types.SimpleNamespace(status=403, reason="x"), "x"))
        await v._picked(i)
        self.assertIn("embed", i.followup.send.await_args.kwargs)

    async def test_station_names_cannot_ping_people(self):
        evil = dict(rec(1), name="@everyone free <@123> **bold**")
        cog, _ = make_cog([evil])
        view = S.StationPicker(cog, 1, cog.bot.directory.stations, "x")
        text = view.embed().description
        self.assertNotIn("@everyone", text.replace("@​everyone", ""))
        self.assertIn("​", text)


if __name__ == "__main__":
    unittest.main()
