import types
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

import directory as D
from cogs import stations as S
import picker as PK


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
    bot.public.area_for = MagicMock(return_value=None)
    bot.public.panel_for = MagicMock(return_value=None)
    bot.public.remember_area = AsyncMock()
    bot.public.forget_area = AsyncMock(return_value=True)
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
        self.assertIsInstance(kw["view"], PK.GuidedPicker)
        self.assertEqual(kw["view"].screen, "stations")
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
        self.assertIsInstance(i.response.send_message.await_args.kwargs["view"], PK.GuidedPicker)

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


if __name__ == "__main__":
    unittest.main()
