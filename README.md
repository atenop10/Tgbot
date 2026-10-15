# Telegram Zip -> Google Drive Bot

Send files to the bot, then `/zip name` to zip them and upload the archive to Google Drive.

## Commands
- `/zip [name]` - zip queued files and upload to Drive
- `/list` - show queued files
- `/clear` - discard queued files

## Install on a VPS (Ubuntu/Debian)
```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git zipbot
cd zipbot
# put your Google credentials.json in this folder, then:
bash setup.sh
```
`setup.sh` installs everything, asks for your bot token and Telegram ID,
logs in to Google Drive, and starts the bot as a background service.

## Getting credentials.json
1. console.cloud.google.com -> new project -> enable **Google Drive API**
2. OAuth consent screen -> External -> add your Gmail as a test user
3. Credentials -> Create OAuth client ID -> **Desktop app** -> download JSON
4. Rename to `credentials.json`

## Notes
- Telegram bots can download files up to 20 MB each (cloud Bot API).
- Never commit `.env`, `token.json` or `credentials.json`.
