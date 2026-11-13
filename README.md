# Crüe FM: Q101 24/7

A tiny Discord bot that sits in one voice channel and plays one station, Q101, around the clock.
If the stream drops or the bot gets disconnected, it rejoins and restarts within 30 seconds.

- `/nowplaying` shows the station.
- Station name and stream live at the top of `bot.py`. The stream defaults to the one in the code; set
  `STATION_URL` to use a different one.

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
