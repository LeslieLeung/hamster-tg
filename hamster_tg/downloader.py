import asyncio
import logging
import random
from dataclasses import dataclass
from pathlib import Path

from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

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
from .storage import copy_and_hash, file_sha256
from .threads import blocking

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadResult:
    file_id: str
    attempts: int
    path: Path | None = None
    error: Exception | str | None = None
    duplicate: bool = False


def is_retryable_download_error(exc):
    return isinstance(exc, (TimedOut, NetworkError, RetryAfter)) and not isinstance(
        exc, BadRequest
    )


def retry_delay_seconds(attempt, exc):
    delay = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
    if isinstance(exc, RetryAfter):
        retry_after = exc.retry_after
        delay = max(
            delay,
            retry_after.total_seconds()
            if hasattr(retry_after, "total_seconds")
            else float(retry_after),
        )
    return delay


async def throttle_api_request():
    async with state.api_rate_lock:
        loop = asyncio.get_running_loop()
        delay = state.next_api_request_at_monotonic - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        state.next_api_request_at_monotonic = (
            loop.time() + API_REQUEST_MIN_INTERVAL_SECONDS
        )


def local_source(file_path):
    path = Path(file_path) if file_path else None
    return path if path and path.is_absolute() and path.is_file() else None


async def download_job(bot, job, store, lock):
    temporary = None
    attempts = job["attempts"]
    try:
        if job.get("path") or job.get("unique_id"):
            async with lock:
                recovered = await blocking(store.recover_job, job)
                if recovered:
                    return DownloadResult(job["file_id"], attempts, recovered)
                if job.get("unique_id"):
                    existing = await blocking(
                        store.valid_duplicate, job, None, job["unique_id"]
                    )
                    if existing:
                        return DownloadResult(
                            job["file_id"], attempts, existing, duplicate=True
                        )
        temporary = await blocking(store.temporary, job)
        for attempt in range(attempts + 1, DOWNLOAD_MAX_RETRIES + 1):
            attempts = attempt
            await blocking(store.db.attempt, job["id"], attempt)
            try:
                await throttle_api_request()
                tg_file = await bot.get_file(
                    job["file_id"],
                    read_timeout=GET_FILE_READ_TIMEOUT_SECONDS,
                    connect_timeout=REQUEST_CONNECT_TIMEOUT_SECONDS,
                    pool_timeout=REQUEST_POOL_TIMEOUT_SECONDS,
                )
                source = await blocking(local_source, tg_file.file_path)
                if source:
                    sha = await blocking(copy_and_hash, source, temporary)
                else:
                    await throttle_api_request()
                    await tg_file.download_to_drive(
                        temporary,
                        read_timeout=DOWNLOAD_FILE_READ_TIMEOUT_SECONDS,
                        connect_timeout=REQUEST_CONNECT_TIMEOUT_SECONDS,
                        pool_timeout=REQUEST_POOL_TIMEOUT_SECONDS,
                    )
                    sha = await blocking(file_sha256, temporary)
                suffix = Path(tg_file.file_path or "").suffix
                if len(suffix) > 16 or (suffix and not suffix[1:].isalnum()):
                    suffix = ""
                async with lock:
                    path, duplicate = await blocking(
                        store.commit, job, temporary, sha, suffix
                    )
                return DownloadResult(
                    job["file_id"], attempt, path, duplicate=duplicate
                )
            except Exception as exc:
                if attempt < DOWNLOAD_MAX_RETRIES and is_retryable_download_error(exc):
                    logger.warning(
                        "Retrying job %s (%s/%s): %s",
                        job["id"],
                        attempt,
                        DOWNLOAD_MAX_RETRIES,
                        type(exc).__name__,
                    )
                    await asyncio.sleep(retry_delay_seconds(attempt, exc))
                    continue
                raise
        raise RuntimeError("自动重试次数已用尽，请点击重试失败文件。")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - isolate and report each file's failure.
        logger.warning("Job %s failed: %s", job["id"], type(exc).__name__)
        return DownloadResult(job["file_id"], attempts, error=exc)
    finally:
        if temporary is not None:
            await blocking(lambda: temporary.unlink(missing_ok=True))
