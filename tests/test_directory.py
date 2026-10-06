import json
import os
import unittest

import directory as D

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "us_stations_sample.json")


def rec(**kw):
    base = {"stationuuid": "u1", "name": "Test FM", "url": "http://x.example/s", "url_resolved": "http://x.example/s",
            "tags": "rock", "state": "", "votes": 1, "clickcount": 0, "codec": "MP3", "bitrate": 128, "lastcheckok": 1,
            "geo_lat": None, "geo_long": None}
    base.update(kw)
    return base


class StateAndCity(unittest.TestCase):
    def test_normalize_state(self):
        cases = {"California": "CA", "CA": "CA", "ca": "CA", "New York NY": "NY", "Chicago, IL  ": "IL",
                 "Boston MA": "MA", "Washington DC": "DC", "District of Columbia": "DC", "West Virginia": "WV",
                 "": None, None: None, "Narnia": None}
        for given, want in cases.items():
            self.assertEqual(D.normalize_state(given), want, given)

    def test_extract_city(self):
        self.assertEqual(D.extract_city("Chicago, IL  ", "x"), ("Chicago", "IL"))
        self.assertEqual(D.extract_city("", "WLS 94.7-FM Chicago, IL"), ("Chicago", "IL"))
        self.assertEqual(D.extract_city("Boston MA", "x"), ("Boston", "MA"))
        self.assertEqual(D.extract_city("", "WCHI 95.5 FM - Chicago, IL"), ("Chicago", "IL"))
        self.assertEqual(D.extract_city("", "Just a name"), (None, None))
        self.assertEqual(D.extract_city("", "Foo, ZZ"), (None, None))

    def test_nearest_market(self):
        self.assertEqual(D.nearest_market(41.9, -87.7, 40), ("Chicago", "IL"))
        self.assertIsNone(D.nearest_market(0, 0, 100))
        self.assertAlmostEqual(D.haversine_miles(41.88, -87.63, 41.88, -87.63), 0, places=3)
        self.assertTrue(700 < D.haversine_miles(41.88, -87.63, 40.71, -74.01) < 800)

    def test_all_states_have_a_name_and_markets_are_valid(self):
        self.assertEqual(len(D.STATES), 51)
        for city, st, lat, lon in D.MARKETS:
            self.assertIn(st, D.STATES, city)
            self.assertTrue(-180 < lon < -60 and 15 < lat < 72, city)
        covered = {st for _, st, _, _ in D.MARKETS}
        self.assertEqual(covered, set(D.STATES), "every state (and DC) should have at least one major market")


class BuildStation(unittest.TestCase):
    def test_rejects_unusable_records(self):
        self.assertIsNone(D.build_station(rec(url="ftp://x/y", url_resolved="ftp://x/y")))
        self.assertIsNone(D.build_station(rec(url="", url_resolved="")))
        self.assertIsNone(D.build_station(rec(name="")))
        self.assertIsNone(D.build_station(rec(lastcheckok=0)))
        self.assertIsNone(D.build_station(rec(bitrate=16)))
        self.assertIsNotNone(D.build_station(rec(bitrate=0)), "unknown bitrate is allowed")

    def test_places_by_state_city_and_market(self):
        s = D.build_station(rec(name="WLS 94.7-FM Chicago, IL", state="Chicago, IL  "))
        self.assertEqual((s.state, s.market, s.place), ("IL", "Chicago", "Chicago, IL"))
        s = D.build_station(rec(name="Some Station", state="Texas"))
        self.assertEqual((s.state, s.market, s.place), ("TX", None, "Texas"))
        s = D.build_station(rec(name="Q101 Chicago Rock", state="Illinois"))
        self.assertEqual(s.market, "Chicago", "market found by name when there is no city field")

    def test_rescues_stations_with_only_coordinates(self):
        s = D.build_station(rec(name="Mystery Radio", geo_lat=41.9, geo_long=-87.7))
        self.assertEqual((s.state, s.market), ("IL", "Chicago"))
        far = D.build_station(rec(name="Remote", geo_lat=47.5, geo_long=-111.0))   # Montana, nowhere near a listed market
        self.assertIsNone(far.market)

    def test_conflicting_geo_does_not_override_a_stated_state(self):
        s = D.build_station(rec(name="Nashville Sound", state="Tennessee", geo_lat=41.9, geo_long=-87.7))
        self.assertEqual(s.state, "TN")
        self.assertNotEqual(s.market, "Chicago")

    def test_genres_and_local_detection(self):
        s = D.build_station(rec(name="WXYZ 95.5", tags="classic rock,80s"))
        self.assertIn("Classic Rock", s.genres)
        self.assertIn("Oldies", s.genres)
        self.assertTrue(s.local)
        self.assertFalse(D.build_station(rec(name="Classic Vinyl HD", tags="oldies")).local)
        self.assertTrue(D.looks_local("1010 WINS"))
        self.assertFalse(D.looks_local("My70sRadio"))
        # 'rock' must not match inside another word ('Brockton')
        self.assertNotIn("Rock", D.build_station(rec(name="Brockton Talk", tags="")).genres)


class Directory(unittest.TestCase):
    def test_variants_collapse_and_best_is_kept(self):
        d = D.Directory()
        n = d.load_records([
            rec(stationuuid="a", name="KERA 90.1 Dallas, TX (MP3)", url="http://a", url_resolved="http://a", state="Texas", votes=5, bitrate=64),
            rec(stationuuid="b", name="KERA 90.1 Dallas, TX (AAC)", url="http://b", url_resolved="http://b", state="Texas", votes=50, bitrate=128),
            rec(stationuuid="c", name="Other Station", url="http://c", url_resolved="http://c", state="Texas"),
        ])
        self.assertEqual(n, 2)
        self.assertEqual(d.by_uuid["b"].name, "KERA 90.1 Dallas, TX (AAC)")
        self.assertNotIn("a", d.by_uuid)

    def test_local_stations_outrank_online_only_ones(self):
        d = D.Directory()
        d.load_records([
            rec(stationuuid="a", name="Online Oldies Mix", url="http://a", url_resolved="http://a", state="Illinois", votes=9000, geo_lat=41.9, geo_long=-87.7),
            rec(stationuuid="b", name="WXYZ 101.1 Chicago, IL", url="http://b", url_resolved="http://b", state="Chicago, IL", votes=5),
        ])
        self.assertEqual([s.uuid for s in d.browse("IL", "Chicago")], ["b", "a"])

    def test_browse_filters_and_cities_order(self):
        d = D.Directory()
        d.load_records([
            rec(stationuuid="1", name="WAAA 90.1 Chicago, IL", url="http://1", url_resolved="http://1", state="Chicago, IL", tags="jazz"),
            rec(stationuuid="2", name="WBBB 91.1 Rockford, IL", url="http://2", url_resolved="http://2", state="Rockford, IL"),
            rec(stationuuid="3", name="WCCC 92.1 Rockford, IL", url="http://3", url_resolved="http://3", state="Rockford, IL"),
            rec(stationuuid="4", name="WDDD 93.1 Tiny, IL", url="http://4", url_resolved="http://4", state="Tiny, IL"),
            rec(stationuuid="5", name="KEEE 94.1 Dallas, TX", url="http://5", url_resolved="http://5", state="Dallas, TX"),
        ])
        self.assertEqual({s.uuid for s in d.browse("IL")}, {"1", "2", "3", "4"})
        self.assertEqual([s.uuid for s in d.browse("IL", genre="Jazz")], ["1"])
        cities = d.cities("IL")
        self.assertEqual(cities[0][0], "Chicago", "major markets first")
        self.assertIn(("Rockford", 2), cities)
        self.assertNotIn("Tiny", [c for c, _ in cities], "one-station towns are hidden")
        self.assertEqual([c for _, c, _ in [(a, b, n) for a, b, n in d.states()]], ["Illinois", "Texas"])

    def test_search(self):
        d = D.Directory()
        d.load_records([
            rec(stationuuid="1", name="WLS 94.7-FM Chicago, IL", url="http://1", url_resolved="http://1", state="Chicago, IL"),
            rec(stationuuid="2", name="The Eagle 97.1", url="http://2", url_resolved="http://2", state="Texas", tags="classic rock"),
        ])
        self.assertEqual([s.uuid for s in d.search("wls")], ["1"])
        self.assertEqual([s.uuid for s in d.search("chicago")], ["1"])
        self.assertEqual([s.uuid for s in d.search("classic rock", state="TX")], ["2"])
        self.assertEqual(d.search("classic rock", state="IL"), [])
        self.assertEqual(d.search("   "), [])

    def test_real_data_sample(self):
        with open(FIXTURE) as f:
            records = json.load(f)
        d = D.Directory()
        n = d.load_records(records)
        self.assertGreater(n, 300)
        self.assertTrue(all(s.url.lower().startswith(("http://", "https://")) for s in d.stations))
        self.assertTrue(all(s.state in D.STATES for s in d.stations if s.state))
        self.assertGreater(sum(1 for s in d.stations if s.state), n * 0.5, "most records should be placed in a state")
        self.assertEqual(len({(D.canonical_name(s.name), s.state or "") for s in d.stations}), n, "no format duplicates")


if __name__ == "__main__":
    unittest.main()
