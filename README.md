# Crüe FM: Q101 24/7

A Discord bot that sits in one voice channel 24/7 and plays free Chicago radio. Use `/radio` to get a drop-down and
pick a station; the choice applies to everyone in the channel. If a stream drops or the bot is disconnected, it rejoins
and restarts within 30 seconds.

| Station | Genre |
|---|---|
| Q101 (WKQX), 97.1 The Drive, 97.9 The Loop, Rock 95.5, 93.1 WXRT | Rock |
| 107.5 WGCI, V103, WSRB 106.3 | Hip hop / R&B |

- `/radio` posts the drop-down (you must be in the radio's voice channel to change the station).
- `/nowplaying` shows the current station.
- Stations live in the `STATIONS` list at the top of `bot.py`; these are the stations' own free public streams, and a
  station can change or retire its stream, so if one goes quiet, update its `url` there.
- `DEFAULT_STATION` (optional, a key like `wgci`) sets what plays at startup. Default is Q101.

## Setup

1. Create a bot at https://discord.com/developers/applications (no privileged intents needed), copy its token.
2. Invite it with scopes `bot` and `applications.commands`, and permissions **View Channel**, **Connect**, **Speak**.
3. Right-click the voice channel (Developer Mode on) and **Copy Channel ID**.
4. `cp .env.example .env` and fill in `DISCORD_TOKEN` and `VOICE_CHANNEL_ID`.

## Run it

Locally (needs Python 3.10+ and FFmpeg on your PATH):

```bash
pip install -r requirements.txt
set -a; . ./.env; set +a
python bot.py
```

With Docker (this is what you'd use on a server):

```bash
docker compose up -d --build     # starts it, restarts it after crashes and reboots
docker compose logs -f           # watch it
```

## Free 24/7 hosting

A voice bot needs a machine that stays on, which rules out most free "sleeping" web hosts. The best free option
I know of is an **Oracle Cloud "Always Free" VM** (check Oracle's current free-tier terms and region availability;
signup asks for a card to verify you). On a fresh Ubuntu VM:

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
git clone https://github.com/solmasta/DiscordMusicBot.git && cd DiscordMusicBot
cp .env.example .env && nano .env        # token and channel ID
sudo docker compose up -d --build
```

To update later: `git pull && sudo docker compose up -d --build`.
Any other always-on machine (a spare PC, a Raspberry Pi) works the same way.

## Tests

`python -m unittest discover` (also runs on pull requests in GitHub Actions).
