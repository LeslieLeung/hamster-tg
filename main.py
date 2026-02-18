import logging
import os
import re
import shutil
import tempfile
import hashlib
import asyncio
import random
from pathlib import Path

from telegram import Bot, BotCommand, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, RetryAfter, TimedOut
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

# PTB wiki recommends timer-based media-group collection. Keep this in 1-2s range.
MEDIA_GROUP_ACK_DELAY_SECONDS = 1.5
DOWNLOAD_MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 0.5
MEDIA_GROUP_MAX_CONCURRENT_DOWNLOADS = 3
GET_FILE_READ_TIMEOUT_SECONDS = 20.0
# Set to None to disable read timeout for large/slow media downloads.
DOWNLOAD_FILE_READ_TIMEOUT_SECONDS: float | None = None
REQUEST_CONNECT_TIMEOUT_SECONDS = 10.0
REQUEST_POOL_TIMEOUT_SECONDS = 30.0


def _is_retryable_download_error(exc: Exception) -> bool:
    return isinstance(exc, (TimedOut, NetworkError, RetryAfter))


def _retry_delay_seconds(attempt: int, exc: Exception) -> float:
    delay = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    delay += random.uniform(0, 0.25)
    if isinstance(exc, RetryAfter):
        delay = max(delay, float(exc.retry_after))
    return delay


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
    bot: Bot,
    chat_id: int,
    media_group_id: str,
    folder: str,
    reply_to_message_id: int,
    file_id: str,
) -> None:
    key = (chat_id, media_group_id)
    state = pending_media_group_acks.get(key)
    if state is None:
        state = {
            "count": 0,
            "folder": folder,
            "reply_to_message_id": reply_to_message_id,
            "file_ids": [],
            "task": None,
        }
        pending_media_group_acks[key] = state

    state["count"] = int(state["count"]) + 1
    state["folder"] = folder
    state["reply_to_message_id"] = min(
        int(state["reply_to_message_id"]), reply_to_message_id
    )
    file_ids = state.get("file_ids")
    if isinstance(file_ids, list):
        file_ids.append(file_id)
    task = state.get("task")
    if isinstance(task, asyncio.Task):
        task.cancel()
    state["task"] = asyncio.create_task(_flush_media_group_ack(bot, chat_id, media_group_id))


async def _download_and_save_with_retry(
    bot: Bot, file_id: str, dest_dir: Path
) -> Path | None:
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
        try:
            tg_file = await bot.get_file(
                file_id,
                read_timeout=GET_FILE_READ_TIMEOUT_SECONDS,
                connect_timeout=REQUEST_CONNECT_TIMEOUT_SECONDS,
                pool_timeout=REQUEST_POOL_TIMEOUT_SECONDS,
            )
            filename = Path(tg_file.file_path or file_id).name
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir) / filename
                await tg_file.download_to_drive(
                    tmp,
                    read_timeout=DOWNLOAD_FILE_READ_TIMEOUT_SECONDS,
                    connect_timeout=REQUEST_CONNECT_TIMEOUT_SECONDS,
                    pool_timeout=REQUEST_POOL_TIMEOUT_SECONDS,
                )
                return _safe_copy(tmp, dest_dir)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < DOWNLOAD_MAX_RETRIES and _is_retryable_download_error(exc):
                delay = _retry_delay_seconds(attempt, exc)
                logger.warning(
                    "Network error for file_id=%s (%s, attempt %s/%s), retrying in %.2fs",
                    file_id,
                    type(exc).__name__,
                    attempt,
                    DOWNLOAD_MAX_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            break

    if last_error is not None:
        logger.error(
            "Download failed for file_id=%s after %s attempts: %s: %s",
            file_id,
            DOWNLOAD_MAX_RETRIES,
            type(last_error).__name__,
            last_error,
        )
    else:
        logger.error(
            "Download failed for file_id=%s after %s attempts",
            file_id,
            DOWNLOAD_MAX_RETRIES,
        )
    return None


async def _flush_media_group_ack(bot: Bot, chat_id: int, media_group_id: str) -> None:
    await asyncio.sleep(MEDIA_GROUP_ACK_DELAY_SECONDS)

    key = (chat_id, media_group_id)
    state = pending_media_group_acks.pop(key, None)
    if not state:
        return

    folder = str(state["folder"])
    reply_to_message_id = int(state["reply_to_message_id"])
    file_ids = [str(x) for x in state.get("file_ids", [])]

    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        dest_dir = _get_dest_dir(folder)
        semaphore = asyncio.Semaphore(MEDIA_GROUP_MAX_CONCURRENT_DOWNLOADS)

        async def _worker(file_id: str) -> Path | None:
            async with semaphore:
                return await _download_and_save_with_retry(bot, file_id, dest_dir)

        results = await asyncio.gather(*(_worker(file_id) for file_id in file_ids))
        saved_count = sum(1 for result in results if result is not None)
        failed_count = len(file_ids) - saved_count

        noun = "file" if saved_count == 1 else "files"
        if failed_count:
            text = f"Saved {saved_count}/{len(file_ids)} files to {folder} ({failed_count} failed)"
        else:
            text = f"Saved {saved_count} {noun} to {folder}"
        await bot.send_message(
            chat_id=chat_id,
            text=text,
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
        file_id = message.photo[-1].file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.animation:
        file_id = message.animation.file_id
    elif message.document and message.document.mime_type:
        mime = message.document.mime_type
        if mime.startswith("image/") or mime.startswith("video/"):
            file_id = message.document.file_id
        else:
            return
    else:
        return

    if message.media_group_id:
        # For album uploads, collect file_ids first and process in one batch reply.
        _queue_media_group_ack(
            context.bot,
            chat_id,
            message.media_group_id,
            folder,
            message.message_id,
            file_id,
        )
    else:
        await context.bot.send_chat_action(
            chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT
        )
        dest_dir = _get_dest_dir(folder)
        saved = await _download_and_save_with_retry(context.bot, file_id, dest_dir)
        if saved is None:
            await message.reply_text(
                f"Failed to save after {DOWNLOAD_MAX_RETRIES} attempts"
            )
            return
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
