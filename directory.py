"""Radio station directory: browse US stations by state, city/market and genre.

Data comes from Radio Browser (radio-browser.info), a free community directory of the stations'
own public streams. Its location fields are messy ("CA", "Boston MA", "Chicago, IL  "), so records
are cleaned up here: states are normalised, cities are extracted, and stations without a usable
location are placed by coordinates into the nearest major market.
"""
import asyncio
import math
import re
import time
from dataclasses import dataclass, field

import aiohttp

API_HOSTS = ("https://de1.api.radio-browser.info", "https://de2.api.radio-browser.info", "https://nl1.api.radio-browser.info")
USER_AGENT = "CrueFM-Discord-Bot/1.0 (+station directory)"
REFRESH_SECONDS = 6 * 3600
ONLINE = "ONLINE"   # pseudo-state for stations with no location (internet-only stations)

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_NAME_TO_AB = {v.lower(): k for k, v in STATES.items()}
_NAME_TO_AB["washington dc"] = "DC"
_NAME_TO_AB["washington d.c."] = "DC"

# (city, state, latitude, longitude): the major markets people look for, with approximate centres.
MARKETS = [
    ("New York", "NY", 40.71, -74.01), ("Los Angeles", "CA", 34.05, -118.24), ("Chicago", "IL", 41.88, -87.63),
    ("Dallas", "TX", 32.78, -96.80), ("Houston", "TX", 29.76, -95.37), ("Washington", "DC", 38.90, -77.04),
    ("Philadelphia", "PA", 39.95, -75.17), ("Atlanta", "GA", 33.75, -84.39), ("Boston", "MA", 42.36, -71.06),
    ("Phoenix", "AZ", 33.45, -112.07), ("Miami", "FL", 25.76, -80.19), ("Seattle", "WA", 47.61, -122.33),
    ("Detroit", "MI", 42.33, -83.05), ("San Francisco", "CA", 37.77, -122.42), ("Minneapolis", "MN", 44.98, -93.27),
    ("San Diego", "CA", 32.72, -117.16), ("Tampa", "FL", 27.95, -82.46), ("Denver", "CO", 39.74, -104.99),
    ("Baltimore", "MD", 39.29, -76.61), ("St. Louis", "MO", 38.63, -90.20), ("Orlando", "FL", 28.54, -81.38),
    ("Charlotte", "NC", 35.23, -80.84), ("Portland", "OR", 45.52, -122.68), ("Sacramento", "CA", 38.58, -121.49),
    ("Pittsburgh", "PA", 40.44, -79.99), ("Austin", "TX", 30.27, -97.74), ("Las Vegas", "NV", 36.17, -115.14),
    ("Cleveland", "OH", 41.50, -81.69), ("Cincinnati", "OH", 39.10, -84.51), ("Kansas City", "MO", 39.10, -94.58),
    ("Nashville", "TN", 36.16, -86.78), ("Indianapolis", "IN", 39.77, -86.16), ("Columbus", "OH", 39.96, -83.00),
    ("Milwaukee", "WI", 43.04, -87.91), ("Salt Lake City", "UT", 40.76, -111.89), ("New Orleans", "LA", 29.95, -90.07),
    ("Memphis", "TN", 35.15, -90.05), ("Louisville", "KY", 38.25, -85.76), ("Oklahoma City", "OK", 35.47, -97.52),
    ("Richmond", "VA", 37.54, -77.44), ("Birmingham", "AL", 33.52, -86.80), ("Albuquerque", "NM", 35.08, -106.65),
    ("Honolulu", "HI", 21.31, -157.86), ("Anchorage", "AK", 61.22, -149.90), ("Boise", "ID", 43.62, -116.20),
    ("Omaha", "NE", 41.26, -95.93), ("Des Moines", "IA", 41.59, -93.62), ("Little Rock", "AR", 34.75, -92.29),
    ("Jackson", "MS", 32.30, -90.18), ("Charleston", "SC", 32.78, -79.93), ("Providence", "RI", 41.82, -71.41),
    ("Hartford", "CT", 41.76, -72.67), ("Burlington", "VT", 44.48, -73.21), ("Portland", "ME", 43.66, -70.26),
    ("Manchester", "NH", 42.99, -71.46), ("Wilmington", "DE", 39.74, -75.55), ("Billings", "MT", 45.78, -108.50),
    ("Fargo", "ND", 46.88, -96.79), ("Sioux Falls", "SD", 43.55, -96.73), ("Cheyenne", "WY", 41.14, -104.82),
    ("Wichita", "KS", 37.69, -97.34), ("Charleston", "WV", 38.35, -81.63), ("San Antonio", "TX", 29.42, -98.49),
    ("San Jose", "CA", 37.34, -121.89), ("Jacksonville", "FL", 30.33, -81.66), ("Buffalo", "NY", 42.89, -78.88),
    ("Raleigh", "NC", 35.78, -78.64), ("Virginia Beach", "VA", 36.85, -75.98), ("Tucson", "AZ", 32.22, -110.97),
    ("El Paso", "TX", 31.76, -106.49), ("Fresno", "CA", 36.74, -119.79), ("Rochester", "NY", 43.16, -77.61),
    ("Tulsa", "OK", 36.15, -95.99), ("Baton Rouge", "LA", 30.45, -91.19), ("Knoxville", "TN", 35.96, -83.92),
    ("Grand Rapids", "MI", 42.96, -85.67), ("Greenville", "SC", 34.85, -82.40), ("Albany", "NY", 42.65, -73.76),
    ("Spokane", "WA", 47.66, -117.43), ("Madison", "WI", 43.07, -89.40), ("Lexington", "KY", 38.04, -84.50),
    ("Colorado Springs", "CO", 38.83, -104.82), ("Syracuse", "NY", 43.05, -76.15), ("Dayton", "OH", 39.76, -84.19),
    ("Toledo", "OH", 41.65, -83.54), ("Akron", "OH", 41.08, -81.52), ("Newark", "NJ", 40.74, -74.17), ("Mobile", "AL", 30.69, -88.04),
    ("Huntsville", "AL", 34.73, -86.59), ("Savannah", "GA", 32.08, -81.09), ("Reno", "NV", 39.53, -119.81),
    ("Eugene", "OR", 44.05, -123.09), ("Lincoln", "NE", 40.81, -96.70), ("Springfield", "MO", 37.21, -93.29),
]
_MARKET_KEYS = {(c.lower(), s) for c, s, _, _ in MARKETS}

GENRES = {
    "Rock": ("rock", "classic rock", "hard rock", "alternative", "alt rock", "indie", "grunge"),
    "Classic Rock": ("classic rock",),
    "Alternative": ("alternative", "alt rock", "indie", "modern rock"),
    "Metal": ("metal",),
    "Pop / Top 40": ("pop", "top 40", "top40", "hits", "chr", "adult contemporary"),
    "Country": ("country",),
    "Hip Hop / R&B": ("hip hop", "hip-hop", "rap", "r&b", "rnb", "urban"),
    "Jazz": ("jazz", "smooth jazz"),
    "Classical": ("classical",),
    "News / Talk": ("news", "talk", "npr", "public radio"),
    "Sports": ("sports",),
    "Christian / Gospel": ("christian", "gospel", "worship"),
    "Latin": ("latin", "spanish", "tejano", "mexican", "regional mexican"),
    "Oldies": ("oldies", "60s", "70s", "80s", "90s", "classic hits"),
    "Electronic": ("electronic", "dance", "edm", "house", "techno"),
    "Blues / Folk": ("blues", "folk", "americana", "bluegrass"),
}


def normalize_state(value: str | None) -> str | None:
    """'California', 'CA', 'New York NY', 'Chicago, IL  ' -> two-letter code, or None."""
    text = (value or "").strip()
    if not text:
        return None
    low = text.lower()
    if low in _NAME_TO_AB:
        return _NAME_TO_AB[low]
    tokens = re.findall(r"[A-Za-z]+", text)
    for tok in reversed(tokens):
        if tok.upper() in STATES:
            return tok.upper()
    for name, ab in _NAME_TO_AB.items():
        if re.search(rf"\b{re.escape(name)}\b", low):
            return ab
    return None


def extract_city(state_field: str | None, name: str | None) -> tuple[str | None, str | None]:
    """Pull a 'City, ST' out of the state field or the station name. Returns (city, state)."""
    for text in (state_field or "", name or ""):
        m = re.search(r",\s*([A-Z]{2})\b", text)
        if not m or m.group(1) not in STATES:
            continue
        st = m.group(1)
        segments = [p.strip() for p in re.split(r"\s[-–|]\s|[-–|()\"“”/]", text[: m.start()]) if p.strip()]
        if not segments:
            continue
        tokens = segments[-1].split()
        while tokens and (any(ch.isdigit() for ch in tokens[0]) or (tokens[0].isupper() and len(tokens[0]) <= 5)):
            tokens.pop(0)
        if not tokens:
            continue
        tail = " ".join(tokens).lower()
        for city, mst, _, _ in MARKETS:
            if mst == st and tail.endswith(city.lower()):
                return city, st
        return " ".join(tokens[-2:]), st
    m = re.match(r"^\s*([A-Za-z.'\- ]+?)\s+([A-Z]{2})\s*$", state_field or "")
    if m and m.group(2) in STATES:
        return m.group(1).strip(), m.group(2)
    return None, None


def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def nearest_market(lat: float, lon: float, within_miles: float):
    best, best_d = None, within_miles
    for city, st, mlat, mlon in MARKETS:
        d = haversine_miles(lat, lon, mlat, mlon)
        if d <= best_d:
            best, best_d = (city, st), d
    return best


@dataclass(frozen=True)
class Station:
    uuid: str
    name: str
    url: str
    state: str | None
    city: str | None
    market: str | None
    tags: tuple = ()
    genres: frozenset = field(default_factory=frozenset)
    bitrate: int = 0
    codec: str = ""
    homepage: str = ""
    favicon: str = ""
    votes: int = 0
    clicks: int = 0
    local: bool = False   # looks like a local broadcaster (call letters or a dial frequency)

    @property
    def place(self) -> str:
        where = self.market or self.city
        if where and self.state:
            return f"{where}, {self.state}"
        return where or (STATES.get(self.state, "") if self.state else "")

    @property
    def rank(self) -> tuple:
        return (self.local, self.votes, self.clicks)


def _genres_for(tags: tuple, name: str) -> frozenset:
    hay = " ".join(tags).lower() + " " + name.lower()
    found = set()
    for label, words in GENRES.items():
        if any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", hay) for w in words):
            found.add(label)
    return frozenset(found)


_CALLSIGN = re.compile(r"(?<![A-Za-z])[WK][A-Z]{2,3}(?:-?(?:FM|AM|HD\d?))?(?![A-Za-z])")
_DIAL = re.compile(r"(?<!\d)(?:8[89]|9\d|10[0-8])\.\d(?!\d)|(?<!\d)(?:5[3-9]|1[0-6])\d0\s?(?:AM|am)\b")


def looks_local(name: str) -> bool:
    return bool(_CALLSIGN.search(name) or _DIAL.search(name))


_VARIANT = re.compile(r"\((?:[^)]*(?:aac|mp3|opus|ogg|hd\d?|kbps|stream)[^)]*)\)|\b(?:aac\+?|mp3|opus|ogg|hd\d?|\d{2,3}\s?kbps?)\b", re.I)


def canonical_name(name: str) -> str:
    """Name without codec/quality decorations, so '(AAC)' and '(MP3)' copies collapse into one."""
    return re.sub(r"[^a-z0-9]+", " ", _VARIANT.sub(" ", name.lower())).strip()


def build_station(rec: dict) -> Station | None:
    """Turn one Radio Browser record into a cleaned-up Station, or None if it isn't usable."""
    url = (rec.get("url_resolved") or rec.get("url") or "").strip()
    name = re.sub(r"\s+", " ", (rec.get("name") or "")).strip()
    if not name or not url.lower().startswith(("http://", "https://")):
        return None
    if rec.get("lastcheckok") in (0, False):
        return None
    bitrate = int(rec.get("bitrate") or 0)
    if 0 < bitrate < 32:
        return None
    tags = tuple(t.strip().lower() for t in (rec.get("tags") or "").split(",") if t.strip())[:12]

    state = normalize_state(rec.get("state"))
    city, city_state = extract_city(rec.get("state"), rec.get("name"))
    if not state:
        state = city_state
    market = None
    lat, lon = rec.get("geo_lat"), rec.get("geo_long")
    if city and state and (city.lower(), state) in _MARKET_KEYS:
        market = next(c for c, s, _, _ in MARKETS if c.lower() == city.lower() and s == state)
    if not market and lat is not None and lon is not None:
        near = nearest_market(float(lat), float(lon), 40)
        if near and (not state or near[1] == state):
            market, state = near[0], near[1]
    if not market and state:
        hay = f"{name} {' '.join(tags)}".lower()
        for c, s, _, _ in MARKETS:
            if s == state and re.search(rf"(?<![a-z]){re.escape(c.lower())}(?![a-z])", hay):
                market = c
                break
    if not state and lat is not None and lon is not None:
        near = nearest_market(float(lat), float(lon), 75)
        if near:
            market, state = near[0], near[1]
    return Station(
        uuid=rec.get("stationuuid") or url, name=name[:100], url=url, state=state, city=city, market=market,
        tags=tags, genres=_genres_for(tags, name), bitrate=bitrate, codec=(rec.get("codec") or "").upper(),
        homepage=rec.get("homepage") or "", favicon=rec.get("favicon") or "",
        votes=int(rec.get("votes") or 0), clicks=int(rec.get("clickcount") or 0), local=looks_local(name),
    )


class Directory:
    def __init__(self, session: aiohttp.ClientSession | None = None):
        self.session = session
        self.stations: list[Station] = []
        self.by_uuid: dict[str, Station] = {}
        self.loaded_at = 0.0
        self._lock = asyncio.Lock()

    # ---- loading
    def load_records(self, records: list[dict]) -> int:
        built = [st for st in (build_station(rec) for rec in records) if st]
        built.sort(key=lambda s: (s.rank, s.bitrate), reverse=True)
        seen, out = set(), []
        for st in built:
            key = (canonical_name(st.name), st.state or "")
            if key in seen:
                continue
            seen.add(key)
            out.append(st)
        out.sort(key=lambda s: s.rank, reverse=True)
        self.stations = out
        self.by_uuid = {s.uuid: s for s in out}
        self.loaded_at = time.monotonic()
        return len(out)

    async def refresh(self, force: bool = False) -> int:
        """Download the US station list (about 8 MB, compressed in transit). Keeps serving the old
        list if the download fails."""
        async with self._lock:
            if not force and self.stations and time.monotonic() - self.loaded_at < REFRESH_SECONDS:
                return len(self.stations)
            path = "/json/stations/search?countrycode=US&hidebroken=true&limit=100000&order=votes&reverse=true"
            last_error = None
            for host in API_HOSTS:
                try:
                    async with self.session.get(
                        host + path, headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=90)
                    ) as resp:
                        resp.raise_for_status()
                        records = await resp.json(content_type=None)
                    return self.load_records(records)
                except Exception as e:
                    last_error = e
            if self.stations:
                self.loaded_at = time.monotonic() - REFRESH_SECONDS + 600   # retry in 10 minutes
                return len(self.stations)
            raise RuntimeError(f"station directory unavailable: {last_error}")

    async def resolve_url(self, station: Station) -> str:
        """Ask Radio Browser for the current stream URL (this also counts a listen for the station,
        which the directory asks apps to do). Falls back to the stored URL."""
        try:
            async with self.session.get(
                f"{API_HOSTS[0]}/json/url/{station.uuid}",
                headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                data = await resp.json(content_type=None)
            url = (data.get("url") or "").strip()
            if url.lower().startswith(("http://", "https://")):
                return url
        except Exception:
            pass
        return station.url

    # ---- browsing
    def states(self) -> list[tuple[str, str, int]]:
        counts: dict[str, int] = {}
        for s in self.stations:
            if s.state:
                counts[s.state] = counts.get(s.state, 0) + 1
        return sorted(((ab, STATES[ab], n) for ab, n in counts.items()), key=lambda t: t[1])

    def cities(self, state: str) -> list[tuple[str, int]]:
        """Major markets first, then other towns with at least two stations."""
        counts: dict[str, int] = {}
        for s in self.stations:
            if s.state != state:
                continue
            place = s.market or s.city
            if place:
                counts[place] = counts.get(place, 0) + 1
        major = {c for c, st, _, _ in MARKETS if st == state}
        rows = [(c, n) for c, n in counts.items() if c in major or n >= 2]
        return sorted(rows, key=lambda t: (t[0] not in major, -t[1], t[0]))

    def browse(self, state: str | None = None, city: str | None = None, genre: str | None = None) -> list[Station]:
        out = []
        for s in self.stations:
            if state == ONLINE:
                if s.state:
                    continue
            elif state and s.state != state:
                continue
            if city and city.lower() not in ((s.market or "").lower(), (s.city or "").lower()):
                continue
            if genre and genre not in s.genres:
                continue
            out.append(s)
        return out

    def search(self, query: str, state: str | None = None, limit: int = 100) -> list[Station]:
        words = [w for w in re.findall(r"[a-z0-9.]+", query.lower()) if w]
        if not words:
            return []
        scored = []
        for s in self.stations:
            if state and s.state != state:
                continue
            hay = f"{s.name} {s.market or ''} {s.city or ''} {STATES.get(s.state, '')} {' '.join(s.tags)}".lower()
            if not all(w in hay for w in words):
                continue
            name = s.name.lower()
            bonus = 2 if all(w in name for w in words) else 0
            bonus += 1 if name.startswith(words[0]) else 0
            scored.append((bonus, s.rank, s))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [s for _, _, s in scored[:limit]]
