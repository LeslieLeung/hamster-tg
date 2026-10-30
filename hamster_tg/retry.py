import logging
import secrets
from time import monotonic

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from . import state
from .config import (
    BOT_TOKEN,
    DOWNLOAD_ROOT,
    RETRY_REQUEST_MAX_ENTRIES,
    RETRY_REQUEST_TTL_SECONDS,
)
from .downloader import DownloadResult, download_file_ids_serially

logger = logging.getLogger(__name__)


def prune_retry_requests() -> None:
    now = monotonic()
    for token, request in list(state.retry_requests.items()):
        if (
            not request.in_progress
            and now - request.created_at >= RETRY_REQUEST_TTL_SECONDS
        ):
            del state.retry_requests[token]


def register_retry(request: state.RetryRequest) -> str | None:
    prune_retry_requests()
    if len(state.retry_requests) >= RETRY_REQUEST_MAX_ENTRIES:
        for token, previous in state.retry_requests.items():
            if not previous.in_progress:
                del state.retry_requests[token]
                break
        else:
            return None
    token = secrets.token_urlsafe(16)
    state.retry_requests[token] = request
    return token


def truncate_text(text: str, max_units: int) -> str:
    # Telegram counts UTF-16 units, so emoji can occupy two units.
    encoded = text.encode("utf-16-le", errors="replace")
    if len(encoded) <= max_units * 2:
        return text
    return encoded[: (max_units - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def result_text(results: list[DownloadResult], folder: str) -> str:
    failures = [
        (i, result) for i, result in enumerate(results, 1) if result.path is None
    ]
    saved_count = len(results) - len(failures)
    if not failures:
        if len(results) == 1:
            return f"Saved to {folder}/{results[0].path.name}"
        return f"Saved {saved_count} files to {folder}"

    lines = [
        f"保存到 {folder}：成功 {saved_count}/{len(results)}，失败 {len(failures)}。"
    ]
    for index, result in failures:
        error = result.error
        detail = f"{type(error).__name__}: {error}" if error else "未知错误"
        detail = detail.replace(BOT_TOKEN, "[BOT_TOKEN]")
        lines.append(
            f"文件 {index}（尝试 {result.attempts} 次）：{truncate_text(detail, 240)}"
        )
    return truncate_text("\n".join(lines), 3500)


def retry_markup(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("重试失败文件", callback_data=f"retry:{token}")]]
    )


async def save_files(
    bot: Bot, chat_id: int, file_ids: list[str], folder: str
) -> list[DownloadResult]:
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
    except TelegramError:
        logger.warning(
            "Failed to send upload action for chat_id=%s", chat_id, exc_info=True
        )
    return await download_file_ids_serially(bot, file_ids, DOWNLOAD_ROOT / folder)


async def save_and_report(
    bot: Bot,
    chat_id: int,
    file_ids: list[str],
    folder: str,
    reply_to_message_id: int,
) -> None:
    results = await save_files(bot, chat_id, file_ids, folder)
    failed_ids = [result.file_id for result in results if result.path is None]
    text = result_text(results, folder)
    markup = None
    if failed_ids:
        token = register_retry(
            state.RetryRequest(chat_id, folder, failed_ids, reply_to_message_id)
        )
        if token is not None:
            markup = retry_markup(token)
        else:
            text += "\n重试任务已满，请重新发送失败文件。"
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=markup,
        reply_to_message_id=reply_to_message_id,
        allow_sending_without_reply=True,
    )


async def handle_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not isinstance(query.data, str) or query.message is None:
        return
    token = query.data.removeprefix("retry:")
    prune_retry_requests()
    request = state.retry_requests.get(token)
    if request is None or request.chat_id != query.message.chat.id:
        await query.answer("重试已失效，请重新发送文件。", show_alert=True)
        return
    if request.in_progress:
        await query.answer("正在重试，请稍候。")
        return

    request.in_progress = True
    try:
        try:
            await query.answer("已开始重试。")
        except TelegramError:
            logger.warning("Failed to answer retry callback", exc_info=True)
        results = await save_files(
            context.bot, request.chat_id, request.file_ids, request.folder
        )
        request.file_ids = [result.file_id for result in results if result.path is None]
        request.created_at = monotonic()
        markup = retry_markup(token) if request.file_ids else None
        if not request.file_ids:
            del state.retry_requests[token]
        text = result_text(results, request.folder)
        try:
            await query.edit_message_text(text=text, reply_markup=markup)
        except TelegramError as exc:
            if (
                isinstance(exc, BadRequest)
                and "message is not modified" in str(exc).lower()
            ):
                return
            # A deleted or uneditable summary must not hide the retry result.
            logger.warning("Failed to edit retry summary", exc_info=True)
            await context.bot.send_message(
                chat_id=request.chat_id,
                text=text,
                reply_markup=markup,
                reply_to_message_id=request.reply_to_message_id,
                allow_sending_without_reply=True,
            )
    finally:
        request.in_progress = False
