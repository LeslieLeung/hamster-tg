import logging

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes

from . import state
from .config import DOWNLOAD_MAX_RETRIES, FOLDER_NAME_RE
from .downloader import download_one_file_serially
from .media_group import queue_media_group_ack
from .storage import get_dest_dir, recent_folder_names


logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Welcome! Send me photos, videos, GIFs, or files and I'll save them.\n\n"
        "Commands:\n"
        "/new <name> — create & switch to a folder\n"
        "/newfolder <name> — create & switch to a folder\n"
        "/list — show 10 recent folders\n"
        "/status — show current folder and file count"
    )


async def switch_folder(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command: str
) -> None:
    if not context.args:
        await update.message.reply_text(f"Usage: /{command} <name>")
        return

    name = context.args[0]
    if not FOLDER_NAME_RE.match(name):
        await update.message.reply_text(
            "Invalid folder name. Only letters, digits, Chinese characters, '-' and '_' are allowed."
        )
        return

    get_dest_dir(name)
    state.chat_folders[update.effective_chat.id] = name
    await update.message.reply_text(f"Switched to folder: {name}")


async def new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await switch_folder(update, context, "new")


async def newfolder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await switch_folder(update, context, "newfolder")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    folder = state.chat_folders.get(update.effective_chat.id, "default")
    dest = get_dest_dir(folder)
    count = sum(1 for f in dest.iterdir() if f.is_file())
    await update.message.reply_text(f"Current folder: {folder}\nFiles: {count}")


async def list_folders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    folders = recent_folder_names()
    if not folders:
        await update.message.reply_text("No folders found.")
        return

    lines = [f"{i}. {name}" for i, name in enumerate(folders, start=1)]
    await update.message.reply_text("Recent folders:\n" + "\n".join(lines))


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat_id = update.effective_chat.id
    folder = state.chat_folders.get(chat_id, "default")

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
        queue_media_group_ack(
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
        dest_dir = get_dest_dir(folder)
        saved = await download_one_file_serially(context.bot, file_id, dest_dir)
        if saved is None:
            await message.reply_text(
                f"Failed to save after {DOWNLOAD_MAX_RETRIES} attempts"
            )
            return
        await message.reply_text(f"Saved to {folder}/{saved.name}")


def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("new", "Create & switch to a folder"),
            BotCommand("newfolder", "Create & switch to a folder"),
            BotCommand("list", "Show 10 recent folders"),
            BotCommand("status", "Show current folder and file count"),
        ]
    )
