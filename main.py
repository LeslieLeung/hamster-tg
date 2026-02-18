import asyncio
import logging
import os
import re
import shutil
import tempfile
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

# group_key -> list of saved filenames
pending_groups: dict[str, list[str]] = {}
# group_key -> asyncio.Task for delayed flush
group_tasks: dict[str, asyncio.Task] = {}


async def _flush_group(
    group_key: str, chat_id: int, reply_to_message_id: int, bot, folder: str
) -> None:
    await asyncio.sleep(2)
    saved_names = pending_groups.pop(group_key, [])
    group_tasks.pop(group_key, None)
    if not saved_names:
        return
    count = len(saved_names)
    await bot.send_message(
        chat_id=chat_id,
        text=f"Saved {count} file(s) to {folder}/",
        reply_to_message_id=reply_to_message_id,
    )


def _get_dest_dir(chat_id: int) -> Path:
    folder = chat_folders.get(chat_id, "default")
    dest = DOWNLOAD_ROOT / folder
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _safe_copy(src: Path, dest_dir: Path) -> Path:
    """Copy src into dest_dir, appending _1, _2, ... on name collision."""
    target = dest_dir / src.name
    if not target.exists():
        shutil.copy2(src, target)
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

    dest_dir = _get_dest_dir(chat_id)
    filename = Path(tg_file.file_path).name
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir) / filename
        await tg_file.download_to_drive(tmp)
        saved = _safe_copy(tmp, dest_dir)
    folder = chat_folders.get(chat_id, "default")

    if message.media_group_id:
        group_key = f"{chat_id}:{message.media_group_id}"
        pending_groups.setdefault(group_key, []).append(saved.name)
        existing = group_tasks.get(group_key)
        if existing and not existing.done():
            existing.cancel()
        group_tasks[group_key] = asyncio.create_task(
            _flush_group(group_key, chat_id, message.message_id, context.bot, folder)
        )
    else:
        await message.reply_text(f"Saved to {folder}/{saved.name}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
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
