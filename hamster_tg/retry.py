import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from .config import BOT_TOKEN

logger = logging.getLogger(__name__)


def truncate_text(text, max_units):
    encoded = text.encode("utf-16-le", errors="replace")
    if len(encoded) <= max_units * 2:
        return text
    return encoded[: (max_units - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def result_text(results, folder):
    failures = [
        (i, result) for i, result in enumerate(results, 1) if result.path is None
    ]
    duplicate = sum(result.duplicate for result in results)
    saved = len(results) - len(failures) - duplicate
    lines = [
        f"保存到 {folder}：新增 {saved}，已存在 {duplicate}，失败 {len(failures)}。"
    ]
    if len(results) == 1 and results[0].path:
        lines.append(f"{folder}/{results[0].path.name}")
    for index, result in failures:
        detail = (
            result.error
            if isinstance(result.error, str)
            else f"{type(result.error).__name__}: {result.error}"
            if result.error
            else "未知错误"
        )
        lines.append(
            f"文件 {index}（尝试 {result.attempts} 次）：{truncate_text(detail.replace(BOT_TOKEN, '[BOT_TOKEN]'), 240)}"
        )
    return truncate_text("\n".join(lines), 3500)


def retry_markup(token):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("重试失败文件", callback_data=f"retry:{token}")]]
    )


async def handle_retry(update, context):
    query = update.callback_query
    if query is None or not isinstance(query.data, str) or query.message is None:
        return
    service = context.application.bot_data["service"]
    result = await service.retry(
        query.data.removeprefix("retry:"), query.message.chat.id
    )
    text = {
        "queued": "已加入重试队列。",
        "busy": "正在重试，请稍候。",
        "expired": "重试已失效，请重新发送文件。",
    }[result]
    try:
        await query.answer(text, show_alert=result == "expired")
    except TelegramError:
        logger.warning("Failed to answer retry callback", exc_info=True)
