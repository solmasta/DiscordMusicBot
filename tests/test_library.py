import unittest
from unittest.mock import MagicMock

import library as L

SAMPLE = {
    "headers": {"status": "success", "code": 0, "error_message": "", "results_count": 3},
    "results": [
        {"id": "1532", "name": "Sunrise  ", "duration": 213, "artist_id": "9", "artist_name": "The Band",
         "album_image": "https://usercontent.jamendo.com/?type=album&id=1&width=200", "audio": "https://prod-1.storage.jamendo.com/?trackid=1532&format=mp32",
         "audiodownload": "https://prod-1.storage.jamendo.com/download/track/1532/mp32/"},
        {"id": "7", "name": "No stream", "duration": 100, "artist_name": "X", "audio": ""},
        {"id": "8", "name": "Insecure", "duration": 100, "artist_name": "X", "audio": "http://example.com/a.mp3"},
        {"id": "9", "name": "Bare", "artist_name": None, "audio": "https://prod-1.storage.jamendo.com/?trackid=9"},
    ],
}


class FakeSession:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        payload = self.payload

        class Resp:
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
            async def json(s, content_type=None):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        return Resp()


class Catalog(unittest.TestCase):
    def test_every_genre_has_curated_hits_and_nothing_is_duplicated(self):
        for label, emoji, tag in L.GENRES:
            songs = L.HITS_LIST.get(label)
            self.assertTrue(songs and len(songs) >= 10, f"{label} needs a real list")
            self.assertEqual(len(set(songs)), len(songs), f"{label} repeats a song")
            self.assertTrue(emoji)
        self.assertEqual(set(L.HITS_LIST), set(L.GENRE_BY_LABEL), "no list without a genre")

    def test_hits_are_songs_with_search_text(self):
        lib = L.Library(client_id="")
        songs = lib.hits("Rock")
        self.assertEqual(songs[0].query, "Foo Fighters - Everlong")
        self.assertEqual(songs[0].source, L.HITS)
        self.assertEqual(len({s.uuid for s in songs}), len(songs))

    def test_song_looks_like_a_station_to_the_cards(self):
        s = L.Song("1", "Sunrise", "The Band", "Rock", L.FREE, url="https://x/y.mp3")
        self.assertEqual((s.name, s.place, s.genres, s.codec, s.bitrate, s.homepage), ("Sunrise", "The Band", frozenset({"Rock"}), "", 0, ""))
        self.assertTrue(s.is_song)

    def test_genre_lists_depend_on_the_source_and_the_key(self):
        self.assertEqual(len(L.Library(client_id="").genres(L.FREE)), 0, "no key, no free library")
        free = [g for g, _ in L.Library(client_id="k").genres(L.FREE)]
        self.assertIn("Rock", free)
        self.assertNotIn("80s", free, "Jamendo has no decade tags")
        self.assertEqual(len(L.Library(client_id="").genres(L.HITS)), len(L.GENRES))
        self.assertLessEqual(len(L.GENRES), 25, "must fit one select menu")


class Jamendo(unittest.IsolatedAsyncioTestCase):
    def test_parse_skips_unplayable_rows_and_cleans_names(self):
        songs = L.parse_jamendo(SAMPLE, "Rock")
        self.assertEqual([s.id for s in songs], ["1532", "9"], "no stream and non-https rows are dropped")
        self.assertEqual((songs[0].title, songs[0].artist, songs[0].duration, songs[0].duration_str), ("Sunrise", "The Band", 213, "3:33"))
        self.assertEqual(songs[1].artist, "Unknown artist")

    def test_parse_handles_an_api_error(self):
        self.assertEqual(L.parse_jamendo({"headers": {"code": 5, "error_message": "bad key"}, "results": []}, "Rock"), [])
        self.assertEqual(L.parse_jamendo({}, "Rock"), [])

    async def test_free_songs_are_fetched_with_the_right_tag_and_cached(self):
        session = FakeSession(SAMPLE)
        lib = L.Library(session, client_id="KEY")
        first = await lib.free("Hip Hop")
        self.assertEqual(session.calls[0]["tags"], "hiphop")
        self.assertEqual((session.calls[0]["client_id"], session.calls[0]["limit"], session.calls[0]["offset"]), ("KEY", 25, 0))
        again = await lib.free("Hip Hop")
        self.assertEqual(len(session.calls), 1, "second browse comes from the cache")
        self.assertEqual(first, again)
        await lib.free("Hip Hop", page=1)
        self.assertEqual(session.calls[1]["offset"], 25)

    async def test_failures_return_nothing_or_the_last_good_page(self):
        session = FakeSession(RuntimeError("down"))
        lib = L.Library(session, client_id="KEY")
        self.assertEqual(await lib.free("Rock"), [])
        session.payload = SAMPLE
        good = await lib.free("Rock")
        lib._cache[("Rock", 0)] = (0.0, good)          # expired
        session.payload = RuntimeError("down again")
        self.assertEqual(await lib.free("Rock"), good, "stale beats nothing")

    async def test_no_key_or_unmapped_genre_makes_no_request(self):
        session = FakeSession(SAMPLE)
        self.assertEqual(await L.Library(session, client_id="").free("Rock"), [])
        self.assertEqual(await L.Library(session, client_id="KEY").free("80s"), [])
        self.assertEqual(session.calls, [])
