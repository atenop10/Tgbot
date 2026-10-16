import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from telegram import Update
from telegram.error import RetryAfter
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
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID") or None  # default parent folder
LOCAL_API_URL = os.getenv("LOCAL_API_URL") or None  # e.g. http://localhost:8081 (files up to 2 GB)
WORK_DIR = Path(os.getenv("WORK_DIR", "work"))
TOKEN_FILE = "token.json"
SETTINGS_FILE = Path("settings.json")
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL") or 8)  # files handled at the same time
COMPRESS_WORKERS = int(os.getenv("COMPRESS_WORKERS") or (os.cpu_count() or 1))
UPLOAD_WORKERS = int(os.getenv("UPLOAD_WORKERS") or 3)
COMPRESS_SEM: asyncio.Semaphore | None = None
UPLOAD_SEM: asyncio.Semaphore | None = None
active: dict[int, int] = {}  # user id -> files still being received/processed
_chat_locks: dict[int, asyncio.Lock] = {}
SCOPES = ["https://www.googleapis.com/auth/drive"]

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("zipbot")


# ---------- Settings (remembered between restarts) ----------
def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def save_settings(data: dict):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))


def get_user_settings(uid: int) -> dict:
    return load_settings().get(str(uid), {})


def set_user_setting(uid: int, **kwargs):
    data = load_settings()
    data.setdefault(str(uid), {}).update(kwargs)
    save_settings(data)


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


def get_or_create_folder(path: str) -> str:
    """Find or create a folder (supports nested like 'Work/Reports'). Returns its ID."""
    service = drive_service()
    parent = DRIVE_FOLDER_ID or "root"
    for part in [p.strip() for p in path.split("/") if p.strip()]:
        escaped = part.replace("\\", "\\\\").replace("'", "\\'")
        q = (
            f"name='{escaped}' and mimeType='application/vnd.google-apps.folder' "
            f"and '{parent}' in parents and trashed=false"
        )
        res = service.files().list(q=q, fields="files(id)", pageSize=1).execute()
        if res["files"]:
            parent = res["files"][0]["id"]
        else:
            meta = {
                "name": part,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent],
            }
            parent = service.files().create(body=meta, fields="id").execute()["id"]
    return parent


def upload_to_drive(path: Path, folder_id: str | None) -> str:
    service = drive_service()
    metadata = {"name": path.name}
    if folder_id:
        metadata["parents"] = [folder_id]
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    media = MediaFileUpload(str(path), mimetype=mime, resumable=True)
    result = (
        service.files()
        .create(body=metadata, media_body=media, fields="id, webViewLink")
        .execute(num_retries=3)
    )
    return result["webViewLink"]


def target_folder_id(uid: int) -> str | None:
    return get_user_settings(uid).get("folder_id") or DRIVE_FOLDER_ID


def target_folder_name(uid: int) -> str:
    return get_user_settings(uid).get("folder") or "My Drive (root)"


# ---------- Helpers ----------
def user_dir(user_id: int) -> Path:
    d = WORK_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", name).strip() or "file"


def unique_path(folder: Path, name: str) -> Path:
    """Pick a free file name and reserve it (safe when many files arrive at once)."""
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / safe_name(name)
    stem, suffix, i = p.stem, p.suffix, 1
    while True:
        try:
            with open(p, "x"):
                pass
            return p
        except FileExistsError:
            p = folder / f"{stem}_{i}{suffix}"
            i += 1


async def say(msg, text):
    """Reply without hitting Telegram's flood limit (about 1 message/second per chat)."""
    lock = _chat_locks.setdefault(msg.chat_id, asyncio.Lock())
    async with lock:
        for _ in range(5):
            try:
                r = await msg.reply_text(text)
                await asyncio.sleep(0.8)
                return r
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception:
                log.exception("reply failed")
                return None
    return None


async def wait_idle(uid: int, msg=None):
    """Wait until every file the user already sent has finished downloading/compressing."""
    n = active.get(uid, 0)
    if n and msg:
        await say(msg, f"Waiting for {n} file(s) still being processed...")
    while active.get(uid, 0) > 0:
        await asyncio.sleep(0.5)


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


# ---------- Audio compression ----------
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wma", ".aiff", ".aif", ".amr"}


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTS


def compress_audio(src: Path, bitrate: int, mono: bool):
    """Re-encode to MP3. Returns (final_path, old_size, new_size). Keeps the original if no smaller."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed (run: sudo apt install -y ffmpeg)")
    old = src.stat().st_size
    tmp = src.with_name(src.stem + ".compressing.mp3")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-vn", "-map_metadata", "0", "-codec:a", "libmp3lame", "-b:a", f"{bitrate}k",
    ]
    if mono:
        cmd += ["-ac", "1"]
    cmd.append(str(tmp))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(r.stderr.strip()[-300:] or "ffmpeg failed")
    new = tmp.stat().st_size
    if new >= old:  # already small/efficient: keep the original
        tmp.unlink(missing_ok=True)
        return src, old, old
    src.unlink()
    final = unique_path(src.parent, src.with_suffix(".mp3").name)
    tmp.rename(final)
    return final, old, new


# ---------- Handlers ----------
@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me files (documents, photos, videos, audio).\n\n"
        "/folder Name   - set the Drive folder (created if missing, 'A/B' for nested)\n"
        "/upload        - upload queued files directly (no zip)\n"
        "/zip [name]    - zip queued files and upload\n"
        "/auto on|off   - upload every file instantly when received\n"
        "/compress on|off - shrink audio files (to MP3) before upload\n"
        "/bitrate 64    - audio quality in kbps (16-320, lower = smaller)\n"
        "/mono on|off   - mix audio to mono (about half the size)\n"
        "/list          - show queued files\n"
        "/clear         - discard queued files\n\n"
        "Tip: send photos as 'File' to keep original quality."
    )


@owner_only
async def set_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            f"Current folder: {target_folder_name(uid)}\n"
            "Change it with: /folder My Folder Name\n"
            "Reset to root with: /folder root"
        )
        return
    name = " ".join(context.args).strip()
    if name.lower() == "root":
        set_user_setting(uid, folder=None, folder_id=None)
        await update.message.reply_text("Folder reset to My Drive root.")
        return
    status = await update.message.reply_text(f"Looking for folder '{name}'...")
    try:
        folder_id = await asyncio.to_thread(get_or_create_folder, name)
        set_user_setting(uid, folder=name, folder_id=folder_id)
        await status.edit_text(f"Folder set: {name}\nAll uploads will go there.")
    except Exception as e:
        log.exception("folder failed")
        await status.edit_text(f"Could not set folder: {e}")


@owner_only
async def set_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        state = "ON" if get_user_settings(uid).get("auto") else "OFF"
        await update.message.reply_text(f"Auto-upload is {state}. Use /auto on or /auto off.")
        return
    set_user_setting(uid, auto=(arg == "on"))
    if arg == "on":
        await update.message.reply_text(
            f"Auto-upload ON. Every file you send goes straight to: {target_folder_name(uid)}"
        )
    else:
        await update.message.reply_text("Auto-upload OFF. Files are queued until /upload or /zip.")


@owner_only
async def set_compress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    arg = context.args[0].lower() if context.args else ""
    if arg in ("on", "off"):
        set_user_setting(uid, compress=(arg == "on"))
    st = get_user_settings(uid)
    await update.message.reply_text(
        f"Audio compression: {'ON' if st.get('compress') else 'OFF'}\n"
        f"Bitrate: {st.get('bitrate', 64)} kbps | Mono: {'yes' if st.get('mono') else 'no'}\n"
        "Change with /compress on|off, /bitrate 64, /mono on|off"
    )


@owner_only
async def set_bitrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        kbps = int(context.args[0])
        assert 16 <= kbps <= 320
    except Exception:
        await update.message.reply_text(
            "Use a number from 16 to 320, e.g. /bitrate 64\n"
            "32 = voice/podcast, 64 = small music, 128 = good, 192+ = high quality."
        )
        return
    set_user_setting(uid, bitrate=kbps)
    await update.message.reply_text(f"Audio bitrate set to {kbps} kbps.")


@owner_only
async def set_mono(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    arg = context.args[0].lower() if context.args else ""
    if arg not in ("on", "off"):
        await update.message.reply_text("Use /mono on or /mono off")
        return
    set_user_setting(uid, mono=(arg == "on"))
    await update.message.reply_text(f"Mono audio: {arg.upper()}")


@owner_only
async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    active[uid] = active.get(uid, 0) + 1
    try:
        await _receive(update, context)
    finally:
        active[uid] -= 1


async def _receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = update.effective_user.id
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

    dest = unique_path(user_dir(uid), name)
    try:
        tg_file = await context.bot.get_file(
            tg_file_id, read_timeout=3600, write_timeout=3600, connect_timeout=60
        )
        local = Path(tg_file.file_path) if tg_file.file_path else None
        if LOCAL_API_URL and local and local.is_absolute() and local.exists():
            # Local Bot API server already saved the file on this machine: just move it.
            await asyncio.to_thread(shutil.move, str(local), str(dest))
        else:
            await tg_file.download_to_drive(custom_path=dest)
    except Exception as e:
        log.exception("download failed")
        dest.unlink(missing_ok=True)
        note = "" if LOCAL_API_URL else "\n(Telegram's default bot limit is 20 MB. Run bigfiles.sh to allow up to 2 GB.)"
        await say(msg, f"Could not download '{name}': {e}{note}")
        return

    info = ""
    st = get_user_settings(uid)
    if st.get("compress") and is_audio(dest):
        try:
            async with COMPRESS_SEM:  # at most one ffmpeg per CPU core
                try:
                    await context.bot.send_chat_action(msg.chat_id, "typing")
                except Exception:
                    pass
                dest, old, new = await asyncio.to_thread(
                    compress_audio, dest, st.get("bitrate", 64), st.get("mono", False)
                )
            if new < old:
                info = f"\nCompressed {human_size(old)} -> {human_size(new)}"
            else:
                info = "\n(Already small, original kept)"
        except Exception as e:
            log.exception("compression failed")
            info = f"\nCompression failed, original kept: {e}"

    if st.get("auto"):
        try:
            async with UPLOAD_SEM:
                link = await asyncio.to_thread(upload_to_drive, dest, target_folder_id(uid))
            dest.unlink(missing_ok=True)
            await say(msg, f"Uploaded {dest.name} to {target_folder_name(uid)}{info}\n{link}")
        except Exception as e:
            log.exception("auto upload failed")
            await say(msg, f"Upload failed: {e}\nFile kept in queue; try /upload.")
        return

    count = len(list(user_dir(uid).iterdir()))
    await say(
        msg,
        f"Added {dest.name} ({human_size(dest.stat().st_size)}). Queue: {count} file(s).{info}",
    )


@owner_only
async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    files = sorted(user_dir(uid).iterdir())
    if not files:
        await update.message.reply_text(f"Queue is empty.\nFolder: {target_folder_name(uid)}")
        return
    total = sum(f.stat().st_size for f in files)
    lines = [f"{i}. {f.name} ({human_size(f.stat().st_size)})" for i, f in enumerate(files, 1)]
    lines.append(f"\nTotal: {human_size(total)}\nFolder: {target_folder_name(uid)}")
    await update.message.reply_text("\n".join(lines))


@owner_only
async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    shutil.rmtree(user_dir(update.effective_user.id), ignore_errors=True)
    await update.message.reply_text("Queue cleared.")


@owner_only
async def upload_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await wait_idle(uid, update.message)
    files = sorted(f for f in user_dir(uid).iterdir() if f.is_file())
    if not files:
        await say(update.message, "No files queued. Send me some files first.")
        return

    folder_id = target_folder_id(uid)
    status = await say(
        update.message, f"Uploading {len(files)} file(s) to {target_folder_name(uid)}..."
    )
    done, failed, last = 0, [], 0.0

    async def one(f: Path):
        nonlocal done, last
        try:
            async with UPLOAD_SEM:
                await asyncio.to_thread(upload_to_drive, f, folder_id)
            f.unlink(missing_ok=True)
            done += 1
        except Exception as e:
            log.exception("upload failed for %s", f.name)
            failed.append(f"{f.name}: {e}")
        now = time.monotonic()
        if status and now - last > 3:
            last = now
            try:
                await status.edit_text(f"Uploading... {done + len(failed)}/{len(files)}")
            except Exception:
                pass

    await asyncio.gather(*(one(f) for f in files))

    text = f"Uploaded {done}/{len(files)} file(s) to {target_folder_name(uid)}."
    if failed:
        text += "\n\nFailed (kept in queue):\n" + "\n".join(failed)
    if status:
        try:
            await status.edit_text(text)
            return
        except Exception:
            pass
    await say(update.message, text)


def make_zip(folder: Path, zip_path: Path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(folder.iterdir()):
            zf.write(f, arcname=f.name)


@owner_only
async def zip_and_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await wait_idle(uid, update.message)
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
        link = await asyncio.to_thread(upload_to_drive, zip_path, target_folder_id(uid))
        await status.edit_text(f"Uploaded: {name} to {target_folder_name(uid)}\n{link}")
        shutil.rmtree(folder, ignore_errors=True)
    except Exception as e:
        log.exception("zip/upload failed")
        await status.edit_text(f"Failed: {e}\nYour queued files are kept; try /zip again.")
    finally:
        shutil.rmtree(zip_dir, ignore_errors=True)


async def post_init(app):
    global COMPRESS_SEM, UPLOAD_SEM
    COMPRESS_SEM = asyncio.Semaphore(max(1, COMPRESS_WORKERS))
    UPLOAD_SEM = asyncio.Semaphore(max(1, UPLOAD_WORKERS))
    log.info(
        "Parallel mode: %d files at once, %d compress, %d upload",
        MAX_PARALLEL, COMPRESS_WORKERS, UPLOAD_WORKERS,
    )


def main():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    builder = (
        Application.builder().token(BOT_TOKEN)
        .concurrent_updates(MAX_PARALLEL).post_init(post_init)
    )
    if LOCAL_API_URL:
        builder = (
            builder.base_url(f"{LOCAL_API_URL}/bot")
            .base_file_url(f"{LOCAL_API_URL}/file/bot")
            .local_mode(True)
        )
        log.info("Using local Bot API server at %s", LOCAL_API_URL)
    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("folder", set_folder))
    app.add_handler(CommandHandler("auto", set_auto))
    app.add_handler(CommandHandler("compress", set_compress))
    app.add_handler(CommandHandler("bitrate", set_bitrate))
    app.add_handler(CommandHandler("mono", set_mono))
    app.add_handler(CommandHandler("upload", upload_direct))
    app.add_handler(CommandHandler("zip", zip_and_upload))
    app.add_handler(CommandHandler("list", list_files))
    app.add_handler(CommandHandler("clear", clear))
    media = filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE
    app.add_handler(MessageHandler(media, receive_file))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
