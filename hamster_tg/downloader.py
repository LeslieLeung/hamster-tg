import asyncio
import logging
import random
import tempfile
from pathlib import Path

from telegram import Bot
from telegram.error import NetworkError, RetryAfter, TimedOut

from . import state
from .config import (
    API_REQUEST_MIN_INTERVAL_SECONDS,
    DOWNLOAD_FILE_READ_TIMEOUT_SECONDS,
    DOWNLOAD_MAX_RETRIES,
    GET_FILE_READ_TIMEOUT_SECONDS,
    REQUEST_CONNECT_TIMEOUT_SECONDS,
    REQUEST_POOL_TIMEOUT_SECONDS,
    RETRY_BACKOFF_BASE_SECONDS,
)
from .storage import safe_copy


logger = logging.getLogger(__name__)


def is_retryable_download_error(exc: Exception) -> bool:
    return isinstance(exc, (TimedOut, NetworkError, RetryAfter))


def retry_delay_seconds(attempt: int, exc: Exception) -> float:
    delay = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    delay += random.uniform(0, 0.25)
    if isinstance(exc, RetryAfter):
        delay = max(delay, float(exc.retry_after))
    return delay


async def throttle_api_request() -> None:
    async with state.api_rate_lock:
        now = asyncio.get_running_loop().time()
        wait_seconds = state.next_api_request_at_monotonic - now
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        state.next_api_request_at_monotonic = (
            asyncio.get_running_loop().time() + API_REQUEST_MIN_INTERVAL_SECONDS
        )


async def download_and_save_with_retry(
    bot: Bot, file_id: str, dest_dir: Path
) -> Path | None:
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
        try:
            await throttle_api_request()
            tg_file = await bot.get_file(
                file_id,
                read_timeout=GET_FILE_READ_TIMEOUT_SECONDS,
                connect_timeout=REQUEST_CONNECT_TIMEOUT_SECONDS,
                pool_timeout=REQUEST_POOL_TIMEOUT_SECONDS,
            )
            filename = Path(tg_file.file_path or file_id).name
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir) / filename
                await throttle_api_request()
                await tg_file.download_to_drive(
                    tmp,
                    read_timeout=DOWNLOAD_FILE_READ_TIMEOUT_SECONDS,
                    connect_timeout=REQUEST_CONNECT_TIMEOUT_SECONDS,
                    pool_timeout=REQUEST_POOL_TIMEOUT_SECONDS,
                )
                return safe_copy(tmp, dest_dir)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < DOWNLOAD_MAX_RETRIES and is_retryable_download_error(exc):
                delay = retry_delay_seconds(attempt, exc)
                logger.warning(
                    "Network error for file_id=%s (%s, attempt %s/%s), retrying in %.2fs",
                    file_id,
                    type(exc).__name__,
                    attempt,
                    DOWNLOAD_MAX_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            break

    if last_error is not None:
        logger.error(
            "Download failed for file_id=%s after %s attempts: %s: %s",
            file_id,
            DOWNLOAD_MAX_RETRIES,
            type(last_error).__name__,
            last_error,
        )
    else:
        logger.error(
            "Download failed for file_id=%s after %s attempts",
            file_id,
            DOWNLOAD_MAX_RETRIES,
        )
    return None


async def download_file_ids_serially(
    bot: Bot, file_ids: list[str], dest_dir: Path
) -> tuple[int, int]:
    saved_count = 0
    async with state.download_pipeline_lock:
        for file_id in file_ids:
            saved = await download_and_save_with_retry(bot, file_id, dest_dir)
            if saved is not None:
                saved_count += 1
    failed_count = len(file_ids) - saved_count
    return saved_count, failed_count


async def download_one_file_serially(
    bot: Bot, file_id: str, dest_dir: Path
) -> Path | None:
    async with state.download_pipeline_lock:
        return await download_and_save_with_retry(bot, file_id, dest_dir)

