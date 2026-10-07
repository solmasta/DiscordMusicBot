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
        no_key = [g for g, _ in L.Library(client_id="").genres(L.FREE)]
        self.assertIn("Rock", no_key, "ccMixter needs no key")
        self.assertNotIn("Metal", no_key, "ccMixter has no metal")
        with_key = [g for g, _ in L.Library(client_id="k").genres(L.FREE)]
        self.assertIn("Metal", with_key, "Jamendo adds the genres ccMixter lacks")
        self.assertNotIn("80s", with_key, "neither catalog has decade tags")
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

    async def test_unmapped_genre_makes_no_request(self):
        session = FakeSession(SAMPLE)
        self.assertEqual(await L.Library(session, client_id="KEY").free("80s"), [])
        self.assertEqual(session.calls, [])


CCMIXTER = [{
    "upload_id": 32423, "upload_name": "Spinnin'", "user_name": "AlexBeroza", "user_real_name": "Alex",
    "license_name": "Attribution (3.0)", "upload_extra": {"nsfw": False, "usertags": "rock"},
    "files": [{"file_nicname": "zip", "download_url": "https://ccmixter.org/content/a.zip"},
              {"file_nicname": "mp3", "download_url": "https://ccmixter.org/content/AlexBeroza/AlexBeroza_-_Spinnin_.mp3",
               "file_format_info": {"ps": "3:32", "sr": "44k"}}],
}, {
    "upload_id": 2, "upload_name": "Adult", "user_name": "x", "upload_extra": {"nsfw": True},
    "files": [{"file_nicname": "mp3", "download_url": "https://ccmixter.org/content/x.mp3"}],
}, {
    "upload_id": 3, "upload_name": "Insecure", "user_name": "x", "upload_extra": {},
    "files": [{"file_nicname": "mp3", "download_url": "http://ccmixter.org/content/x.mp3"}],
}, {
    "upload_id": 4, "upload_name": "No files", "user_name": "x", "upload_extra": {}, "files": [],
}, {
    "upload_id": 5, "upload_name": "Plain", "user_name": "solo", "upload_extra": {},
    "files": [{"file_nicname": "mp3", "download_url": "https://ccmixter.org/content/solo/p.mp3", "file_format_info": {}}],
}]


class CcMixter(unittest.IsolatedAsyncioTestCase):
    def test_parse_picks_the_mp3_skips_unsuitable_rows_and_credits_the_licence(self):
        songs = L.parse_ccmixter(CCMIXTER, "Rock")
        self.assertEqual([s.id for s in songs], ["cc32423", "cc5"])
        first = songs[0]
        self.assertEqual((first.title, first.artist, first.duration, first.source), ("Spinnin'", "Alex", 212, L.FREE))
        self.assertTrue(first.url.endswith(".mp3") and first.url.startswith("https://"))
        self.assertEqual(first.credit, "Attribution (3.0) · ccMixter")
        self.assertEqual(songs[1].artist, "solo")
        self.assertEqual(songs[1].duration, 0)

    def test_real_world_oddities_do_not_hide_the_page(self):
        rows = [dict(CCMIXTER[0], upload_extra=""),                       # ccMixter sometimes sends a string here
                dict(CCMIXTER[0], upload_id=77, upload_extra="{}", files=[None, "x", CCMIXTER[0]["files"][1]]),
                {"upload_id": 78, "upload_name": "Odd", "files": [{"file_nicname": "mp3", "download_url": "https://ccmixter.org/a.mp3",
                                                                    "file_format_info": "nope"}]}]
        self.assertEqual([s.id for s in L.parse_ccmixter(rows, "Rock")], ["cc32423", "cc77", "cc78"])

    def test_parse_survives_garbage(self):
        for bad in ({}, None, "x", [None, 3, {"files": None}]):
            self.assertEqual(L.parse_ccmixter(bad, "Rock"), [])

    def test_seconds(self):
        self.assertEqual((L._seconds("3:32"), L._seconds("1:02:03"), L._seconds(""), L._seconds("x")), (212, 3723, 0, 0))

    async def test_without_a_key_free_music_comes_from_ccmixter(self):
        session = FakeSession(CCMIXTER)
        lib = L.Library(session, client_id="")
        songs = await lib.free("Hip Hop")
        self.assertEqual(session.calls[0]["tags"], "hip_hop")
        self.assertEqual((session.calls[0]["limit"], session.calls[0]["offset"]), (25, 0))
        self.assertEqual(len(songs), 2)
        await lib.free("Hip Hop")
        self.assertEqual(len(session.calls), 1, "cached")

    async def test_jamendo_is_tried_first_and_ccmixter_covers_for_it(self):
        session = FakeSession({"headers": {"code": 11, "error_message": "suspended"}, "results": []})
        lib = L.Library(session, client_id="KEY")
        original = session.get

        def get(url, params=None, timeout=None):
            session.payload = CCMIXTER if "ccmixter" in url else {"headers": {"code": 11}, "results": []}
            return original(url, params=params, timeout=timeout)
        session.get = get
        songs = await lib.free("Rock")
        self.assertEqual(len(songs), 2, "a bad key must not leave the library empty")
        self.assertEqual(len(session.calls), 2)

    async def test_genres_only_jamendo_has_do_not_fall_back(self):
        session = FakeSession([])
        lib = L.Library(session, client_id="")
        self.assertEqual(await lib.free("Metal"), [])
        self.assertEqual(session.calls, [])


class Referer(unittest.TestCase):
    def test_ccmixter_songs_carry_their_page_as_the_referrer(self):
        songs = L.parse_ccmixter([dict(CCMIXTER[0], file_page_url="https://ccmixter.org/files/AlexBeroza/32423")], "Rock")
        self.assertEqual(songs[0].referer, "https://ccmixter.org/files/AlexBeroza/32423")
        self.assertEqual(L.parse_ccmixter(CCMIXTER, "Rock")[0].referer, "https://ccmixter.org/")

    def test_ffmpeg_options_add_the_referrer_only_when_needed(self):
        cc = L.parse_ccmixter(CCMIXTER, "Rock")[0]
        self.assertIn("-referer https://ccmixter.org/", L.ffmpeg_before("-a -b", cc))
        jam = L.parse_jamendo(SAMPLE, "Rock")[0]
        self.assertEqual(L.ffmpeg_before("-a -b", jam), "-a -b")
        self.assertEqual(L.ffmpeg_before("-a", object()), "-a")

    def test_the_switch_turns_ccmixter_off(self):
        from unittest import mock
        with mock.patch.object(L, "CCMIXTER_ON", False):
            self.assertEqual([g for g, _ in L.Library(client_id="").genres(L.FREE)], [], "nothing without ccMixter or a key")
            self.assertIn("Metal", [g for g, _ in L.Library(client_id="k").genres(L.FREE)])
