import json
import os
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

import directory as D
import picker as PK
from cogs import stations as S
from tests.test_stations_cog import interaction, make_cog, rec

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "us_stations_sample.json")
AB = list(D.STATES)


def valid(view: discord.ui.View):
    """Everything Discord enforces on a message's components."""
    rows = view.to_components()
    assert 1 <= len(rows) <= 5, f"{len(rows)} rows"
    total = 0
    for row in rows:
        comps = row["components"]
        assert 1 <= len(comps) <= 5, f"row with {len(comps)} components"
        total += len(comps)
        for c in comps:
            if c["type"] == 3:                                   # select menu
                assert len(comps) == 1, "a select menu must be alone in its row"
                opts = c["options"]
                assert 1 <= len(opts) <= 25, f"{len(opts)} options"
                assert len({o["value"] for o in opts}) == len(opts), "duplicate option values"
                for o in opts:
                    assert 0 < len(o["label"]) <= 100 and 0 < len(o["value"]) <= 100
                    assert len(o.get("description") or "") <= 100
                assert len(c.get("placeholder") or "") <= 150
            else:
                assert len(c.get("label") or "") <= 80
    assert total <= 25
    return rows


def all_states_records(per_state=2):
    out = []
    for i, ab in enumerate(AB):
        for k in range(per_state):
            out.append({"stationuuid": f"{ab}{k}", "name": f"WA{k}{ab} 9{k}.1 FM", "url": f"http://93.184.216.34/{ab}{k}",
                        "url_resolved": f"http://93.184.216.34/{ab}{k}", "tags": "rock,jazz" if k else "country",
                        "state": D.STATES[ab], "votes": 50 - k, "clickcount": 0, "codec": "MP3", "bitrate": 128, "lastcheckok": 1})
    return out


def world_records():
    chi = [rec(i) for i in range(60)]                                          # Chicago: 60, mixed genres
    for i, r in enumerate(chi):
        r["tags"] = "rock" if i % 3 == 0 else ("jazz" if i % 3 == 1 else "news")
    rockford = [rec(100 + i, city="Rockford") for i in range(3)]
    dallas = [dict(rec(200 + i, city="Dallas"), state="Dallas, TX", name=f"KD{i} 9{i}.5 Dallas, TX") for i in range(10)]
    wyoming = [dict(rec(300 + i, city=None), state="Wyoming", name=f"KW{i} 8{i}.1 FM") for i in range(4)]
    online = [dict(rec(400 + i, city=None), state="", name=f"Online Mix {i}") for i in range(5)]
    return chi + rockford + dallas + wyoming + online


class Layout(unittest.IsolatedAsyncioTestCase):
    async def test_states_screen_covers_all_51_states_once_within_discord_limits(self):
        cog, _ = make_cog(all_states_records())
        v = PK.GuidedPicker(cog, 1)
        rows = valid(v)
        seen = [o["value"] for r in rows for c in r["components"] if c["type"] == 3 for o in c["options"]]
        self.assertEqual(sorted(seen), sorted(AB))
        selects = [c for r in rows for c in r["components"] if c["type"] == 3]
        self.assertEqual(len(selects), 3)
        self.assertTrue(selects[0]["placeholder"].startswith("States: Alabama"))
        labels = [c["label"] for r in rows for c in r["components"] if c["type"] == 2]
        self.assertEqual(labels, ["Online & nationwide", "Search", "Close"])

    async def test_state_groups_are_alphabetical_and_balanced(self):
        rows = [(ab, D.STATES[ab], 5) for ab in AB]
        groups = PK.state_groups(rows)
        self.assertEqual(sum(len(g) for g in groups), 51)
        self.assertTrue(all(len(g) <= 25 for g in groups))
        names = [r[1] for g in groups for r in g]
        self.assertEqual(names, sorted(names))

    async def test_real_data_sample_renders_every_screen_validly(self):
        with open(FIXTURE) as f:
            recs = json.load(f)
        cog, _ = make_cog(recs)
        v = PK.GuidedPicker(cog, 1)
        valid(v)
        for ab, _, _ in cog.bot.directory.states()[:8]:
            v.show_areas(ab); valid(v); v.embed()
            v.show_stations(ab, None); valid(v); v.embed()
        v.show_stations(D.ONLINE, None); valid(v)


class Flow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog, self.bot = make_cog(world_records())
        self.i = interaction()
        self.i.data = {}

    def pick(self, values):
        self.i.data = {"values": values}
        return self.i

    async def test_state_then_area_then_station_list(self):
        v = PK.GuidedPicker(self.cog, 1)
        await v._state_picked(self.pick(["IL"]))
        self.assertEqual(v.screen, "areas")
        valid(v)
        options = v.children[0].options
        self.assertEqual(options[0].label, "All of Illinois")
        self.assertEqual(options[0].value, PK.ALL)
        self.assertEqual([o.value for o in options[1:3]], ["Chicago", "Rockford"], "major markets first")
        await v._area_picked(self.pick(["Chicago"]))
        self.assertEqual((v.screen, v.city, len(v.stations)), ("stations", "Chicago", 60))
        self.assertEqual(v.title, "Chicago, IL")
        valid(v)
        self.i.response.edit_message.assert_awaited()

    async def test_whole_state_option(self):
        v = PK.GuidedPicker(self.cog, 1)
        await v._state_picked(self.pick(["IL"]))
        await v._area_picked(self.pick([PK.ALL]))
        self.assertEqual((v.city, len(v.stations)), (None, 63))
        self.assertEqual(v.title, "Illinois")

    async def test_state_without_cities_goes_straight_to_stations(self):
        v = PK.GuidedPicker(self.cog, 1)
        await v._state_picked(self.pick(["WY"]))
        self.assertEqual((v.screen, len(v.stations), v.back_to), ("stations", 4, "states"))
        await v._back(self.i)
        self.assertEqual(v.screen, "states")

    async def test_back_buttons_retrace_the_steps(self):
        v = PK.GuidedPicker(self.cog, 1)
        await v._state_picked(self.pick(["IL"]))
        await v._area_picked(self.pick(["Chicago"]))
        await v._back(self.i)
        self.assertEqual((v.screen, v.state), ("areas", "IL"))
        await v._back(self.i)
        self.assertEqual((v.screen, v.state), ("states", None))
        self.assertEqual(v.stations, [])

    async def test_genre_filter(self):
        v = PK.GuidedPicker(self.cog, 1)
        v.show_stations("IL", "Chicago")
        genre_select = v.children[0]
        values = {o.value: o.description for o in genre_select.options}
        self.assertEqual(values[PK.ALL], "60 stations")
        self.assertEqual(values["Rock"], "20 stations")
        await v._genre_picked(self.pick(["Rock"]))
        self.assertEqual((v.genre, len(v.stations), v.page), ("Rock", 20, 0))
        self.assertIn("Rock", v.embed().title)
        self.assertTrue(any(o.default and o.value == "Rock" for o in v.children[0].options))
        await v._genre_picked(self.pick([PK.ALL]))
        self.assertEqual((v.genre, len(v.stations)), (None, 60))
        valid(v)

    async def test_paging_resets_when_the_filter_changes(self):
        v = PK.GuidedPicker(self.cog, 1)
        v.show_stations("IL", "Chicago")
        self.assertEqual((v.pages, len(v.current())), (3, 25))
        await v._next(self.i); await v._next(self.i)
        self.assertEqual((v.page, len(v.current())), (2, 10))
        prev_b, next_b = v.children[-4], v.children[-3]
        self.assertFalse(prev_b.disabled)
        self.assertTrue(next_b.disabled)
        await v._next(self.i)
        self.assertEqual(v.page, 2, "cannot page past the end")
        await v._genre_picked(self.pick(["Jazz"]))
        self.assertEqual(v.page, 0)
        self.assertIn("page 1/1", v.embed().footer.text)

    async def test_online_and_nationwide_button(self):
        v = PK.GuidedPicker(self.cog, 1)
        await v._online_clicked(self.i)
        self.assertEqual((v.screen, len(v.stations)), ("stations", 5))
        self.assertTrue(all(not s.state for s in v.stations))
        self.assertEqual(v.title, "Nationwide / online")

    async def test_saved_area_shortcut(self):
        v = PK.GuidedPicker(self.cog, 1, saved_area=("TX", "Dallas"))
        buttons = [c for c in v.children if isinstance(c, discord.ui.Button)]
        saved = buttons[0]
        self.assertEqual((saved.label, saved.style), ("Dallas, TX", discord.ButtonStyle.success))
        self.assertIn("Last time: **Dallas, TX**", v.embed().description)
        await v._saved_clicked(self.i)
        self.assertEqual((v.screen, v.state, v.city, len(v.stations)), ("stations", "TX", "Dallas", 10))
        # whole-state memory, an invalid one, and none at all
        self.assertEqual([b for b in PK.GuidedPicker(self.cog, 1, ("IL", None)).children if isinstance(b, discord.ui.Button)][0].label, "Illinois")
        none = PK.GuidedPicker(self.cog, 1, None)
        self.assertNotIn("📍", [str(getattr(b, "emoji", "")) for b in none.children])
        bad = PK.GuidedPicker(self.cog, 1, ("ZZ", None))
        self.assertEqual(len([b for b in bad.children if isinstance(b, discord.ui.Button)]), 3)

    async def test_locations_are_shown_only_when_they_add_information(self):
        recs = [dict(rec(900 + i, name=f"Plain Radio {i}", city="Chicago")) for i in range(3)]
        recs += [dict(rec(950, name="WLS 94.7 Chicago, IL", city="Chicago"))]
        cog, _ = make_cog(recs + [dict(rec(960, name="Texas Plain", city=None), state="Texas")])
        v = PK.GuidedPicker(cog, 1)
        v.show_stations("IL", None)
        lines = v.embed().description.splitlines()
        self.assertTrue(any("Plain Radio 0 · Chicago, IL" in line for line in lines), "whole-state lists say where each station is")
        self.assertTrue(any(line.endswith("WLS 94.7 Chicago, IL") for line in lines), "...unless the name already says so")
        v.show_stations("IL", "Chicago")
        self.assertNotIn(" · ", v.embed().description, "already inside Chicago, so repeating it adds nothing")
        v.show_results(cog.bot.directory.search("Plain"), "Results")
        self.assertIn("Chicago, IL", v.embed().description, "search results always show where")

    async def test_counts_are_grammatical(self):
        self.assertEqual((PK.count(1), PK.count(0), PK.count(2), PK.count(1234)), ("1 station", "0 stations", "2 stations", "1,234 stations"))
        v = PK.GuidedPicker(self.cog, 1)
        v.show_results(v.directory.search("Online Mix 1"), "x")
        self.assertIn("1 station ·", v.embed().footer.text)

    async def test_embeds_guide_the_user(self):
        v = PK.GuidedPicker(self.cog, 1)
        self.assertIn("Join a voice channel", v.embed().description)
        self.assertIn("Where are you listening from", v.embed().description)
        v.show_areas("IL")
        self.assertIn("All of Illinois", v.embed().description)


class Searching(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog, self.bot = make_cog(world_records())

    async def test_search_button_opens_a_modal(self):
        v = PK.GuidedPicker(self.cog, 1)
        i = interaction(); i.response.send_modal = AsyncMock()
        await v._search_clicked(i)
        modal = i.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, PK.SearchModal)
        self.assertEqual(modal.title, "Search stations")
        self.assertEqual((modal.query.min_length, modal.query.max_length), (2, 60))

    async def test_modal_results_replace_the_menu(self):
        v = PK.GuidedPicker(self.cog, 1)
        modal = PK.SearchModal(v)
        modal.query._value = "KD3"
        i = interaction()
        await modal.on_submit(i)
        self.assertEqual(v.screen, "stations")
        self.assertEqual([s.name[:3] for s in v.stations][:1], ["KD3"])
        self.assertIn("Results for “KD3”", v.title)
        i.response.edit_message.assert_awaited()
        valid(v)

    async def test_modal_with_no_matches_keeps_the_menu(self):
        v = PK.GuidedPicker(self.cog, 1)
        modal = PK.SearchModal(v)
        modal.query._value = "zzqqxx"
        i = interaction()
        await modal.on_submit(i)
        self.assertEqual(v.screen, "states")
        self.assertIn("No stations matched", i.response.send_message.await_args.args[0])
        self.assertTrue(i.response.send_message.await_args.kwargs["ephemeral"])


class Playing(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog, self.bot = make_cog(world_records())

    async def picked(self, v, uuid, user_id=1):
        i = interaction(uid=user_id)
        i.data = {"values": [uuid]}
        await v._station_picked(i)
        return i

    async def test_success_tunes_remembers_the_area_and_announces(self):
        v = PK.GuidedPicker(self.cog, 1)
        v.show_stations("IL", "Chicago")
        i = await self.picked(v, v.stations[2].uuid)
        self.bot.public.tune.assert_awaited_once()
        self.assertEqual(self.bot.public.tune.await_args.args[1].uuid, v.stations[2].uuid)
        self.bot.public.remember_area.assert_awaited_once_with(1, "IL", "Chicago")
        self.assertEqual(i.channel.send.await_args.kwargs["embed"].author.name, "Tuned by Pat")
        self.assertEqual(v.saved_area, ("IL", "Chicago"))
        self.assertEqual(v.screen, "stations", "the menu stays open so they can try another")

    async def test_listener_is_told_the_volume_starts_low_and_how_to_raise_it(self):
        self.bot.public.players = {5: types.SimpleNamespace(volume=0.4)}
        v = PK.GuidedPicker(self.cog, 1)
        v.show_stations("IL", "Chicago")
        i = await self.picked(v, v.stations[0].uuid)
        text = i.followup.send.await_args.args[0]
        self.assertIn("Volume starts low (40%)", text)
        self.assertIn("User Volume", text)
        self.assertIn("🔊 button", text)
        self.bot.public.players = {5: types.SimpleNamespace(volume=0.9)}
        i = await self.picked(v, v.stations[1].uuid)
        self.assertNotIn("starts low", i.followup.send.await_args.args[0], "no tip when the volume is already high")

    async def test_whole_state_is_remembered_without_a_city(self):
        v = PK.GuidedPicker(self.cog, 1)
        v.show_stations("TX", None)
        await self.picked(v, v.stations[0].uuid)
        self.bot.public.remember_area.assert_awaited_once_with(1, "TX", None)

    async def test_search_and_online_lists_do_not_change_the_remembered_area(self):
        v = PK.GuidedPicker(self.cog, 1)
        v.show_results(self.cog.bot.directory.search("KD"), "Results")
        await self.picked(v, v.stations[0].uuid)
        v.show_stations(D.ONLINE, None)
        await self.picked(v, v.stations[0].uuid)
        self.bot.public.remember_area.assert_not_awaited()

    async def test_failure_is_private_and_nothing_is_remembered(self):
        self.bot.public.tune = AsyncMock(return_value=(False, "Join a voice channel first, then pick a station."))
        v = PK.GuidedPicker(self.cog, 1)
        v.show_stations("IL", "Chicago")
        i = await self.picked(v, v.stations[0].uuid)
        i.channel.send.assert_not_awaited()
        self.assertIn("Join a voice channel", i.followup.send.await_args.args[0])
        self.assertTrue(i.followup.send.await_args.kwargs["ephemeral"])
        self.bot.public.remember_area.assert_not_awaited()
        i = await self.picked(v, "gone")
        self.assertIn("no longer in the list", i.followup.send.await_args.args[0])

    async def test_announcement_falls_back_when_the_bot_cannot_post(self):
        v = PK.GuidedPicker(self.cog, 1)
        v.show_stations("IL", "Chicago")
        i = interaction(); i.data = {"values": [v.stations[0].uuid]}
        i.channel.send = AsyncMock(side_effect=discord.HTTPException(types.SimpleNamespace(status=403, reason="x"), "x"))
        await v._station_picked(i)
        self.assertIn("embed", i.followup.send.await_args.kwargs)

    async def test_only_the_owner_can_use_the_menu(self):
        v = PK.GuidedPicker(self.cog, 1)
        i = interaction(uid=99)
        self.assertFalse(await v.interaction_check(i))
        self.assertTrue(i.response.send_message.await_args.kwargs["ephemeral"])
        self.assertTrue(await v.interaction_check(interaction(uid=1)))

    async def test_station_names_cannot_ping_people(self):
        cog, _ = make_cog([dict(rec(1), name="@everyone free <@123> **bold**")])
        v = PK.GuidedPicker(cog, 1)
        v.show_stations("IL", None)
        self.assertNotIn("@everyone", v.embed().description.replace("@​everyone", ""))

    async def test_close_ends_the_menu(self):
        v = PK.GuidedPicker(self.cog, 1)
        i = interaction()
        await v._close(i)
        self.assertEqual(i.response.edit_message.await_args.kwargs["view"], None)
        self.assertTrue(v.is_finished())


class Entry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog, self.bot = make_cog(world_records())

    async def test_browse_without_arguments_opens_the_guided_menu(self):
        i = interaction()
        await self.cog.browse.callback(self.cog, i)
        kw = i.response.send_message.await_args.kwargs
        self.assertTrue(kw["ephemeral"])
        self.assertEqual(kw["view"].screen, "states")

    async def test_browse_can_skip_ahead(self):
        i = interaction()
        await self.cog.browse.callback(self.cog, i, state="IL")
        self.assertEqual(i.response.send_message.await_args.kwargs["view"].screen, "areas")
        i = interaction()
        await self.cog.browse.callback(self.cog, i, state="IL", city="Chicago")
        v = i.response.send_message.await_args.kwargs["view"]
        self.assertEqual((v.screen, len(v.stations)), ("stations", 60))
        i = interaction()
        await self.cog.browse.callback(self.cog, i, state="IL", city="Chicago", genre=app_commands_choice("Jazz"))
        v = i.response.send_message.await_args.kwargs["view"]
        self.assertEqual((v.genre, len(v.stations)), ("Jazz", 20))

    async def test_browse_with_only_a_city_asks_for_the_state(self):
        i = interaction()
        await self.cog.browse.callback(self.cog, i, city="Chicago")
        self.assertIn("Pick a state", i.response.send_message.await_args.args[0])

    async def test_the_menu_uses_the_remembered_area(self):
        self.bot.public.area_for = MagicMock(return_value=("TX", "Dallas"))
        i = interaction(uid=7)
        await self.cog.open_picker(i)
        v = i.response.send_message.await_args.kwargs["view"]
        self.assertEqual(v.saved_area, ("TX", "Dallas"))
        self.bot.public.area_for.assert_called_with(7)

    async def test_forget_command(self):
        i = interaction()
        await self.cog.forget.callback(self.cog, i)
        self.bot.public.forget_area.assert_awaited_once_with(1)
        self.assertIn("forgotten", i.response.send_message.await_args.args[0])
        self.bot.public.forget_area = AsyncMock(return_value=False)
        i = interaction()
        await self.cog.forget.callback(self.cog, i)
        self.assertIn("hadn't saved", i.response.send_message.await_args.args[0])

    async def test_home_server_is_told_to_use_another_server(self):
        cog, _ = make_cog(world_records(), home="5")
        i = interaction(guild_id=5)
        await cog.open_picker(i)
        self.assertIn("rotation", i.response.send_message.await_args.args[0])


class Panel(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog, self.bot = make_cog(world_records())

    async def test_buttons_are_permanent_and_valid(self):
        v = PK.PanelView(self.cog)
        self.assertIsNone(v.timeout)
        ids = [c.custom_id for c in v.children]
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(0 < len(x) <= 100 for x in ids))
        valid(v)
        self.assertTrue(v.is_persistent())

    async def test_cog_registers_the_panel_at_startup(self):
        await self.cog.cog_load()
        added = [c.args[0] for c in self.bot.add_view.call_args_list]
        self.assertTrue(any(isinstance(a, PK.PanelView) for a in added))
        self.assertTrue(any(isinstance(a, PK.RemoteView) for a in added), "the remote under Now Playing survives restarts")

    async def test_buttons_open_the_picker_show_now_playing_and_stop(self):
        v = PK.PanelView(self.cog)
        i = interaction()
        await v.find.callback(i)
        self.assertEqual(i.response.send_message.await_args.kwargs["view"].screen, "states")
        i = interaction()
        await v.now.callback(i)
        self.assertIn("Nothing is playing", i.response.send_message.await_args.args[0])
        player = types.SimpleNamespace(station=D.Station("u", "WXYZ", "http://x", "IL", "Chicago", "Chicago"))
        self.bot.public.players = {5: player}
        self.bot.public.may_control = MagicMock(return_value=True)
        self.bot.public.stop = AsyncMock()
        i = interaction(); i.guild = MagicMock()
        await v.stop.callback(i)
        self.bot.public.stop.assert_awaited()
        self.bot.public.may_control = MagicMock(return_value=False)
        self.bot.public.stop = AsyncMock()
        await v.stop.callback(interaction())
        self.bot.public.stop.assert_not_awaited()

    async def test_panel_command_is_for_managers_only(self):
        i = interaction()
        i.user.guild_permissions = types.SimpleNamespace(manage_guild=False)
        await self.cog.panel.callback(self.cog, i)
        self.assertIn("Manage Server", i.response.send_message.await_args.args[0])
        self.assertNotIn("view", i.response.send_message.await_args.kwargs)
        i = interaction()
        i.user.guild_permissions = types.SimpleNamespace(manage_guild=True)
        await self.cog.panel.callback(self.cog, i)
        kw = i.response.send_message.await_args.kwargs
        self.assertIsInstance(kw["view"], PK.PanelView)
        self.assertNotIn("ephemeral", kw, "the panel is posted publicly")
        self.assertEqual(self.cog.panel.default_permissions.manage_guild, True)


def app_commands_choice(value):
    from discord import app_commands
    return app_commands.Choice(name=value, value=value)


if __name__ == "__main__":
    unittest.main()


class Card(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog, self.bot = make_cog(world_records())
        self.station = self.bot.directory.stations[0]

    def test_card_renders_a_png_for_odd_names(self):
        import io
        from PIL import Image
        import visuals
        for name, place, genres in [("WXRT 93.1 FM Chicago", "Chicago, IL", ["Rock"]), ("", "", []),
                                    ("Ünïcödé Radio 🎸 " * 6, "Somewhere " * 12, ["A", "B", "C", "D"])]:
            png = visuals.render_station_card(name, place, genres, "MP3 · 128 kbps")
            self.assertEqual(Image.open(io.BytesIO(png)).size, (visuals.CARD_W, visuals.CARD_H))

    def test_each_station_gets_its_own_stable_colour_and_dial_position(self):
        import visuals
        self.assertEqual(visuals.station_accent("WXRT"), visuals.station_accent("wxrt"))
        self.assertNotEqual(visuals.station_accent("WXRT"), visuals.station_accent("KROQ"))
        self.assertEqual(visuals.find_frequency("WXRT 93.1 FM"), "93.1")
        self.assertIsNone(visuals.find_frequency("Smooth Jazz Online"))

    async def test_now_playing_attaches_the_card_and_keeps_the_embed_short(self):
        embed, card = await PK.now_playing(self.station, "Now playing", by="Pat")
        self.assertEqual(card.filename, "station.png")
        self.assertEqual(embed.image.url, "attachment://station.png")
        self.assertEqual(embed.author.name, "Tuned by Pat")
        self.assertNotIn("Location", [f.name for f in embed.fields], "the card already shows the place")

    async def test_now_playing_falls_back_to_the_text_embed_if_drawing_fails(self):
        from unittest.mock import patch
        PK._cards.clear()
        with patch("visuals.render_station_card", side_effect=ValueError("boom")):
            embed, card = await PK.now_playing(self.station, "Now playing")
        self.assertIsNone(card)
        self.assertIn("Location", [f.name for f in embed.fields])

    def test_every_genre_has_an_emoji_and_menus_show_them(self):
        self.assertEqual(set(PK.GENRE_EMOJI), set(D.GENRES))
        v = PK.GuidedPicker(self.cog, 1)
        v.show_stations("IL", "Chicago")
        valid(v)
        station_select = [c for c in v.children if isinstance(c, discord.ui.Select)][-1]
        self.assertTrue(all(o.emoji for o in station_select.options))
        self.assertIn(PK.genre_emoji(v.stations[0]), v.embed().description)

    def test_the_first_screen_shows_the_brand_icon(self):
        v = PK.GuidedPicker(self.cog, 1)
        self.assertEqual(v.embed().thumbnail.url, "attachment://icon.png")
        self.assertIsNotNone(PK.brand_file())


class Remote(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog, self.bot = make_cog(world_records())
        self.bot.public.may_control = MagicMock(return_value=True)
        self.bot.public.set_volume = MagicMock()
        self.bot.public.players = {5: types.SimpleNamespace(volume=0.4, station=self.bot.directory.stations[0])}

    def test_remote_is_permanent_and_valid(self):
        v = PK.RemoteView(self.cog)
        self.assertIsNone(v.timeout)
        self.assertTrue(v.is_persistent())
        valid(v)
        self.assertEqual(len({c.custom_id for c in v.children}), 4)

    async def test_volume_buttons_step_by_ten_and_clamp(self):
        i = interaction()
        await self.cog.nudge_volume(i, +0.1)
        self.bot.public.set_volume.assert_called_with(5, 0.5)
        self.bot.public.players[5].volume = 0.97
        await self.cog.nudge_volume(interaction(), +0.1)
        self.bot.public.set_volume.assert_called_with(5, 1.0)
        self.bot.public.players[5].volume = 0.07
        await self.cog.nudge_volume(interaction(), -0.1)
        self.bot.public.set_volume.assert_called_with(5, 0.05)

    async def test_volume_buttons_respect_who_may_control(self):
        self.bot.public.may_control.return_value = False
        i = interaction()
        await self.cog.nudge_volume(i, +0.1)
        self.bot.public.set_volume.assert_not_called()
        self.assertIn("Someone else", i.response.send_message.await_args.args[0])

    async def test_volume_button_with_nothing_playing(self):
        self.bot.public.players = {}
        i = interaction()
        await self.cog.nudge_volume(i, +0.1)
        self.assertIn("Nothing is playing", i.response.send_message.await_args.args[0])

    async def test_now_command_posts_the_card_with_the_remote(self):
        i = interaction()
        await self.cog.show_now(i)
        kw = i.followup.send.await_args.kwargs
        self.assertIsInstance(kw["view"], PK.RemoteView)
        self.assertEqual(kw["file"].filename, "station.png")
