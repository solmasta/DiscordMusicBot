#!/usr/bin/env bash
# Usage (on an Ubuntu server):
#   curl -fsSL https://raw.githubusercontent.com/solmasta/DiscordMusicBot/main/deploy/oracle-setup.sh \
#     | sudo bash -s -- <DISCORD_TOKEN>
# Re-run it any time to pull the latest bot and restart.
set -euo pipefail

TOKEN="${1:?Usage: sudo bash -s -- <DISCORD_TOKEN>}"
DIR=/opt/crue-fm

if ! command -v apt-get >/dev/null; then
  echo "This script needs an Ubuntu server image." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git ffmpeg libopus0 python3-venv

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone https://github.com/solmasta/DiscordMusicBot "$DIR"
fi

python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --upgrade pip
"$DIR/venv/bin/pip" install -r "$DIR/requirements.txt"

install -m 600 /dev/null /etc/crue-fm.env
printf 'DISCORD_TOKEN=%s\n' "$TOKEN" > /etc/crue-fm.env

cat > /etc/systemd/system/crue-fm.service <<EOF
[Unit]
Description=CRUE FM Discord radio
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/crue-fm.env
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python -u bot.py
Restart=always
RestartSec=10
DynamicUser=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable crue-fm
systemctl restart crue-fm
echo "CRUE FM is running. Watch it with: sudo journalctl -u crue-fm -f"
