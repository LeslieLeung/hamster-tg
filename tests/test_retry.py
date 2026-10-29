import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from hamster_tg import downloader, handlers, media_group, retry, state
from hamster_tg.app import create_application
from hamster_tg.downloader import DownloadResult


def make_bot():
    return SimpleNamespace(send_chat_action=AsyncMock(), send_message=AsyncMock())


def make_query(token, chat_id=7):
    return SimpleNamespace(
        data=f"retry:{token}",
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id)),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


def test_failure_message_includes_details_actual_attempts_and_button(monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(
        retry,
        "save_files",
        AsyncMock(
            return_value=[
                DownloadResult("file", 1, error=PermissionError("disk denied"))
            ]
        ),
    )

    asyncio.run(retry.save_and_report(bot, 7, ["file"], "original", 42))

    sent = bot.send_message.call_args.kwargs
    assert "PermissionError: disk denied" in sent["text"]
    assert "尝试 1 次" in sent["text"]
    data = sent["reply_markup"].inline_keyboard[0][0].callback_data
    assert len(data.encode()) <= 64
    request = state.retry_requests[data.removeprefix("retry:")]
    assert request.file_ids == ["file"]
    assert request.folder == "original"
    assert sent["reply_to_message_id"] == 42


def test_album_retry_saves_only_failures_to_original_folder(monkeypatch):
    bot = make_bot()
    save = AsyncMock(
        side_effect=[
            [
                DownloadResult("good", 1, path=Path("file_0.jpg")),
                DownloadResult("bad", 6, error=TimeoutError("timed out")),
            ],
            [DownloadResult("bad", 1, path=Path("file_1.jpg"))],
        ]
    )
    monkeypatch.setattr(retry, "save_files", save)
    asyncio.run(retry.save_and_report(bot, 7, ["good", "bad"], "original", 42))
    token = next(iter(state.retry_requests))
    state.chat_folders[7] = "new-folder"
    query = make_query(token)

    asyncio.run(
        retry.handle_retry(
            SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot)
        )
    )

    save.assert_awaited_with(bot, 7, ["bad"], "original")
    assert token not in state.retry_requests
    assert query.edit_message_text.call_args.kwargs["reply_markup"] is None
    assert "original/file_1.jpg" in query.edit_message_text.call_args.kwargs["text"]


def test_retry_partial_failure_keeps_only_remaining_files(monkeypatch):
    token = retry.register_retry(state.RetryRequest(7, "folder", ["a", "b"], 42))
    monkeypatch.setattr(
        retry,
        "save_files",
        AsyncMock(
            return_value=[
                DownloadResult("a", 1, path=Path("file_0.jpg")),
                DownloadResult("b", 1, error=OSError("still full")),
            ]
        ),
    )
    query = make_query(token)

    asyncio.run(
        retry.handle_retry(
            SimpleNamespace(callback_query=query), SimpleNamespace(bot=make_bot())
        )
    )

    request = state.retry_requests[token]
    assert request.file_ids == ["b"]
    assert not request.in_progress
    sent = query.edit_message_text.call_args.kwargs
    assert "OSError: still full" in sent["text"]
    assert sent["reply_markup"].inline_keyboard[0][0].callback_data == f"retry:{token}"


def test_duplicate_click_does_not_start_second_download(monkeypatch):
    token = retry.register_retry(state.RetryRequest(7, "folder", ["a"], 42))

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def save(*args):
            started.set()
            await release.wait()
            return [DownloadResult("a", 1, path=Path("file_0.jpg"))]

        mock = AsyncMock(side_effect=save)
        monkeypatch.setattr(retry, "save_files", mock)
        context = SimpleNamespace(bot=make_bot())
        first = asyncio.create_task(
            retry.handle_retry(
                SimpleNamespace(callback_query=make_query(token)), context
            )
        )
        await started.wait()
        duplicate = make_query(token)
        try:
            await retry.handle_retry(SimpleNamespace(callback_query=duplicate), context)
            duplicate.answer.assert_awaited_once_with("正在重试，请稍候。")
            assert mock.await_count == 1
        finally:
            release.set()
            await first

    asyncio.run(scenario())


def test_expired_and_cross_chat_buttons_do_not_download(monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(retry, "save_files", save)
    token = retry.register_retry(state.RetryRequest(7, "folder", ["a"], 42))
    context = SimpleNamespace(bot=make_bot())
    foreign = make_query(token, chat_id=8)
    asyncio.run(retry.handle_retry(SimpleNamespace(callback_query=foreign), context))
    assert foreign.answer.call_args.kwargs["show_alert"]
    state.retry_requests[token].created_at -= retry.RETRY_REQUEST_TTL_SECONDS + 1
    expired = make_query(token)
    asyncio.run(retry.handle_retry(SimpleNamespace(callback_query=expired), context))
    assert expired.answer.call_args.kwargs["show_alert"]
    save.assert_not_awaited()


def test_retry_result_is_sent_if_original_message_cannot_be_edited(monkeypatch):
    token = retry.register_retry(state.RetryRequest(7, "folder", ["a"], 42))
    monkeypatch.setattr(
        retry,
        "save_files",
        AsyncMock(
            return_value=[
                DownloadResult("a", 1, path=Path("file_0.jpg")),
            ]
        ),
    )
    query = make_query(token)
    query.edit_message_text.side_effect = BadRequest("message to edit not found")
    bot = make_bot()

    asyncio.run(
        retry.handle_retry(
            SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot)
        )
    )

    assert bot.send_message.call_args.kwargs["text"] == "Saved to folder/file_0.jpg"


def test_upload_action_failure_does_not_prevent_saving(monkeypatch):
    bot = make_bot()
    bot.send_chat_action.side_effect = BadRequest("action unavailable")
    download = AsyncMock(return_value=[])
    monkeypatch.setattr(retry, "download_file_ids_serially", download)

    asyncio.run(retry.save_files(bot, 7, ["a"], "original"))

    download.assert_awaited_once_with(bot, ["a"], retry.DOWNLOAD_ROOT / "original")


def test_long_error_details_are_bounded_and_bot_token_is_redacted():
    results = [
        DownloadResult(str(i), 1, error=OSError(retry.BOT_TOKEN + "😀" * 500))
        for i in range(30)
    ]
    text = retry.result_text(results, "folder")
    assert retry.BOT_TOKEN not in text
    assert "[BOT_TOKEN]" in text
    assert len(text.encode("utf-16-le")) // 2 <= 3500


def test_retry_request_cache_is_bounded_without_evicting_active_jobs(monkeypatch):
    monkeypatch.setattr(retry, "RETRY_REQUEST_MAX_ENTRIES", 1)
    first = retry.register_retry(state.RetryRequest(7, "folder", ["a"], 42))
    second = retry.register_retry(state.RetryRequest(7, "folder", ["b"], 42))
    assert first not in state.retry_requests
    state.retry_requests[second].in_progress = True
    assert retry.register_retry(state.RetryRequest(7, "folder", ["c"], 42)) is None
    assert list(state.retry_requests) == [second]


def test_single_media_uses_shared_reporting(monkeypatch):
    report = AsyncMock()
    monkeypatch.setattr(handlers, "save_and_report", report)
    message = SimpleNamespace(
        photo=[SimpleNamespace(file_id="photo")],
        media_group_id=None,
        message_id=42,
    )
    bot = make_bot()
    asyncio.run(
        handlers.handle_media(
            SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=7)),
            SimpleNamespace(bot=bot),
        )
    )
    report.assert_awaited_once_with(bot, 7, ["photo"], "default", 42)


def test_album_uses_shared_reporting(monkeypatch):
    report = AsyncMock()
    monkeypatch.setattr(media_group, "save_and_report", report)
    monkeypatch.setattr(media_group, "MEDIA_GROUP_ACK_DELAY_SECONDS", 0)
    state.pending_media_group_acks[(7, "group")] = {
        "folder": "album",
        "reply_to_message_id": 42,
        "file_ids": ["a", "b"],
    }
    bot = make_bot()
    asyncio.run(media_group.flush_media_group_ack(bot, 7, "group"))
    report.assert_awaited_once_with(bot, 7, ["a", "b"], "album", 42)


def test_app_registers_retry_callback_without_blocking_other_updates():
    app = create_application()
    handler = next(h for h in app.handlers[0] if h.callback is retry.handle_retry)
    assert handler.pattern.match("retry:token")
    assert handler.block is False


def test_failed_download_then_button_retry_saves_file(monkeypatch, tmp_path):
    monkeypatch.setattr(retry, "DOWNLOAD_ROOT", tmp_path)
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())

    async def download(path, **kwargs):
        path.write_bytes(b"photo")

    tg_file = SimpleNamespace(
        file_path="photo.jpg", download_to_drive=AsyncMock(side_effect=download)
    )
    bot = make_bot()
    bot.get_file = AsyncMock(side_effect=[OSError("storage unavailable"), tg_file])

    async def scenario():
        await retry.save_and_report(bot, 7, ["photo"], "original", 42)
        assert (
            "OSError: storage unavailable" in bot.send_message.call_args.kwargs["text"]
        )
        token = next(iter(state.retry_requests))
        query = make_query(token)
        await retry.handle_retry(
            SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot)
        )
        assert query.edit_message_text.call_args.kwargs["reply_markup"] is None

    asyncio.run(scenario())

    assert (tmp_path / "original" / "file_0.jpg").read_bytes() == b"photo"
    assert state.retry_requests == {}


def test_retry_cancellation_releases_request_for_next_click(monkeypatch):
    import pytest

    token = retry.register_retry(state.RetryRequest(7, "folder", ["a"], 42))
    monkeypatch.setattr(
        retry, "save_files", AsyncMock(side_effect=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            retry.handle_retry(
                SimpleNamespace(callback_query=make_query(token)),
                SimpleNamespace(bot=make_bot()),
            )
        )
    assert not state.retry_requests[token].in_progress
    assert state.retry_requests[token].file_ids == ["a"]
