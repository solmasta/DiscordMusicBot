# Discord Music Bot

A Discord bot that plays audio from YouTube, SoundCloud, direct URLs, and more — 24/7.

## Features

| Command | Description |
|---|---|
| `/play <query>` | Play a song from a URL or search YouTube |
| `/skip` | Skip the current song |
| `/pause` | Pause playback |
| `/resume` | Resume playback |
| `/stop` | Stop and clear the queue |
| `/queue` | Show the current queue |
| `/nowplaying` | Show what's currently playing |
| `/volume <1–100>` | Adjust playback volume |
| `/loop` | Toggle looping the current track |
| `/loopqueue` | Toggle looping the entire queue |
| `/shuffle` | Shuffle the queue |
| `/remove <position>` | Remove a song from the queue |
| `/move <from> <to>` | Reorder a song in the queue |
| `/clearqueue` | Clear the queue without stopping playback |
| `/join` | Pull the bot into your voice channel |
| `/leave` | Disconnect the bot |

## Requirements

- Python 3.10+
- FFmpeg (must be in PATH)
- A Discord bot token

## Setup

### 1. Create a Discord Application & Bot

1. Go to https://discord.com/developers/applications
2. Click **New Application**, give it a name.
3. Go to **Bot** → **Add Bot**.
4. Under **Privileged Gateway Intents**, enable **Message Content Intent** and **Server Members Intent**.
5. Copy the **Token** — you'll need it shortly.

### 2. Invite the Bot to Your Server

Generate an invite URL from **OAuth2 → URL Generator**:
- Scopes: `bot`, `applications.commands`
- Bot permissions: `Connect`, `Speak`, `Send Messages`, `Embed Links`, `Read Message History`

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
# Optionally set GUILD_ID=your_server_id for instant slash command registration
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
