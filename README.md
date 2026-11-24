# Crüe FM: 24/7 Discord radio

A Discord bot that sits in one voice channel and plays an internet radio station around the clock. A background check
runs every 20 seconds: if the stream drops or the bot is disconnected, it rejoins and restarts.

## Commands

| Command | What it does |
|---|---|
| `/station` | Switch the default station from the drop-down: CRUE FM (SomaFM Metal), Underground 80s, and Chicago stations (Q101, The Loop, WLS-FM, WGCI, WBEZ, Vocalo, WFMT, WDCB, WLUW, WCKG, The Score, WNUA). Also cancels any `/stream`. |
| `/nowplaying` | Show the current default station and its stream URL. |
| `/stream play <url>` | Play any http(s) radio URL (Icecast/Shoutcast mp3/aac/ogg, `.m3u`/`.pls` playlists, HLS) or a preset, in your voice channel. Reconnects up to 5 times if it drops. |
| `/stream nowplaying` | Station name and current song, when the station publishes it (ICY metadata). |
| `/stream volume <1–100>` | Volume for the custom stream. |
| `/stream stations` | List preset stations. |
| `/stream stop` | Stop the custom stream and go back to the default station. |

Station lists live in `cogs/stations.py` (stream URLs can change without notice, so update them there if one goes quiet).

The bot also serves `/` and `/health` on `PORT` (default 8080) for host health checks.

## Setup

1. Create a bot at https://discord.com/developers/applications (no privileged intents needed) and copy its token.
2. Invite it with scopes `bot` and `applications.commands`, and permissions **View Channel**, **Connect** and **Speak**.
3. Right-click the voice channel (Developer Mode on) and **Copy Channel ID**.

## Configuration

The bot reads environment variables (it does not read `.env` itself; Docker Compose does).

| Variable | Required | Meaning |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token. |
| `VOICE_CHANNEL_ID` | yes | Voice channel the radio lives in. |
| `PORT` | no | Health-check port (default `8080`). |
| `PAUSED` | no | If set, only the health server runs and the bot stays off Discord. Use it to stand a copy down. |

## Run it

With Docker (restarts after crashes and reboots):

```bash
cp .env.example .env   # then fill in DISCORD_TOKEN and VOICE_CHANNEL_ID
docker compose up -d --build
docker compose logs -f
```

Locally (Python 3.10+, FFmpeg and libopus on your PATH):

```bash
pip install -r requirements.txt
export DISCORD_TOKEN=... VOICE_CHANNEL_ID=...
python bot.py
```

## Hosting 24/7

A voice bot needs a machine that stays on, so most "sleeping" free web hosts won't work.

- **Fly.io:** `fly.toml` is set up for it (`fly secrets set DISCORD_TOKEN=...`, then `fly deploy`).
- **Ubuntu server (e.g. Oracle Cloud Always Free):** `deploy/oracle-setup.sh` installs the bot as a systemd service:

  ```bash
  curl -fsSL https://raw.githubusercontent.com/solmasta/DiscordMusicBot/main/deploy/oracle-setup.sh \
    | sudo bash -s -- <DISCORD_TOKEN> [VOICE_CHANNEL_ID]
  ```

## Tests

```bash
python -m unittest discover
```

They also run on pull requests in GitHub Actions.
