# Crüe FM

A Discord bot that plays live radio from every US state and free music by genre in your voice channel.
Pick a city, get a station that actually works, and keep it on air. Optional **Premium** (a Discord server
subscription) adds true 24/7, your own theme (name, colour, tagline, logo), more saved favorites and priority slots.

## Commands

**Every server**

| Command | Description |
|---|---|
| `/stations play [place] [genre]` | Type a city or state; plays the best working station there (uses your remembered area if empty) |
| `/stations browse` | Guided menus: state, city, genre, station |
| `/stations search <text>` | Search by name, call letters, frequency or genre |
| `/stations now` / `stop` / `volume` | See what's playing, stop, set the server volume |
| `/stations panel` | Post a permanent control panel (managers) |
| `/stations favorite` / `favorites` / `unfavorite` | Save the playing station, play a saved one, remove one (3 saved free, 25 Premium) |
| `/stations forget` | Forget the area the bot remembered for you |
| `/library` | Browse free (Creative Commons) songs by genre |
| `/radio` | Show what's playing |
| `/premium` | See the server's plan and subscribe |
| `/vote` | Vote on top.gg, then give your server a free 12-hour boost (24/7 + priority slots) |
| `/theme set` / `logo` / `preview` / `reset` | Premium: your server's name, colour, tagline and logo (managers) |

**Home server only** (`GUILD_ID`): the original radio rotation and music commands (`/play`, `/queue`, `/skip`,
`/volume`, `/loop`, `/shuffle`, `/help` and friends) are registered only on that server.

Free servers can run the radio for `FREE_SESSION_HOURS` (default 4) per session once Premium is enabled;
Premium servers run 24/7.

## Requirements

- Python 3.10+
- FFmpeg (must be in PATH)
- A Discord bot token

## Setup

### 1. Create a Discord Application & Bot

1. Go to https://discord.com/developers/applications
2. Click **New Application**, give it a name.
3. Go to **Bot** → **Add Bot**.
4. No privileged intents are needed (leave them off).
5. Copy the **Token** — you'll need it shortly.

### 2. Invite the Bot to Your Server

Generate an invite URL from **OAuth2 → URL Generator**:
- Scopes: `bot`, `applications.commands`
- Bot permissions: `Connect`, `Speak`, `Send Messages`, `Embed Links`

### 3. Install Dependencies

```bash
# Install FFmpeg
# Ubuntu/Debian:
sudo apt install ffmpeg
# macOS (Homebrew):
brew install ffmpeg
# Windows: download from https://ffmpeg.org/download.html and add to PATH

# Install Python packages
pip install -r requirements.txt
```

### 4. Configure Environment

```bash
cp .env.example .env
# Edit .env and set DISCORD_TOKEN=your_token_here
# GUILD_ID = your home server (gets the music commands). PREMIUM_SKU_ID turns Premium on.
```

### 5. Run

```bash
python bot.py
```

## Running 24/7 (Linux systemd)

Create `/etc/systemd/system/musicbot.service`:

```ini
[Unit]
Description=Discord Music Bot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/DiscordMusicBot
ExecStart=/usr/bin/python3 /path/to/DiscordMusicBot/bot.py
Restart=always
RestartSec=10
EnvironmentFile=/path/to/DiscordMusicBot/.env

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable musicbot
sudo systemctl start musicbot
sudo systemctl status musicbot
```

## Running 24/7 (Docker / cloud)

A minimal Dockerfile is provided:

```bash
docker build -t musicbot .
docker run -d --env-file .env --restart unless-stopped musicbot
```

## Supported Sources

yt-dlp supports 1000+ sites including:
- YouTube (videos, playlists — first track played from playlists)
- SoundCloud
- Bandcamp
- Twitch streams
- Direct MP3/audio URLs
- Many more


## Song library (`/library`)

Browse songs by genre and play them. Two sources:

- **Free music** (every server): Creative Commons songs. It works with no setup, using ccMixter. For a bigger
  catalog and cover art you can add Jamendo (free, but their developer accounts are approved by hand):
  make an app at https://developer.jamendo.com, then
  `fly secrets set JAMENDO_CLIENT_ID=your_client_id --app discordmusicbot-k-zztq`.
  With a key, Jamendo is tried first and ccMixter fills in any genre it has nothing for. Each song's licence
  is shown on its card. Jamendo's free API is for non-commercial use; if you start charging for the bot, get
  a Jamendo licence first, and check each ccMixter track's licence (some are non-commercial).
- **Popular hits** (home server only): a curated list per genre, found on YouTube when played. These are not
  licensed for a public bot, so they never play on other servers.

Songs play on the home radio (the rotation pauses until they finish or someone taps **Back to rotation**), or
through the public player on other servers (the list repeats until stopped; **Skip song** moves on).


## Premium (Discord server subscriptions)

Off until you set `PREMIUM_SKU_ID` (Developer Portal > Monetization > create a server subscription).
Then `fly secrets set PREMIUM_SKU_ID=<id> -a discordmusicbot-k-zztq`. Settings: `PREMIUM_RESERVED_STREAMS`,
`FREE_SESSION_HOURS`, `FREE_FAVORITES`, `PREMIUM_FAVORITES` (see `.env.example`).

## Vote perks (top.gg)

Each top.gg vote earns one credit; `/vote` spends it on a server for 12 hours of true 24/7 and priority slots
(stacks up to 48 hours ahead; themes and favorites stay Premium-only). Setup: in top.gg > your bot > Webhooks, set
the URL to `https://discordmusicbot-k-zztq.fly.dev/topgg` and run
`fly secrets set TOPGG_WEBHOOK_SECRET=<the secret top.gg gives you> -a discordmusicbot-k-zztq`.
The bot serves `/healthz` and `/topgg` on port 8080; `/topgg` refuses everything until the secret is set.

## Tests and deploys

`python -m unittest discover` runs the tests. Pull requests and pushes to `main` run them in GitHub Actions;
a push to `main` deploys to Fly.io only if they pass.

## Before you charge for it

Read `docs/launch-checklist.md`: licensing, privacy, capacity and what is still untested.
