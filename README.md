# Crüe FM: Q101 24/7

A Discord bot that sits in one voice channel and plays **Q101** around the clock. If the stream drops or the bot is
disconnected, it rejoins and restarts within seconds. One station, no rotation.

- **Live Now Playing card** in the voice channel's chat (or `NOW_PLAYING_CHANNEL_ID`): song, artist, album art, progress
  bar and an animated equalizer, updated as the radio plays.
- **Join prompt:** when someone joins the radio channel, the radio is muted for a moment while they choose
  **Approve** or **Dismiss**, so it never blasts them. It comes back by itself after 30 seconds.
- `/radio` shows what's playing.
- **Music commands** (home server only, set with `GUILD_ID`): `/play`, `/skip`, `/pause`, `/resume`, `/stop`, `/queue`,
  `/nowplaying`, `/volume`, `/loop`, `/loopqueue`, `/shuffle`, `/remove`, `/move`, `/clearqueue`, `/join`, `/leave`, `/help`.
  Note: `/play` uses yt-dlp for YouTube and similar sites, so check those sites' terms before using it.

## Setup

1. Create a bot at https://discord.com/developers/applications (no privileged intents needed) and copy its token.
2. Invite it with scopes `bot` and `applications.commands`, and permissions **View Channel**, **Connect**, **Speak**,
   **Send Messages**, **Embed Links**, **Attach Files** and **Read Message History** (the last four are for the card).
3. Right-click the voice channel (Developer Mode on) and **Copy Channel ID**.
4. `cp .env.example .env` and fill in `DISCORD_TOKEN` and `RADIO_CHANNEL_ID`. The bot reads `.env` itself.

## Run it

Locally (Python 3.10+ and FFmpeg on your PATH):

```bash
pip install -r requirements.txt
python bot.py
```

On Windows PowerShell, if `pip` is blocked by an Application Control policy, use `python -m pip install -r requirements.txt`.

With Docker (for a server; restarts after crashes and reboots):

```bash
docker compose up -d --build
docker compose logs -f
```

## Free 24/7 hosting

A voice bot needs a machine that stays on, which rules out most free "sleeping" web hosts. Options: any computer you
leave on (a spare PC or Raspberry Pi) or an Oracle Cloud "Always Free" VM (check their current limits and idle-reclaim
rules). On a fresh Ubuntu VM:

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
git clone https://github.com/solmasta/DiscordMusicBot.git && cd DiscordMusicBot
cp .env.example .env && nano .env
sudo docker compose up -d --build
```

## Settings (`.env`)

See `.env.example`: `GUILD_ID` (home server for `/play`), `NOW_PLAYING_CHANNEL_ID`, `JOIN_MUTE`, `BRANDING` (off by
default; turns on renaming the bot to Crüe FM with its icon and banner), `RADIO_URL` (a different stream).

## Tests

`python -m unittest discover` (also runs on pull requests in GitHub Actions).
