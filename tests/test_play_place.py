import unittest
from unittest.mock import AsyncMock, MagicMock

import directory as D
from tests.test_publicradio import make_radio, station


def st(uuid, name, state, city, votes=1):
    return D.Station(uuid=uuid, name=name, url="http://x.example/s", state=state, city=city, market=city, votes=votes)


class FindPlace(unittest.TestCase):
    def setUp(self):
        self.d = D.Directory()
        self.d.stations = [st("1", "A", "IL", "Chicago"), st("2", "B", "TX", "Austin"), st("3", "C", "OH", "Columbus"),
                           st("4", "D", "IL", "Springfield"), st("5", "E", "MO", "Springfield")]

    def test_city_state_and_abbreviation(self):
        self.assertEqual(self.d.find_place("Chicago"), ("IL", "Chicago"))
        self.assertEqual(self.d.find_place("austin tx"), ("TX", "Austin"))
        self.assertEqual(self.d.find_place("  CHICAGO, il "), ("IL", "Chicago"))
        self.assertEqual(self.d.find_place("Ohio"), ("OH", None))
        self.assertEqual(self.d.find_place("oh"), ("OH", None))

    def test_same_name_needs_the_state(self):
        self.assertEqual(self.d.find_place("Springfield, MO"), ("MO", "Springfield"))
        self.assertEqual(self.d.find_place("Springfield IL"), ("IL", "Springfield"))

    def test_unknown_and_empty(self):
        self.assertIsNone(self.d.find_place("Narnia"))
        self.assertIsNone(self.d.find_place(""))


class TuneFirstWorking(unittest.IsolatedAsyncioTestCase):
    async def test_skips_dead_stations_and_stops_on_other_problems(self):
        radio, _ = make_radio([])
        a, b, c = station("A", "a"), station("B", "b"), station("C", "c")
        calls = []

        async def fake_tune(member, s, text_channel_id=None, dead=None):
            calls.append(s.name)
            if s.name in ("A", "B"):
                dead.append(s)
                return False, "dead"
            return True, "ok"
        radio.tune = fake_tune
        member = MagicMock(); member.id = 9
        ok, _, played = await radio.tune_first_working(member, [a, b, c])
        self.assertTrue(ok)
        self.assertEqual((calls, played.name), (["A", "B", "C"], "C"))

        calls.clear()

        async def blocked(member, s, text_channel_id=None, dead=None):
            calls.append(s.name)
            return False, "Join a voice channel first"
        radio.tune = blocked
        ok, msg, played = await radio.tune_first_working(member, [a, b, c])
        self.assertFalse(ok)
        self.assertEqual((calls, msg), (["A"], "Join a voice channel first"))

    async def test_all_dead(self):
        radio, _ = make_radio([])

        async def dead_all(member, s, text_channel_id=None, dead=None):
            dead.append(s)
            return False, "dead"
        radio.tune = dead_all
        member = MagicMock(); member.id = 9
        ok, msg, played = await radio.tune_first_working(member, [station(str(i), str(i)) for i in range(6)], tries=3)
        self.assertFalse(ok)
        self.assertIn("responding", msg)


if __name__ == "__main__":
    unittest.main()
