import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import enqueue
from telegram.error import BadRequest

from hamster_tg import retry
from hamster_tg.downloader import DownloadResult


def test_long_error_is_bounded_and_token_redacted():
    results = [
        DownloadResult(str(i), 1, error=OSError(retry.BOT_TOKEN + "😀" * 500))
        for i in range(30)
    ]
    text = retry.result_text(results, "folder")
    assert retry.BOT_TOKEN not in text
    assert "[BOT_TOKEN]" in text
    assert len(text.encode("utf-16-le")) // 2 <= 3500


@pytest.mark.parametrize(
    "message",
    ["message to edit not found", "message can't be edited", "Message is not modified"],
)
def test_edit_failure_falls_back_except_unchanged_summary(service, bot, message):
    job = enqueue(service.db, service.store)
    service.db.fail(job["id"], "PermissionError: denied")
    batch = service.db.claim_report(86400, 1000)
    batch["summary_id"] = 123
    bot.edit_message_text.side_effect = BadRequest(message)

    async def scenario():
        try:
            await service.report(batch)
            assert bot.send_message.await_count == int(
                message != "Message is not modified"
            )
        finally:
            await service.close()

    asyncio.run(scenario())


def test_retry_callback_only_enqueues_and_checks_chat(service):
    job = enqueue(service.db, service.store)
    service.db.fail(job["id"], "failed")
    batch = service.db.claim_report(86400, 1000)
    service.db.report_done(batch, 900)
    query = SimpleNamespace(
        data=f"retry:{batch['token']}",
        message=SimpleNamespace(chat=SimpleNamespace(id=8)),
        answer=AsyncMock(),
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"service": service})
    )

    async def scenario():
        try:
            await retry.handle_retry(SimpleNamespace(callback_query=query), context)
            assert query.answer.call_args.kwargs["show_alert"]
            query.message.chat.id = 7
            await retry.handle_retry(SimpleNamespace(callback_query=query), context)
            assert query.answer.call_args.args[0] == "已加入重试队列。"
            await retry.handle_retry(SimpleNamespace(callback_query=query), context)
            assert query.answer.call_args.args[0] == "正在重试，请稍候。"
        finally:
            await service.close()

    asyncio.run(scenario())


def test_late_album_member_does_not_change_claimed_summary_snapshot(service, bot):
    service.store.directory("default")
    service.db.enqueue(7, 42, "first", None, "default", "album", 0)
    job = service.db.claim()
    temporary = service.store.temporary(job)
    temporary.write_bytes(b"first")
    from hamster_tg.storage import file_sha256

    service.store.commit(job, temporary, file_sha256(temporary), ".jpg")
    batch = service.db.claim_report(86400, 1000)
    service.db.enqueue(7, 43, "late", None, "default", "album", 0)

    async def scenario():
        try:
            await service.report(batch)
            text = bot.send_message.call_args.kwargs["text"]
            assert "新增 1" in text
            assert "失败 0" in text
            assert service.db.claim_report(86400, 1000) is None
            late = service.db.claim()
            service.db.fail(late["id"], "failed")
            next_batch = service.db.claim_report(86400, 1000)
            await service.report(next_batch)
            assert bot.send_message.await_count == 1
            assert bot.edit_message_text.await_count == 1
            assert "失败 1" in bot.edit_message_text.call_args.kwargs["text"]
        finally:
            await service.close()

    asyncio.run(scenario())
