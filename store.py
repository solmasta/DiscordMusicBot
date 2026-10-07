"""Saved per-server radio settings (SQLite), so a restart or deploy picks each server back up where
it left off. Lives on a Fly volume mounted at /data; without one it still works but is wiped by the
next deploy, and says so in the log."""
import asyncio
import dataclasses
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass

import directory as D

log = logging.getLogger("store")

DEFAULT_DIR = os.getenv("DATA_DIR", "/data")
SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_radio (
    guild_id         INTEGER PRIMARY KEY,
    station_json     TEXT    NOT NULL,
    url              TEXT    NOT NULL,
    voice_channel_id INTEGER NOT NULL,
    text_channel_id  INTEGER,
    started_by       INTEGER NOT NULL,
    volume           REAL    NOT NULL,
    updated_at       REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS guild_panel (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS user_area (
    user_id    INTEGER PRIMARY KEY,
    state      TEXT NOT NULL,
    city       TEXT,
    updated_at REAL NOT NULL
);
"""


@dataclass
class SavedRadio:
    guild_id: int
    station: D.Station
    url: str
    voice_channel_id: int
    text_channel_id: int | None
    started_by: int
    volume: float


def station_to_json(station: D.Station) -> str:
    data = dataclasses.asdict(station)
    data["tags"] = list(station.tags)
    data["genres"] = sorted(station.genres)
    return json.dumps(data, separators=(",", ":"))


def station_from_json(text: str) -> D.Station:
    data = json.loads(text)
    known = {f.name for f in dataclasses.fields(D.Station)}
    data = {k: v for k, v in data.items() if k in known}   # tolerate fields added or removed later
    data["tags"] = tuple(data.get("tags") or ())
    data["genres"] = frozenset(data.get("genres") or ())
    return D.Station(**data)


class Store:
    def __init__(self, directory: str | None = None):
        self.dir = directory or DEFAULT_DIR
        self.path = os.path.join(self.dir, "crue.db")
        self.persistent = False
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # ---- lifecycle
    async def open(self):
        await asyncio.to_thread(self._open)

    def _open(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            probe = os.path.join(self.dir, ".write-test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except OSError:
            self.dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
            os.makedirs(self.dir, exist_ok=True)
            self.path = os.path.join(self.dir, "crue.db")
        # A directory on its own device is a mounted volume; otherwise it's the container's disk.
        self.persistent = os.stat(self.dir).st_dev != os.stat("/").st_dev
        if self.persistent:
            log.info("Saved settings are stored on a persistent volume (%s)", self.dir)
        else:
            log.warning("No persistent volume at %s: saved settings will be lost on the next deploy", self.dir)
        try:
            self._connect()
        except sqlite3.DatabaseError as e:
            aside = f"{self.path}.corrupt-{int(time.time())}"
            log.error("Settings database is unreadable (%s); moved it to %s and starting fresh", e, aside)
            os.replace(self.path, aside)
            self._connect()

    def _connect(self):
        db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        db.execute("SELECT COUNT(*) FROM guild_radio").fetchone()
        db.commit()
        self._db = db

    async def close(self):
        def _close():
            with self._lock:
                if self._db:
                    self._db.close()
                    self._db = None
        await asyncio.to_thread(_close)

    # ---- operations
    def _run(self, sql: str, args: tuple = (), fetch: bool = False):
        with self._lock:
            if self._db is None:
                raise RuntimeError("store is not open")
            cur = self._db.execute(sql, args)
            rows = cur.fetchall() if fetch else None
            self._db.commit()
            return rows

    async def save(self, saved: SavedRadio):
        await asyncio.to_thread(
            self._run,
            "INSERT INTO guild_radio (guild_id, station_json, url, voice_channel_id, text_channel_id, started_by, volume, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET station_json=excluded.station_json, "
            "url=excluded.url, voice_channel_id=excluded.voice_channel_id, text_channel_id=excluded.text_channel_id, "
            "started_by=excluded.started_by, volume=excluded.volume, updated_at=excluded.updated_at",
            (saved.guild_id, station_to_json(saved.station), saved.url, saved.voice_channel_id, saved.text_channel_id,
             saved.started_by, float(saved.volume), time.time()),
        )

    async def update_volume(self, guild_id: int, volume: float):
        await asyncio.to_thread(self._run, "UPDATE guild_radio SET volume=?, updated_at=? WHERE guild_id=?",
                                (float(volume), time.time(), guild_id))

    async def delete(self, guild_id: int):
        await asyncio.to_thread(self._run, "DELETE FROM guild_radio WHERE guild_id=?", (guild_id,))

    async def all(self) -> list[SavedRadio]:
        rows = await asyncio.to_thread(
            self._run,
            "SELECT guild_id, station_json, url, voice_channel_id, text_channel_id, started_by, volume FROM guild_radio",
            (), True,
        )
        out = []
        for gid, sj, url, vcid, tcid, by, vol in rows:
            try:
                out.append(SavedRadio(gid, station_from_json(sj), url, vcid, tcid, by, vol))
            except Exception as e:
                log.warning("Skipping an unreadable saved record for server %s: %s", gid, e)
        return out

    # ---- listeners' remembered areas (so "where are you listening from?" is one tap next time)
    async def save_area(self, user_id: int, state: str, city: str | None):
        await asyncio.to_thread(
            self._run,
            "INSERT INTO user_area (user_id, state, city, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, city=excluded.city, updated_at=excluded.updated_at",
            (user_id, state, city, time.time()),
        )

    async def delete_area(self, user_id: int):
        await asyncio.to_thread(self._run, "DELETE FROM user_area WHERE user_id=?", (user_id,))

    async def all_areas(self) -> dict[int, tuple[str, str | None]]:
        rows = await asyncio.to_thread(self._run, "SELECT user_id, state, city FROM user_area", (), True)
        return {uid: (state, city) for uid, state, city in rows}

    # ---- the live control panel message each server has (edited in place as the radio changes)
    async def save_panel(self, guild_id: int, channel_id: int, message_id: int):
        await asyncio.to_thread(
            self._run,
            "INSERT INTO guild_panel (guild_id, channel_id, message_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=excluded.message_id",
            (guild_id, channel_id, message_id),
        )

    async def delete_panel(self, guild_id: int):
        await asyncio.to_thread(self._run, "DELETE FROM guild_panel WHERE guild_id=?", (guild_id,))

    async def all_panels(self) -> dict[int, tuple[int, int]]:
        rows = await asyncio.to_thread(self._run, "SELECT guild_id, channel_id, message_id FROM guild_panel", (), True)
        return {g: (c, m) for g, c, m in rows}
