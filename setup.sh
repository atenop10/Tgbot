#!/usr/bin/env bash
# One-command installer for Ubuntu/Debian VPS. Run:  bash setup.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_NAME="$(whoami)"

if [ ! -f credentials.json ]; then
  echo "credentials.json is missing. Put it in this folder first."; exit 1
fi

echo "== Installing Python =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

echo "== Installing bot packages =="
python3 -m venv venv
./venv/bin/pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  echo "== Bot settings =="
  read -rp "Telegram bot token (from @BotFather): " BOT_TOKEN
  read -rp "Your Telegram numeric ID (from @userinfobot): " OWNER_ID
  read -rp "Drive folder ID (press Enter to skip): " FOLDER
  printf "BOT_TOKEN=%s\nOWNER_ID=%s\nDRIVE_FOLDER_ID=%s\n" "$BOT_TOKEN" "$OWNER_ID" "$FOLDER" > .env
  chmod 600 .env
fi

if [ ! -f token.json ]; then
  echo "== Google Drive login =="
  ./venv/bin/python auth.py
  chmod 600 token.json
fi

echo "== Creating background service =="
sudo tee /etc/systemd/system/zipbot.service >/dev/null <<UNIT
[Unit]
Description=Telegram Zip to Drive Bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now zipbot
sleep 2
sudo systemctl --no-pager status zipbot | head -n 5
echo
echo "Done! Open your bot in Telegram and send /start"
