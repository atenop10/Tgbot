import asyncio
import logging
import os
import re
import shutil
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])  # only this Telegram user can use the bot
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID") or None  # optional target folder
WORK_DIR = Path(os.getenv("WORK_DIR", "work"))
TOKEN_FILE = "token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("zipbot")


# ---------- Google Drive ----------
def drive_service():
    if not os.path.exists(TOKEN_FILE):
        raise RuntimeError("token.json not found. Run `python auth.py` first.")
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            Path(TOKEN_FILE).write_text(creds.to_json())
        else:
            raise RuntimeError("Drive credentials invalid. Re-run `python auth.py`.")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_to_drive(path: Path) -> str:
    service = drive_service()
    metadata = {"name": path.name}
    if DRIVE_FOLDER_ID:
        metadata["parents"] = [DRIVE_FOLDER_ID]
    media = MediaFileUpload(str(path), mimetype="application/zip", resumable=True)
    result = (
        service.files()
        .create(body=metadata, media_body=media, fields="id, webViewLink")
        .execute()
    )
    return result["webViewLink"]


# ---------- Helpers ----------
def user_dir(user_id: int) -> Path:
    d = WORK_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", name).strip() or "file"


def unique_path(folder: Path, name: str) -> Path:
    p = folder / safe_name(name)
    stem, suffix, i = p.stem, p.suffix, 1
    while p.exists():
        p = folder / f"{stem}_{i}{suffix}"
        i += 1
    return p


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != OWNER_ID:
            if update.message:
                await update.message.reply_text("Sorry, this bot is private.")
            return
        return await func(update, context)

    return wrapper


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------- Handlers ----------
@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me files (as documents, photos, videos, audio).\n\n"
        "/zip [name]  - zip everything and upload to Google Drive\n"
        "/list        - show queued files\n"
        "/clear       - discard queued files\n\n"
        "Tip: send photos as 'File' to keep original quality."
    )


@owner_only
async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.document:
        tg_file_id, name = msg.document.file_id, msg.document.file_name or "document"
    elif msg.photo:
        p = msg.photo[-1]
        tg_file_id, name = p.file_id, f"photo_{p.file_unique_id}.jpg"
    elif msg.video:
        tg_file_id = msg.video.file_id
        name = msg.video.file_name or f"video_{msg.video.file_unique_id}.mp4"
    elif msg.audio:
        tg_file_id = msg.audio.file_id
        name = msg.audio.file_name or f"audio_{msg.audio.file_unique_id}.mp3"
    elif msg.voice:
        tg_file_id, name = msg.voice.file_id, f"voice_{msg.voice.file_unique_id}.ogg"
    else:
        return

    dest = unique_path(user_dir(update.effective_user.id), name)
    try:
        tg_file = await context.bot.get_file(tg_file_id)
        await tg_file.download_to_drive(custom_path=dest)
    except Exception as e:
        log.exception("download failed")
        await msg.reply_text(
            f"Could not download '{name}': {e}\n"
            "(Telegram's default limit for bots is 20 MB.)"
        )
        return

    count = len(list(user_dir(update.effective_user.id).iterdir()))
    await msg.reply_text(f"Added {dest.name} ({human_size(dest.stat().st_size)}). Queue: {count} file(s).")


@owner_only
async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = sorted(user_dir(update.effective_user.id).iterdir())
    if not files:
        await update.message.reply_text("Queue is empty.")
        return
    total = sum(f.stat().st_size for f in files)
    lines = [f"{i}. {f.name} ({human_size(f.stat().st_size)})" for i, f in enumerate(files, 1)]
    lines.append(f"\nTotal: {human_size(total)}")
    await update.message.reply_text("\n".join(lines))


@owner_only
async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    shutil.rmtree(user_dir(update.effective_user.id), ignore_errors=True)
    await update.message.reply_text("Queue cleared.")


def make_zip(folder: Path, zip_path: Path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(folder.iterdir()):
            zf.write(f, arcname=f.name)


@owner_only
async def zip_and_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    folder = user_dir(uid)
    files = [f for f in folder.iterdir() if f.is_file()]
    if not files:
        await update.message.reply_text("No files queued. Send me some files first.")
        return

    name = safe_name(" ".join(context.args)) if context.args else "archive"
    if not name.lower().endswith(".zip"):
        name += ".zip"

    zip_dir = WORK_DIR / f"_zips_{uid}"
    zip_dir.mkdir(parents=True, exist_ok=True)
    zip_path = zip_dir / name

    status = await update.message.reply_text(f"Zipping {len(files)} file(s)...")
    try:
        await asyncio.to_thread(make_zip, folder, zip_path)
        await status.edit_text(f"Zipped ({human_size(zip_path.stat().st_size)}). Uploading to Drive...")
        link = await asyncio.to_thread(upload_to_drive, zip_path)
        await status.edit_text(f"Uploaded: {name}\n{link}")
        shutil.rmtree(folder, ignore_errors=True)  # clear queue after success
    except Exception as e:
        log.exception("zip/upload failed")
        await status.edit_text(f"Failed: {e}\nYour queued files are kept; try /zip again.")
    finally:
        shutil.rmtree(zip_dir, ignore_errors=True)


def main():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_files))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("zip", zip_and_upload))
    media = filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE
    app.add_handler(MessageHandler(media, receive_file))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
