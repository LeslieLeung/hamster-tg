import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from conftest import enqueue, eventually
from telegram.error import BadRequest, TimedOut

from hamster_tg import downloader
from hamster_tg.downloader import download_job


def test_network_failures_report_actual_attempts(db, store, bot, monkeypatch):
    job = enqueue(db, store)
    bot.get_file.side_effect = TimedOut("stalled")
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    monkeypatch.setattr(downloader, "retry_delay_seconds", lambda *_: 0)
    result = asyncio.run(download_job(bot, job, store, asyncio.Lock()))
    assert result.attempts == 6
    assert isinstance(result.error, TimedOut)
    assert bot.get_file.await_count == 6
    assert not store.temporary(job).exists()


def test_bad_request_and_local_errors_are_not_retried(db, store, bot, monkeypatch):
    job = enqueue(db, store)
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    bot.get_file.side_effect = BadRequest("invalid file")
    result = asyncio.run(download_job(bot, job, store, asyncio.Lock()))
    assert result.attempts == 1
    assert bot.get_file.await_count == 1
    bot.get_file.side_effect = None
    with patch(
        "hamster_tg.storage.os.link", side_effect=PermissionError("disk denied")
    ):
        result = asyncio.run(download_job(bot, job, store, asyncio.Lock()))
    assert isinstance(result.error, PermissionError)
    assert result.attempts == 1
    assert not list((db.root / "default").glob("file_*"))


def test_resume_preserves_attempt_budget(db, store, bot, monkeypatch):
    job = enqueue(db, store)
    job["attempts"] = 5
    bot.get_file.side_effect = TimedOut("stalled")
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    result = asyncio.run(download_job(bot, job, store, asyncio.Lock()))
    assert result.attempts == 6
    assert bot.get_file.await_count == 1


def test_cancellation_propagates_and_removes_temp_file(db, store, bot, monkeypatch):
    job = enqueue(db, store)
    bot.get_file.side_effect = asyncio.CancelledError()
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(download_job(bot, job, store, asyncio.Lock()))
    assert not store.temporary(job).exists()


def test_telegram_identity_skips_download_only_for_matching_current_content(
    db, store, bot, monkeypatch
):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())

    async def scenario():
        first = enqueue(db, store, unique_id="identity")
        saved = await download_job(bot, first, store, asyncio.Lock())
        second = enqueue(db, store, message_id=43, unique_id="identity")
        reused = await download_job(bot, second, store, asyncio.Lock())
        assert reused.duplicate
        assert bot.get_file.await_count == 1
        saved.path.write_bytes(b"external replacement")
        from test_scanner import scan

        scan(db, "default")
        third = enqueue(db, store, message_id=44, unique_id="identity")
        result = await download_job(bot, third, store, asyncio.Lock())
        assert not result.duplicate
        assert result.path != saved.path
        assert bot.get_file.await_count == 2

    asyncio.run(scenario())


def test_cancelled_save_keeps_gate_until_commit_thread_finishes(
    db, store, bot, monkeypatch
):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    job = enqueue(db, store)
    entered, release = threading.Event(), threading.Event()
    real_commit = store.commit

    def paused(*args):
        entered.set()
        assert release.wait(5)
        return real_commit(*args)

    async def scenario():
        lock = asyncio.Lock()
        task = None
        try:
            with patch.object(store, "commit", side_effect=paused):
                task = asyncio.create_task(download_job(bot, job, store, lock))
                await eventually(entered.is_set)
                task.cancel()
                await asyncio.sleep(0.01)
                assert lock.locked()
                assert not task.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert not lock.locked()
            assert db.items(job["batch_id"])[0]["state"] == "saved"
            assert not store.temporary(job).exists()
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_retry_recovers_published_file_without_another_download(
    db, store, bot, monkeypatch
):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    job = enqueue(db, store)

    async def scenario():
        with patch.object(db, "finish", side_effect=OSError("commit failed")):
            first = await download_job(bot, job, store, asyncio.Lock())
        assert isinstance(first.error, OSError)
        db.fail(job["id"], "commit failed")
        batch = db.claim_report(86400, 1000)
        db.report_done(batch, 900)
        assert db.retry(batch["token"], 7) == "queued"
        retry_job = db.claim()
        result = await download_job(bot, retry_job, store, asyncio.Lock())
        assert result.path.name == "file_0.jpg"
        assert bot.get_file.await_count == 1
        assert len(list((db.root / "default").glob("file_*"))) == 1

    asyncio.run(scenario())


def test_http_download_fallback_saves_and_hashes_file(db, store, bot, monkeypatch):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    job = enqueue(db, store)

    async def download(path, **kwargs):
        path.write_bytes(b"remote photo")

    telegram_file = SimpleNamespace(
        file_path="https://example.test/photo.jpg",
        download_to_drive=AsyncMock(side_effect=download),
    )
    bot.get_file.return_value = telegram_file
    result = asyncio.run(download_job(bot, job, store, asyncio.Lock()))
    assert result.path.read_bytes() == b"remote photo"
    telegram_file.download_to_drive.assert_awaited_once()
    assert not store.temporary(job).exists()


def test_partial_copy_is_removed_without_publishing(db, store, bot, monkeypatch):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    job = enqueue(db, store)

    def partial(source, target):
        target.write_bytes(b"partial")
        raise OSError("disk full")

    with patch.object(downloader, "copy_and_hash", side_effect=partial):
        result = asyncio.run(download_job(bot, job, store, asyncio.Lock()))
    assert isinstance(result.error, OSError)
    assert result.attempts == 1
    assert list((db.root / "default").iterdir()) == []
