import asyncio
import logging

from telegram import Bot

from . import state
from .config import MEDIA_GROUP_ACK_DELAY_SECONDS
from .retry import save_and_report

logger = logging.getLogger(__name__)


def queue_media_group_ack(
    bot: Bot,
    chat_id: int,
    media_group_id: str,
    folder: str,
    reply_to_message_id: int,
    file_id: str,
) -> None:
    key = (chat_id, media_group_id)
    pending_state = state.pending_media_group_acks.get(key)
    if pending_state is None:
        pending_state = {
            "count": 0,
            "folder": folder,
            "reply_to_message_id": reply_to_message_id,
            "file_ids": [],
            "task": None,
        }
        state.pending_media_group_acks[key] = pending_state

    pending_state["count"] = int(pending_state["count"]) + 1
    pending_state["folder"] = folder
    pending_state["reply_to_message_id"] = min(
        int(pending_state["reply_to_message_id"]), reply_to_message_id
    )
    file_ids = pending_state.get("file_ids")
    if isinstance(file_ids, list):
        file_ids.append(file_id)
    task = pending_state.get("task")
    if isinstance(task, asyncio.Task):
        task.cancel()
    pending_state["task"] = asyncio.create_task(
        flush_media_group_ack(bot, chat_id, media_group_id)
    )


async def flush_media_group_ack(bot: Bot, chat_id: int, media_group_id: str) -> None:
    await asyncio.sleep(MEDIA_GROUP_ACK_DELAY_SECONDS)

    key = (chat_id, media_group_id)
    pending_state = state.pending_media_group_acks.pop(key, None)
    if not pending_state:
        return

    folder = str(pending_state["folder"])
    reply_to_message_id = int(pending_state["reply_to_message_id"])
    file_ids = [str(x) for x in pending_state.get("file_ids", [])]

    try:
        await save_and_report(bot, chat_id, file_ids, folder, reply_to_message_id)
    except Exception:
        logger.exception("Failed to send media group summary for chat_id=%s", chat_id)
