"""The song library: browse songs by genre and play them.

Two sources:
  * "free"  - Creative Commons music, safe to play on any server. ccMixter needs no sign-up and is always
              on; Jamendo (a free JAMENDO_CLIENT_ID) is added when a key is set, and is tried first.
  * "hits"  - curated popular songs per genre, found on YouTube when played. Home server only: those
              songs are not licensed for a public bot to stream.
"""
import logging
import os
import shlex
import time
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("library")

JAMENDO_URL = "https://api.jamendo.com/v3.0/tracks/"
CCMIXTER_URL = "https://ccmixter.org/api/query"
PAGE = 25                  # one page fits a Discord select menu
CACHE_S = 1800
FREE, HITS = "free", "hits"

# (label, emoji, Jamendo tag or None when that catalog has nothing for it)
GENRES = [
    ("Rock", "🎸", "rock"), ("Classic Rock", "🎸", None), ("Alternative", "🎧", "indie"), ("Metal", "🤘", "metal"),
    ("Punk", "⚡", "punk"), ("Pop", "🎤", "pop"), ("Hip Hop", "🎤", "hiphop"), ("R&B / Soul", "💜", "soul"),
    ("Country", "🤠", "country"), ("Electronic", "🎛️", "electronic"), ("Jazz", "🎷", "jazz"), ("Blues", "🪕", "blues"),
    ("Latin", "💃", "latin"), ("Reggae", "🌴", "reggae"), ("Folk", "🪗", "folk"), ("Classical", "🎻", "classical"),
    ("Chill", "🌙", "chillout"), ("80s", "📼", None), ("90s", "💿", None),
]
GENRE_BY_LABEL = {g[0]: g for g in GENRES}
# ccMixter's tags for the same genres (it has none for metal, classic rock or the decades)
CCMIXTER_TAGS = {
    "Rock": "rock", "Alternative": "indie", "Punk": "punk", "Pop": "pop", "Hip Hop": "hip_hop", "R&B / Soul": "soul",
    "Country": "country", "Electronic": "electronic", "Jazz": "jazz", "Blues": "blues", "Latin": "latin",
    "Reggae": "reggae", "Folk": "folk", "Classical": "classical", "Chill": "chill",
}

# Curated popular songs per genre: (artist, title). Played on the home server only.
HITS_LIST: dict[str, list[tuple[str, str]]] = {
    "Rock": [("Foo Fighters", "Everlong"), ("Red Hot Chili Peppers", "Under the Bridge"), ("Green Day", "Basket Case"),
             ("The Killers", "Mr. Brightside"), ("Pearl Jam", "Alive"), ("Tom Petty", "Free Fallin'"),
             ("Queens of the Stone Age", "No One Knows"), ("Arctic Monkeys", "Do I Wanna Know?"), ("Weezer", "Buddy Holly"),
             ("Audioslave", "Like a Stone"), ("Nickelback", "How You Remind Me"), ("Linkin Park", "In the End")],
    "Classic Rock": [("Led Zeppelin", "Stairway to Heaven"), ("Queen", "Bohemian Rhapsody"), ("Eagles", "Hotel California"),
                     ("Pink Floyd", "Comfortably Numb"), ("AC/DC", "Back in Black"), ("Lynyrd Skynyrd", "Sweet Home Alabama"),
                     ("The Rolling Stones", "Paint It Black"), ("Fleetwood Mac", "Go Your Own Way"), ("Boston", "More Than a Feeling"),
                     ("Journey", "Don't Stop Believin'"), ("Creedence Clearwater Revival", "Fortunate Son"), ("The Who", "Baba O'Riley")],
    "Alternative": [("Nirvana", "Smells Like Teen Spirit"), ("Radiohead", "Creep"), ("Foster the People", "Pumped Up Kicks"),
                    ("Gotye", "Somebody That I Used to Know"), ("Twenty One Pilots", "Stressed Out"), ("Imagine Dragons", "Radioactive"),
                    ("Cage the Elephant", "Ain't No Rest for the Wicked"), ("The Strokes", "Last Nite"), ("Beck", "Loser"),
                    ("Evanescence", "Bring Me to Life"), ("Muse", "Uprising"), ("Portugal. The Man", "Feel It Still")],
    "Metal": [("Metallica", "Enter Sandman"), ("Black Sabbath", "Paranoid"), ("Iron Maiden", "The Trooper"),
              ("Slipknot", "Duality"), ("Megadeth", "Symphony of Destruction"), ("Pantera", "Walk"),
              ("System of a Down", "Chop Suey!"), ("Disturbed", "Down with the Sickness"), ("Judas Priest", "Breaking the Law"),
              ("Rage Against the Machine", "Killing in the Name"), ("Ozzy Osbourne", "Crazy Train"), ("Avenged Sevenfold", "Bat Country")],
    "Punk": [("Ramones", "Blitzkrieg Bop"), ("The Clash", "London Calling"), ("Blink-182", "All the Small Things"),
             ("Sum 41", "In Too Deep"), ("Misfits", "Where Eagles Dare"), ("Dead Kennedys", "Holiday in Cambodia"),
             ("Bad Religion", "21st Century (Digital Boy)"), ("The Offspring", "Self Esteem"), ("Rancid", "Time Bomb"),
             ("Green Day", "American Idiot"), ("Sex Pistols", "Anarchy in the U.K."), ("NOFX", "Linoleum")],
    "Pop": [("Michael Jackson", "Billie Jean"), ("Taylor Swift", "Shake It Off"), ("Dua Lipa", "Don't Start Now"),
            ("Ed Sheeran", "Shape of You"), ("Adele", "Rolling in the Deep"), ("The Weeknd", "Blinding Lights"),
            ("Bruno Mars", "Uptown Funk"), ("Katy Perry", "Firework"), ("Lady Gaga", "Poker Face"),
            ("Harry Styles", "As It Was"), ("Billie Eilish", "bad guy"), ("Justin Timberlake", "Can't Stop the Feeling!")],
    "Hip Hop": [("Eminem", "Lose Yourself"), ("Kendrick Lamar", "HUMBLE."), ("Notorious B.I.G.", "Juicy"),
                ("2Pac", "California Love"), ("Jay-Z", "Empire State of Mind"), ("Drake", "Hotline Bling"),
                ("Outkast", "Ms. Jackson"), ("Dr. Dre", "Still D.R.E."), ("Nas", "N.Y. State of Mind"),
                ("Kanye West", "Stronger"), ("Missy Elliott", "Work It"), ("Run-D.M.C.", "It's Tricky")],
    "R&B / Soul": [("Stevie Wonder", "Superstition"), ("Aretha Franklin", "Respect"), ("Marvin Gaye", "Let's Get It On"),
                   ("Al Green", "Let's Stay Together"), ("Beyoncé", "Crazy in Love"), ("Alicia Keys", "Fallin'"),
                   ("Sam Cooke", "A Change Is Gonna Come"), ("Otis Redding", "(Sittin' On) The Dock of the Bay"), ("Amy Winehouse", "Rehab"),
                   ("Bill Withers", "Ain't No Sunshine"), ("SZA", "Good Days"), ("Usher", "Yeah!")],
    "Country": [("Johnny Cash", "Ring of Fire"), ("Dolly Parton", "Jolene"), ("Luke Combs", "Beer Never Broke My Heart"),
                ("Chris Stapleton", "Tennessee Whiskey"), ("Garth Brooks", "Friends in Low Places"), ("Shania Twain", "Man! I Feel Like a Woman!"),
                ("Willie Nelson", "On the Road Again"), ("Kenny Chesney", "Summertime"), ("Morgan Wallen", "Last Night"),
                ("Zac Brown Band", "Chicken Fried"), ("Alan Jackson", "Chattahoochee"), ("Brooks & Dunn", "Boot Scootin' Boogie")],
    "Electronic": [("Daft Punk", "One More Time"), ("Calvin Harris", "Summer"), ("Avicii", "Levels"),
                   ("Deadmau5", "Strobe"), ("The Chemical Brothers", "Block Rockin' Beats"), ("Swedish House Mafia", "Don't You Worry Child"),
                   ("Skrillex", "Scary Monsters and Nice Sprites"), ("Zedd", "Clarity"), ("Kavinsky", "Nightcall"),
                   ("Justice", "D.A.N.C.E."), ("Fatboy Slim", "Right Here, Right Now"), ("Martin Garrix", "Animals")],
    "Jazz": [("Miles Davis", "So What"), ("John Coltrane", "Giant Steps"), ("Dave Brubeck", "Take Five"),
             ("Louis Armstrong", "What a Wonderful World"), ("Ella Fitzgerald", "Summertime"), ("Duke Ellington", "Take the 'A' Train"),
             ("Herbie Hancock", "Cantaloupe Island"), ("Nina Simone", "Feeling Good"), ("Billie Holiday", "Strange Fruit"),
             ("Charlie Parker", "Ornithology"), ("Thelonious Monk", "'Round Midnight"), ("Norah Jones", "Don't Know Why")],
    "Blues": [("B.B. King", "The Thrill Is Gone"), ("Muddy Waters", "Hoochie Coochie Man"), ("Stevie Ray Vaughan", "Pride and Joy"),
              ("Robert Johnson", "Cross Road Blues"), ("Howlin' Wolf", "Smokestack Lightning"), ("John Lee Hooker", "Boom Boom"),
              ("Eric Clapton", "Crossroads"), ("Buddy Guy", "Damn Right, I've Got the Blues"), ("Albert King", "Born Under a Bad Sign"),
              ("The Black Keys", "Lonely Boy"), ("Gary Clark Jr.", "Bright Lights"), ("Joe Bonamassa", "Sloe Gin")],
    "Latin": [("Luis Fonsi", "Despacito"), ("Shakira", "Hips Don't Lie"), ("Bad Bunny", "Tití Me Preguntó"),
              ("Ricky Martin", "Livin' la Vida Loca"), ("Santana", "Smooth"), ("J Balvin", "Mi Gente"),
              ("Enrique Iglesias", "Bailando"), ("Celia Cruz", "La Vida Es Un Carnaval"), ("Selena", "Bidi Bidi Bom Bom"),
              ("Daddy Yankee", "Gasolina"), ("Marc Anthony", "Vivir Mi Vida"), ("Juanes", "La Camisa Negra")],
    "Reggae": [("Bob Marley & The Wailers", "No Woman, No Cry"), ("Bob Marley & The Wailers", "Three Little Birds"), ("Peter Tosh", "Legalize It"),
               ("Jimmy Cliff", "Many Rivers to Cross"), ("UB40", "Red Red Wine"), ("Toots & The Maytals", "Pressure Drop"),
               ("Sublime", "What I Got"), ("Damian Marley", "Welcome to Jamrock"), ("Shaggy", "It Wasn't Me"),
               ("Steel Pulse", "Steppin' Out"), ("Burning Spear", "Marcus Garvey"), ("Third World", "Now That We Found Love")],
    "Folk": [("Bob Dylan", "Blowin' in the Wind"), ("Simon & Garfunkel", "The Sound of Silence"), ("Fleet Foxes", "White Winter Hymnal"),
             ("Mumford & Sons", "Little Lion Man"), ("The Lumineers", "Ho Hey"), ("Joni Mitchell", "Both Sides, Now"),
             ("Neil Young", "Heart of Gold"), ("Bon Iver", "Skinny Love"), ("Johnny Cash", "Hurt"),
             ("Woody Guthrie", "This Land Is Your Land"), ("Cat Stevens", "Wild World"), ("Iron & Wine", "Naked as We Came")],
    "Classical": [("Beethoven", "Symphony No. 5"), ("Mozart", "Eine kleine Nachtmusik"), ("Bach", "Toccata and Fugue in D minor"),
                  ("Vivaldi", "Spring (The Four Seasons)"), ("Debussy", "Clair de Lune"), ("Chopin", "Nocturne Op. 9 No. 2"),
                  ("Tchaikovsky", "Swan Lake"), ("Pachelbel", "Canon in D"), ("Satie", "Gymnopédie No. 1"),
                  ("Beethoven", "Moonlight Sonata"), ("Grieg", "In the Hall of the Mountain King"), ("Ravel", "Boléro")],
    "Chill": [("Bon Iver", "Holocene"), ("Khruangbin", "Time (You and I)"), ("Tycho", "A Walk"),
              ("Bonobo", "Kerala"), ("Air", "La Femme d'Argent"), ("Nujabes", "Aruarian Dance"),
              ("Massive Attack", "Teardrop"), ("Zero 7", "In the Waiting Line"), ("Röyksopp", "Eple"),
              ("Boards of Canada", "Roygbiv"), ("Moby", "Porcelain"), ("Sade", "By Your Side")],
    "80s": [("a-ha", "Take On Me"), ("Prince", "Purple Rain"), ("Madonna", "Like a Prayer"),
            ("Michael Jackson", "Thriller"), ("Duran Duran", "Hungry Like the Wolf"), ("Tears for Fears", "Everybody Wants to Rule the World"),
            ("The Police", "Every Breath You Take"), ("Whitney Houston", "I Wanna Dance with Somebody"), ("Bon Jovi", "Livin' on a Prayer"),
            ("Guns N' Roses", "Sweet Child o' Mine"), ("Journey", "Don't Stop Believin'"), ("Cyndi Lauper", "Girls Just Want to Have Fun")],
    "90s": [("Nirvana", "Come as You Are"), ("Oasis", "Wonderwall"), ("Backstreet Boys", "I Want It That Way"),
            ("Spice Girls", "Wannabe"), ("TLC", "No Scrubs"), ("Alanis Morissette", "You Oughta Know"),
            ("Smash Mouth", "All Star"), ("Radiohead", "Karma Police"), ("Hanson", "MMMBop"),
            ("Britney Spears", "...Baby One More Time"), ("Counting Crows", "Mr. Jones"), ("Green Day", "Longview")],
}

# ccMixter refuses downloads that don't look like they come from its own site (anti-hotlinking), so its
# songs are fetched with the song's own ccMixter page as the referrer. Set CCMIXTER=0 to turn the source
# off completely (it can't play without the referrer).
CCMIXTER_ON = os.getenv("CCMIXTER", "1") != "0"

# Fly sets this (a free key from developer.jamendo.com). Without it the free library is hidden.
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID", "")


@dataclass(frozen=True)
class Song:
    """One song. It also answers to the same names as a radio station (name, place, genres...), so
    the existing Now Playing card and panel can show it unchanged."""
    id: str
    title: str
    artist: str
    genre: str
    source: str            # FREE or HITS
    url: str = ""          # direct audio stream (free songs); empty for hits, which are looked up when played
    duration: int = 0
    art: str = ""
    credit: str = ""       # licence and where it came from, shown on the card (Creative Commons asks for credit)
    referer: str = ""      # page to present as the referrer when fetching the audio (ccMixter needs it)

    @property
    def query(self) -> str:
        return f"{self.artist} - {self.title}"

    # ---- station-shaped view
    @property
    def name(self) -> str:
        return self.title

    @property
    def place(self) -> str:
        return self.artist

    @property
    def genres(self) -> frozenset:
        return frozenset({self.genre})

    @property
    def uuid(self) -> str:
        return f"song:{self.source}:{self.id}"

    codec = ""
    bitrate = 0
    homepage = ""
    is_song = True

    @property
    def duration_str(self) -> str:
        m, s = divmod(int(self.duration or 0), 60)
        return f"{m}:{s:02d}" if self.duration else ""


def ffmpeg_before(base: str, song) -> str:
    """ffmpeg input options for a song: the shared ones, plus the referrer when the song needs one."""
    referer = getattr(song, "referer", "")
    return f"{base} -referer {shlex.quote(referer)}" if referer else base


def genre_emoji(label: str) -> str:
    return GENRE_BY_LABEL.get(label, ("", "🎵", None))[1]


class Library:
    def __init__(self, session: aiohttp.ClientSession | None = None, client_id: str | None = None):
        self.session = session
        self._own_session = False
        self.client_id = JAMENDO_CLIENT_ID if client_id is None else client_id
        self._cache: dict[tuple[str, str, int], tuple[float, list[Song]]] = {}

    @property
    def free_enabled(self) -> bool:
        return True            # ccMixter needs no key, so free music is always available

    def genres(self, source: str) -> list[tuple[str, str]]:
        """(label, emoji) of the genres a source has songs for."""
        out = []
        for label, emoji, tag in GENRES:
            if source == FREE and ((tag and self.client_id) or (CCMIXTER_ON and label in CCMIXTER_TAGS)):
                out.append((label, emoji))
            elif source == HITS and HITS_LIST.get(label):
                out.append((label, emoji))
        return out

    def hits(self, genre: str) -> list[Song]:
        return [Song(id=f"{genre}:{i}", title=title, artist=artist, genre=genre, source=HITS)
                for i, (artist, title) in enumerate(HITS_LIST.get(genre, []))]

    async def free(self, genre: str, page: int = 0) -> list[Song]:
        """One page of popular free songs for a genre: Jamendo when there's a key, else (or if it has
        nothing) ccMixter."""
        if self.session is None:
            # ccMixter repeats its whole JSON answer in a response header (tens of KB), which aiohttp's
            # default limits reject, so the library keeps its own session with roomier limits.
            self.session = aiohttp.ClientSession(max_line_size=2 ** 21, max_field_size=2 ** 21)
            self._own_session = True
        songs: list[Song] = []
        if self.client_id and GENRE_BY_LABEL.get(genre, (None, None, None))[2]:
            songs = await self._fetch("jamendo", genre, page, parse_jamendo)
        if not songs and CCMIXTER_ON and genre in CCMIXTER_TAGS:
            songs = await self._fetch("ccmixter", genre, page, parse_ccmixter)
        return songs

    async def close(self):
        if self._own_session and self.session is not None:
            await self.session.close()
            self.session = None

    async def _fetch(self, provider: str, genre: str, page: int, parser) -> list[Song]:
        """Cached for a while so browsing is quick and the services aren't hit again and again."""
        key = (provider, genre, page)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        if provider == "jamendo":
            url = JAMENDO_URL
            params = {"client_id": self.client_id, "format": "json", "limit": PAGE, "offset": page * PAGE,
                      "tags": GENRE_BY_LABEL[genre][2], "order": "popularity_month", "audioformat": "mp32",
                      "imagesize": 200, "audiodlallowed": "false"}
        else:
            url = CCMIXTER_URL
            params = {"f": "json", "tags": CCMIXTER_TAGS[genre], "sort": "rank", "limit": PAGE, "offset": page * PAGE,
                      "dataview": "links_by"}      # the default view is too big for some networks (502)
        try:
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
        except Exception as e:
            log.warning("%s lookup failed (%s)", provider, e)
            return hit[1] if hit else []
        songs = parser(data, genre)
        if songs or (isinstance(data, dict) and data.get("headers", {}).get("code") == 0) or data == []:
            self._cache[key] = (time.monotonic(), songs)
        return songs


def parse_jamendo(data: dict, genre: str) -> list[Song]:
    """Turn a Jamendo /tracks response into songs, skipping anything without a playable stream."""
    headers = data.get("headers") or {}
    if headers.get("code", 0) != 0:
        log.warning("Jamendo error: %s", headers.get("error_message") or headers)
        return []
    songs = []
    for row in data.get("results") or []:
        url = row.get("audio") or ""
        if not url.startswith("https://") or not row.get("name"):
            continue
        songs.append(Song(id=str(row.get("id")), title=str(row["name"]).strip(), artist=str(row.get("artist_name") or "Unknown artist").strip(),
                          genre=genre, source=FREE, url=url, duration=int(row.get("duration") or 0),
                          art=str(row.get("album_image") or row.get("image") or ""), credit="Creative Commons · Jamendo"))
    return songs


def _seconds(text: str) -> int:
    """'3:32' or '1:02:03' -> seconds; 0 if it isn't a time."""
    try:
        total = 0
        for part in str(text).split(":"):
            total = total * 60 + int(part)
        return total
    except ValueError:
        return 0


def parse_ccmixter(data, genre: str) -> list[Song]:
    """Turn a ccMixter query (a JSON list of uploads) into songs: the first mp3 of each, skipping
    anything flagged adult or without an https link."""
    if not isinstance(data, list):
        return []
    songs = []
    for row in data:
        try:
            song = _ccmixter_song(row, genre)
        except Exception as e:       # one odd row must not hide the rest of the page
            log.warning("Skipping an unreadable ccMixter row: %s", e)
            continue
        if song:
            songs.append(song)
    return songs


def _ccmixter_song(row, genre: str) -> Song | None:
    if not isinstance(row, dict):
        return None
    extra = row.get("upload_extra")
    if isinstance(extra, dict) and extra.get("nsfw"):
        return None
    files = [f for f in (row.get("files") or []) if isinstance(f, dict)]
    mp3 = next((f for f in files if f.get("file_nicname") == "mp3" and str(f.get("download_url", "")).startswith("https://")), None)
    if not mp3 or not row.get("upload_name"):
        return None
    info = mp3.get("file_format_info")
    licence = (row.get("license_name") or "Creative Commons").strip()
    return Song(id=f"cc{row.get('upload_id')}", title=str(row["upload_name"]).strip(),
                artist=str(row.get("user_real_name") or row.get("user_name") or "Unknown artist").strip(),
                genre=genre, source=FREE, url=mp3["download_url"],
                duration=_seconds((info if isinstance(info, dict) else {}).get("ps", "")),
                credit=f"{licence} · ccMixter",
                referer=str(row.get("file_page_url") or "https://ccmixter.org/"))
