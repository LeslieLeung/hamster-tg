import logging

from telegram import BotCommand

from .config import DOWNLOAD_ROOT, FOLDER_NAME_RE
from .service import SaveService
from .threads import blocking

logger = logging.getLogger(__name__)


def get_service(context):
    return context.application.bot_data["service"]


async def start(update, context):
    await update.message.reply_text(
        "发送图片、视频或 GIF，机器人会将它们加入保存队列。\n\n"
        "/new <名称> — 创建并切换目录\n"
        "/list — 最近目录\n/status — 当前目录和队列\n"
        "/scan [目录名] — 扫描文件并更新索引；运行中再次发送可查询进度"
    )


async def switch_folder(update, context, command):
    if len(context.args) != 1:
        await update.message.reply_text(f"Usage: /{command} <name>")
        return
    name = context.args[0]
    if not FOLDER_NAME_RE.fullmatch(name):
        await update.message.reply_text("目录名称仅支持字母、数字、中文、'-' 和 '_'。")
        return
    try:
        await get_service(context).switch_folder(update.effective_chat.id, name)
    except (OSError, ValueError) as exc:
        await update.message.reply_text(f"切换目录失败：{exc}")
        return
    await update.message.reply_text(f"Switched to folder: {name}")


async def new(update, context):
    await switch_folder(update, context, "new")


async def newfolder(update, context):
    await switch_folder(update, context, "newfolder")


async def status(update, context):
    service = get_service(context)
    folder = await blocking(service.db.current_folder, update.effective_chat.id)
    count, pending = await blocking(service.db.status, folder)
    info = await blocking(service.db.folder, folder)
    text = f"当前目录：{folder}\n文件：{count}\n等待/处理中：{pending}\n保存并发：{service.concurrency}"
    if info and not info["indexed"]:
        text += f"\n目录索引：{info['scan_state']}；未完成前任务将等待，可用 /scan {folder} 重试。"
    if service.scanner.running:
        text += "\n" + service.scanner.progress.text()
    await update.message.reply_text(text)


async def list_folders(update, context):
    folders = await blocking(get_service(context).db.recent_folders)
    text = (
        "Recent folders:\n"
        + "\n".join(f"{i}. {name}" for i, name in enumerate(folders, 1))
        if folders
        else "No folders found."
    )
    await update.message.reply_text(text)


async def scan(update, context):
    scanner = get_service(context).scanner
    if scanner.running:
        await update.message.reply_text(scanner.progress.text())
        return
    if len(context.args) > 1 or (
        context.args and not FOLDER_NAME_RE.fullmatch(context.args[0])
    ):
        await update.message.reply_text("Usage: /scan [目录名]")
        return
    folder = context.args[0] if context.args else None
    scanner.start(folder, update.effective_chat.id)
    await update.message.reply_text(
        "扫描已启动，完成后会发送汇总。再次发送 /scan 可查询进度。"
    )


async def handle_media(update, context):
    message = update.message
    if message.photo:
        media = message.photo[-1]
    elif message.video:
        media = message.video
    elif message.animation:
        media = message.animation
    elif (
        message.document
        and message.document.mime_type
        and message.document.mime_type.startswith(("image/", "video/"))
    ):
        media = message.document
    else:
        return
    await get_service(context).enqueue(
        update.effective_chat.id,
        message.message_id,
        media.file_id,
        getattr(media, "file_unique_id", None),
        message.media_group_id,
    )


async def error_handler(update, context):
    logger.error("Exception while handling an update:", exc_info=context.error)


async def post_init(application):
    await application.bot.set_my_commands(
        [
            BotCommand("new", "Create & switch to a folder"),
            BotCommand("newfolder", "Create & switch to a folder"),
            BotCommand("list", "Show 10 recent folders"),
            BotCommand("status", "Show current folder and pending files"),
            BotCommand("scan", "Scan saved files; repeat to check progress"),
        ]
    )
    service = SaveService(DOWNLOAD_ROOT, application.bot)
    application.bot_data["service"] = service
    await service.start()


async def post_stop(application):
    service = application.bot_data.get("service")
    if service:
        await service.close()
