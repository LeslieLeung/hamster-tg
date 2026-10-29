import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import TimedOut

from hamster_tg import downloader


def test_network_failure_reports_actual_attempts(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    monkeypatch.setattr(downloader.asyncio, "sleep", AsyncMock())
    bot = SimpleNamespace(get_file=AsyncMock(side_effect=TimedOut("server stalled")))

    result = asyncio.run(downloader.download_and_save_with_retry(bot, "file", tmp_path))

    assert result.path is None
    assert isinstance(result.error, TimedOut)
    assert result.attempts == 6
    assert bot.get_file.await_count == 6
    assert downloader.asyncio.sleep.await_count == 5


def test_directory_failure_is_reported_without_network_request(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    bot = SimpleNamespace(get_file=AsyncMock())

    result = asyncio.run(
        downloader.download_and_save_with_retry(bot, "file", blocker / "folder")
    )

    assert result.path is None
    assert isinstance(result.error, OSError)
    assert result.attempts == 1
    bot.get_file.assert_not_awaited()


def test_batch_continues_after_failure_and_saves_success(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())

    async def download(path, **kwargs):
        path.write_bytes(b"media content")

    tg_file = SimpleNamespace(
        file_path="video.mp4", download_to_drive=AsyncMock(side_effect=download)
    )
    bot = SimpleNamespace(
        get_file=AsyncMock(side_effect=[PermissionError("denied"), tg_file])
    )

    results = asyncio.run(
        downloader.download_file_ids_serially(bot, ["bad", "good"], tmp_path)
    )

    assert results[0].file_id == "bad"
    assert isinstance(results[0].error, PermissionError)
    assert results[1].path.read_bytes() == b"media content"
    assert results[1].error is None


def test_cancellation_is_not_reported_as_a_save_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "throttle_api_request", AsyncMock())
    bot = SimpleNamespace(get_file=AsyncMock(side_effect=asyncio.CancelledError()))
    import pytest

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(downloader.download_and_save_with_retry(bot, "file", tmp_path))
