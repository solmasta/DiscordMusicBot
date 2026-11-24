"""Station lists shared by /station (bot.py) and /stream presets (cogs/radio.py).

/station choices come from STATIONS (Discord allows at most 25 choices).
/stream presets are STATIONS plus EXTRA_PRESETS. Stream URLs can change without notice.
"""

_STW = "https://playerservices.streamtheworld.com/api/livestream-redirect"

# key -> (label, url). The first entry is the default station.
STATIONS: dict[str, tuple[str, str]] = {
    "crue":   ("CRUE FM 🤘 Metal/Hard Rock",     "https://ice3.somafm.com/metal-128-mp3"),
    "u80s":   ("Underground 80s (SomaFM)",       "https://ice3.somafm.com/u80s-128-mp3"),
    "wkqx":   ("Q101 (WKQX 101.1)",              f"{_STW}/WKQXFM.mp3"),
    "wlup":   ("97.9 The Loop (WLUP)",           f"{_STW}/WLUPFM.mp3"),
    "wls":    ("94.7 WLS-FM",                    f"{_STW}/WLSFM.mp3"),
    "wksc":   ("103.5 KISS FM (WKSC)",           "https://stream.revma.ihrhls.com/zc849"),
    "wdrv":   ("97.1 The Drive (WDRV)",          f"{_STW}/WDRVFM.mp3"),
    "wgci":   ("107.5 WGCI",                     "https://stream.revma.ihrhls.com/zc841"),
    "wbez":   ("91.5 WBEZ Public Radio",         "https://stream.wbez.org/wbez128.mp3"),
    "vocalo": ("Vocalo",                         "http://stream.vocalo.org/vocalo128"),
    "wfmt":   ("98.7 WFMT Classical",            "https://wfmt.streamguys1.com/main128.aac"),
    "wdcb":   ("90.9 WDCB Jazz & Blues",         "http://wdcb-ice.streamguys.org/wdcb128"),
    "wluw":   ("88.7 WLUW",                      "https://ice26.securenetsystems.net/WLUW"),
    "wckg":   ("WCKG 102.3",                     "https://ice7.securenetsystems.net/WCKG"),
    "wscr":   ("670 The Score (WSCR)",           "https://stream.abacast.net/direct/audacy-wscrfmaac-imc"),
    "wnua":   ("Smooth Jazz WNUA",               "https://vip2.fastcast4u.com/proxy/wnua?mp=/1"),
}
DEFAULT_STATION = next(iter(STATIONS))

# Extra non-Chicago presets for /stream only.
EXTRA_PRESETS: dict[str, str] = {
    "SomaFM Groove Salad": "https://ice1.somafm.com/groovesalad-128-mp3",
    "SomaFM Defcon": "https://ice1.somafm.com/defcon-128-mp3",
    "Radio Paradise (Main)": "https://stream.radioparadise.com/mp3-128",
    "Radio Paradise (Mellow)": "https://stream.radioparadise.com/mellow-128",
    "NTS Radio 1": "https://stream-relay-geo.ntslive.net/stream",
    "KEXP Seattle": "https://kexp.streamguys1.com/kexp128.mp3",
}

PRESETS: dict[str, str] = {label: url for label, url in STATIONS.values()} | EXTRA_PRESETS
