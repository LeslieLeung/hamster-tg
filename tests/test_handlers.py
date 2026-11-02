import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from conftest import eventually

from hamster_tg import handlers
from hamster_tg import scanner as scanner_module
from hamster_tg.app import create_application
from hamster_tg.retry import handle_retry
from hamster_tg.scanner import scan_directory


def context(service, args=()):
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"service": service}), args=list(args)
    )


def update():
    return SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock()),
        effective_chat=SimpleNamespace(id=7),
    )


def test_app_registers_scan_and_nonblocking_retry():
    app = create_application()
    assert any(h.callback is handlers.scan for h in app.handlers[0])
    callback = next(h for h in app.handlers[0] if h.callback is handle_retry)
    assert callback.block is False
    assert app.post_stop is handlers.post_stop


def test_scan_command_responds_without_waiting_and_duplicate_reports_progress(service):
    service.store.directory("photos")
    entered, release = threading.Event(), threading.Event()

    def paused(*args):
        entered.set()
        assert release.wait(5)
        return scan_directory(*args)

    async def scenario():
        message = update()
        try:
            with patch.object(
                scanner_module, "scan_directory", side_effect=paused
            ) as scan_mock:
                await handlers.scan(message, context(service, ["photos"]))
                assert "扫描已启动" in message.message.reply_text.call_args.args[0]
                await eventually(entered.is_set)
                first_task = service.scanner.task
                await handlers.scan(message, context(service))
                assert "检查" in message.message.reply_text.call_args.args[0]
                assert service.scanner.task is first_task
                assert scan_mock.call_count == 1
                release.set()
                await first_task
        finally:
            release.set()
            await service.close()

    asyncio.run(scenario())


def test_scan_rejects_bad_arguments(service):
    async def scenario():
        try:
            for args in (["../outside"], ["a", "b"]):
                message = update()
                await handlers.scan(message, context(service, args))
                assert "Usage" in message.message.reply_text.call_args.args[0]
                assert not service.scanner.running
        finally:
            await service.close()

    asyncio.run(scenario())


def test_media_handler_enqueues_without_downloading(service):
    message = update()
    message.message.photo = [SimpleNamespace(file_id="photo", file_unique_id="unique")]
    message.message.media_group_id = "album"
    message.message.message_id = 42

    async def scenario():
        try:
            await handlers.handle_media(message, context(service))
            with service.db.connect() as conn:
                item = conn.execute("SELECT * FROM job_items").fetchone()
                assert item["unique_id"] == "unique"
                assert item["state"] == "queued"
            service.bot.get_file.assert_not_awaited()
        finally:
            await service.close()

    asyncio.run(scenario())
