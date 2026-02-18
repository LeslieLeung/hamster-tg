import logging
import os
import re
import shutil
import tempfile
import hashlib
import asyncio
from pathlib import Path

from telegram import Bot, BotCommand, Update
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
# (chat_id, media_group_id) -> pending summary state
pending_media_group_acks: dict[tuple[int, str], dict[str, object]] = {}

MEDIA_GROUP_ACK_DELAY_SECONDS = 1.0

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


def _queue_media_group_ack(
    bot: Bot, chat_id: int, media_group_id: str, folder: str, reply_to_message_id: int
) -> None:
    key = (chat_id, media_group_id)
    state = pending_media_group_acks.get(key)
    if state is None:
        state = {
            "count": 0,
            "folder": folder,
            "reply_to_message_id": reply_to_message_id,
            "task": None,
        }
        pending_media_group_acks[key] = state

    state["count"] = int(state["count"]) + 1
    state["folder"] = folder
    task = state.get("task")
    if isinstance(task, asyncio.Task):
        task.cancel()
    state["task"] = asyncio.create_task(_flush_media_group_ack(bot, chat_id, media_group_id))


async def _flush_media_group_ack(bot: Bot, chat_id: int, media_group_id: str) -> None:
    await asyncio.sleep(MEDIA_GROUP_ACK_DELAY_SECONDS)

    key = (chat_id, media_group_id)
    state = pending_media_group_acks.pop(key, None)
    if not state:
        return

    count = int(state["count"])
    folder = str(state["folder"])
    reply_to_message_id = int(state["reply_to_message_id"])

    noun = "file" if count == 1 else "files"
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"Saved {count} {noun} to {folder}",
            reply_to_message_id=reply_to_message_id,
        )
    except Exception:
        logger.exception("Failed to send media group summary for chat_id=%s", chat_id)


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

    if message.media_group_id:
        # For album-style uploads, send one aggregated acknowledgement.
        _queue_media_group_ack(
            context.bot, chat_id, message.media_group_id, folder, message.message_id
        )
    else:
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
