import asyncio
import threading
from unittest.mock import AsyncMock, patch

import pytest
from conftest import enqueue, eventually
from telegram.error import BadRequest

from hamster_tg import downloader
from hamster_tg import scanner as scanner_module
from hamster_tg.scanner import scan_directory
from hamster_tg.service import SaveService


@pytest.mark.parametrize("replacement", ["file", "symlink"])
def test_startup_isolates_broken_historical_folder(service, bot, monkeypatch, replacement):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    bad = enqueue(service.db, service.store, folder="broken")
    service.db.fail(bad["id"], "previous failure")
    folder = service.db.root / "broken"
    folder.rmdir()
    if replacement == "file":
        folder.write_text("external replacement")
    else:
        folder.symlink_to(service.db.root, target_is_directory=True)
    good = enqueue(service.db, service.store, message_id=43, folder="healthy")

    async def scenario():
        try:
            await service.start()
            await eventually(lambda: service.db.status("healthy") == (1, 0))
            assert service.db.items(bad["batch_id"])[0]["state"] == "failed"
            assert service.db.items(good["batch_id"])[0]["state"] == "saved"
            await service.switch_folder(7, "new")
            await service.enqueue(7, 44, "another")
            await eventually(lambda: service.db.status("new") == (1, 0))
            assert bot.get_file.await_count == 2
        finally:
            await service.close()

    asyncio.run(scenario())


def test_default_two_consumers_and_commands_remain_responsive(
    service, bot, monkeypatch
):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())

    async def scenario():
        active = 0
        peak = 0
        release = asyncio.Event()
        local_file = bot.get_file.return_value

        async def get_file(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await release.wait()
                return local_file
            finally:
                active -= 1

        bot.get_file.side_effect = get_file
        await service.start()
        try:
            for message in (42, 43, 44):
                await service.enqueue(7, message, str(message))
            await eventually(lambda: active == 2)
            await service.switch_folder(7, "later")
            assert service.db.current_folder(7) == "later"
            assert peak == 2
            assert service.db.status("default")[1] == 3
            release.set()
            await eventually(lambda: service.db.status("default")[1] == 0)
            assert service.db.status("default")[0] == 1
            assert service.db.status("later")[0] == 0
        finally:
            release.set()
            await service.close()

    asyncio.run(scenario())


def test_configurable_single_consumer(db, bot, monkeypatch):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())

    async def scenario():
        service = SaveService(db.root, bot, concurrency=1)
        release = asyncio.Event()
        local = bot.get_file.return_value

        async def get_file(*args, **kwargs):
            await release.wait()
            return local

        bot.get_file.side_effect = get_file
        await service.start()
        try:
            await service.enqueue(7, 42, "a")
            await service.enqueue(7, 43, "b")
            await eventually(lambda: bot.get_file.await_count == 1)
            await asyncio.sleep(0.05)
            assert bot.get_file.await_count == 1
            release.set()
            await eventually(lambda: service.db.status("default")[1] == 0)
        finally:
            release.set()
            await service.close()

    asyncio.run(scenario())


def test_album_reports_once_and_retry_survives_restart(service, bot, monkeypatch):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    monkeypatch.setattr("hamster_tg.service.MEDIA_GROUP_ACK_DELAY_SECONDS", 0.05)

    async def scenario():
        local = bot.get_file.return_value
        failing = True

        async def get_file(file_id, **kwargs):
            if file_id == "bad" and failing:
                raise PermissionError("disk denied")
            return local

        bot.get_file.side_effect = get_file
        await service.start()
        try:
            await service.switch_folder(7, "original")
            await service.enqueue(7, 42, "good", group_id="album")
            await service.switch_folder(7, "later")
            await service.enqueue(7, 43, "bad", group_id="album")
            await eventually(lambda: bot.send_message.await_count == 1)
            with service.db.connect() as conn:
                await eventually(
                    lambda: conn.execute(
                        "SELECT reported=revision FROM batches"
                    ).fetchone()[0]
                )
            sent = bot.send_message.call_args.kwargs
            assert "PermissionError: disk denied" in sent["text"]
            assert "尝试 1 次" in sent["text"]
            assert "original" in sent["text"]
            token = (
                sent["reply_markup"]
                .inline_keyboard[0][0]
                .callback_data.removeprefix("retry:")
            )
            await service.close()
            failing = False
            restarted = SaveService(service.db.root, bot)
            await restarted.start()
            try:
                assert restarted.db.current_folder(7) == "later"
                assert await restarted.retry(token, 8) == "expired"
                assert await restarted.retry(token, 7) == "queued"
                assert await restarted.retry(token, 7) == "busy"
                await eventually(lambda: bot.edit_message_text.await_count == 1)
                assert bot.send_message.await_count == 1
                assert bot.edit_message_text.call_args.kwargs["reply_markup"] is None
                assert bot.get_file.await_count == 3
                assert restarted.db.status("original") == (1, 0)
            finally:
                await restarted.close()
        finally:
            await service.close()

    asyncio.run(scenario())


def test_notify_failure_is_retried_without_redownloading(service, bot, monkeypatch):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())

    async def scenario():
        bot.send_message.side_effect = BadRequest("cannot send")
        await service.start()
        try:
            await service.enqueue(7, 42, "photo")
            await eventually(lambda: bot.send_message.await_count == 1)
            await eventually(lambda: service.db.status("default")[1] == 0)
            # Restart clears an interrupted reporting claim and retains saved jobs.
            await service.close()
            bot.send_message.side_effect = None
            with service.db.connect() as conn:
                conn.execute("UPDATE batches SET notify_after=0")
            restarted = SaveService(service.db.root, bot)
            await restarted.start()
            try:
                await eventually(lambda: bot.send_message.await_count == 2)
                assert bot.get_file.await_count == 1
                assert restarted.db.status("default") == (1, 0)
            finally:
                await restarted.close()
        finally:
            await service.close()

    asyncio.run(scenario())


def test_scan_and_simultaneous_publication_share_folder_gate(service, bot, monkeypatch):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    entered, release = threading.Event(), threading.Event()

    def paused(*args):
        entered.set()
        assert release.wait(5)
        return scan_directory(*args)

    async def scenario():
        # Claim before scanning, so these represent already in-flight downloads.
        service.store.directory("default")
        for message in (42, 43):
            service.db.enqueue(7, message, str(message), None, "default", None, 0)
        jobs = [service.db.claim(), service.db.claim()]
        tasks = []
        try:
            with patch.object(scanner_module, "scan_directory", side_effect=paused):
                service.scanner.start("default")
                await eventually(entered.is_set)
                lock = service.locks["default"]
                tasks = [
                    asyncio.create_task(
                        downloader.download_job(bot, job, service.store, lock)
                    )
                    for job in jobs
                ]
                await eventually(
                    lambda: all(service.store.temporary(job).exists() for job in jobs)
                )
                await asyncio.sleep(0.02)
                assert not list((service.db.root / "default").glob("file_*"))
                release.set()
                await service.scanner.task
                results = await asyncio.gather(*tasks)
            assert sum(result.duplicate for result in results) == 1
            assert len(list((service.db.root / "default").glob("file_*"))) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await service.close()

    asyncio.run(scenario())


def test_shutdown_inflight_download_resumes_after_restart(service, bot, monkeypatch):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())

    async def scenario():
        started = asyncio.Event()

        async def stalled(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        bot.get_file.side_effect = stalled
        await service.start()
        try:
            await service.enqueue(7, 42, "photo")
            await asyncio.wait_for(started.wait(), 5)
            await service.close()
            bot.get_file.side_effect = None
            restarted = SaveService(service.db.root, bot)
            await restarted.start()
            try:
                await eventually(lambda: restarted.db.status("default") == (1, 0))
                assert bot.get_file.await_count == 2
                with restarted.db.connect() as conn:
                    assert (
                        conn.execute("SELECT attempts FROM job_items").fetchone()[0]
                        == 2
                    )
            finally:
                await restarted.close()
        finally:
            await service.close()

    asyncio.run(scenario())


def test_startup_migrates_legacy_files_and_next_restart_skips_rehash(
    service, bot, monkeypatch
):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    folder = service.db.root / "legacy"
    folder.mkdir()
    (folder / "file_20.jpg").write_bytes(b"photo")

    async def scenario():
        await service.start()
        try:
            await service.switch_folder(7, "legacy")
            await service.enqueue(7, 42, "photo")
            await eventually(lambda: service.db.status("legacy") == (1, 0))
            assert service.db.folder("legacy")["next_index"] == 21
            assert (folder / "file_20.jpg").read_bytes() == b"photo"
            assert list(folder.glob("file_*")) == [folder / "file_20.jpg"]
            await service.close()
            restarted = SaveService(service.db.root, bot)
            with patch.object(
                scanner_module,
                "file_sha256",
                side_effect=AssertionError("should reuse index"),
            ):
                await restarted.start()
                try:
                    await restarted.scanner.task
                    assert restarted.scanner.progress.failed == 0
                    assert restarted.scanner.progress.checked == 0
                finally:
                    await restarted.close()
        finally:
            await service.close()

    asyncio.run(scenario())


def test_failed_initial_index_waits_until_successful_manual_scan(
    service, bot, monkeypatch
):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    folder = service.db.root / "legacy"
    folder.mkdir()
    (folder / "photo.jpg").write_bytes(b"photo")

    async def scenario():
        try:
            with patch.object(
                scanner_module, "file_sha256", side_effect=PermissionError("denied")
            ):
                await service.start()
                await service.switch_folder(7, "legacy")
                await service.enqueue(7, 42, "photo")
                await service.scanner.task
                assert service.db.folder("legacy")["indexed"] == 0
                assert service.db.status("legacy")[1] == 1
                bot.get_file.assert_not_awaited()
            assert service.scanner.start("legacy", chat_id=7)
            await service.scanner.task
            await eventually(lambda: service.db.status("legacy") == (1, 0))
            assert bot.get_file.await_count == 1
        finally:
            await service.close()

    asyncio.run(scenario())


def test_chat_action_failure_does_not_prevent_save(service, bot, monkeypatch):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    bot.send_chat_action.side_effect = BadRequest("unavailable")

    async def scenario():
        await service.start()
        try:
            await service.enqueue(7, 42, "photo")
            await eventually(lambda: service.db.status("default") == (1, 0))
        finally:
            await service.close()

    asyncio.run(scenario())


def test_pending_work_does_not_make_reporter_busy_loop(service, bot, monkeypatch):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())

    async def scenario():
        release = asyncio.Event()
        local = bot.get_file.return_value

        async def get_file(*args, **kwargs):
            await release.wait()
            return local

        bot.get_file.side_effect = get_file
        with patch.object(
            service.db, "claim_report", wraps=service.db.claim_report
        ) as report:
            await service.start()
            try:
                await service.enqueue(7, 42, "a")
                await service.enqueue(7, 43, "b")
                await eventually(lambda: bot.get_file.await_count == 2)
                service.wake.set()
                before = report.call_count
                await asyncio.sleep(0.1)
                assert report.call_count - before < 5
            finally:
                release.set()
                await service.close()

    asyncio.run(scenario())
