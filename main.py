import logging
import os
import re
import shutil
import tempfile
import hashlib
from pathlib import Path

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("TELEGRAM_API_BASE_URL", "http://telegram-bot-api:8081/bot")
BASE_FILE_URL = os.environ.get(
    "TELEGRAM_API_BASE_FILE_URL", "http://telegram-bot-api:8081/file/bot"
)
DOWNLOAD_ROOT = Path(os.environ.get("DOWNLOAD_ROOT", "/downloads"))

FOLDER_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff\u3400-\u4dbf-]+$")

# chat_id -> current folder name
chat_folders: dict[int, str] = {}

def _get_dest_dir(folder: str) -> Path:
    dest = DOWNLOAD_ROOT / folder
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _safe_copy(src: Path, dest_dir: Path) -> Path:
    """Copy src into dest_dir; skip if same-content duplicate exists."""
    target = dest_dir / src.name
    if not target.exists():
        shutil.copy2(src, target)
        return target
    if _file_sha256(src) == _file_sha256(target):
        return target

    stem, suffix = src.stem, src.suffix
    counter = 1
    while True:
        target = dest_dir / f"{stem}_{counter}{suffix}"
        if not target.exists():
            shutil.copy2(src, target)
            return target
        counter += 1


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Welcome! Send me photos, videos, GIFs, or files and I'll save them.\n\n"
        "Commands:\n"
        "/newfolder <name> — create & switch to a folder\n"
        "/status — show current folder and file count"
    )


async def newfolder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /newfolder <name>")
        return

    name = context.args[0]
    if not FOLDER_NAME_RE.match(name):
        await update.message.reply_text(
            "Invalid folder name. Only letters, digits, Chinese characters, '-' and '_' are allowed."
        )
        return

    dest = DOWNLOAD_ROOT / name
    dest.mkdir(parents=True, exist_ok=True)
    chat_folders[update.effective_chat.id] = name
    await update.message.reply_text(f"Switched to folder: {name}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    folder = chat_folders.get(update.effective_chat.id, "default")
    dest = DOWNLOAD_ROOT / folder
    dest.mkdir(parents=True, exist_ok=True)
    count = sum(1 for f in dest.iterdir() if f.is_file())
    await update.message.reply_text(
        f"Current folder: {folder}\nFiles: {count}"
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat_id = update.effective_chat.id
    folder = chat_folders.get(chat_id, "default")

    if message.photo:
        tg_file = await message.photo[-1].get_file()
    elif message.video:
        tg_file = await message.video.get_file()
    elif message.animation:
        tg_file = await message.animation.get_file()
    elif message.document and message.document.mime_type:
        mime = message.document.mime_type
        if mime.startswith("image/") or mime.startswith("video/"):
            tg_file = await message.document.get_file()
        else:
            return
    else:
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)

    dest_dir = _get_dest_dir(folder)
    filename = Path(tg_file.file_path).name
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir) / filename
        await tg_file.download_to_drive(tmp)
        saved = _safe_copy(tmp, dest_dir)
    await message.reply_text(f"Saved to {folder}/{saved.name}")


def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("newfolder", "Create & switch to a folder"),
        BotCommand("status", "Show current folder and file count"),
    ])


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .base_url(BASE_URL)
        .base_file_url(BASE_FILE_URL)
        .local_mode(True)
        .post_init(post_init)
        .build()
    )

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newfolder", newfolder))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.IMAGE | filters.Document.VIDEO,
            handle_media,
        )
    )

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
