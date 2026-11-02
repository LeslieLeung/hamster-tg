import asyncio
import logging
from pathlib import Path

from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError

from .config import (
    BOT_TOKEN,
    DOWNLOAD_CONCURRENCY,
    MEDIA_GROUP_ACK_DELAY_SECONDS,
    RETRY_REQUEST_MAX_ENTRIES,
    RETRY_REQUEST_TTL_SECONDS,
)
from .database import Database
from .downloader import DownloadResult, download_job
from .retry import result_text, retry_markup
from .scanner import Scanner
from .storage import FileStore
from .threads import blocking

logger = logging.getLogger(__name__)


class SaveService:
    def __init__(self, root: Path, bot, concurrency=DOWNLOAD_CONCURRENCY):
        if concurrency < 1:
            raise ValueError("Download concurrency must be positive")
        self.db = Database(root.resolve())
        self.store = FileStore(self.db)
        self.bot, self.concurrency = bot, concurrency
        self.locks = {}
        self.wake = asyncio.Event()
        self.scanner = Scanner(self.db, self.locks, self.wake, bot)
        self.tasks = []
        self.stopping = False

    async def start(self):
        await blocking(self.db.initialize)
        await blocking(self.store.recover)
        self.scanner.start(only_unindexed=True)
        self.tasks = [
            asyncio.create_task(self._worker()) for _ in range(self.concurrency)
        ]
        self.tasks.append(asyncio.create_task(self._reporter()))

    async def enqueue(
        self, chat_id, message_id, file_id, unique_id=None, group_id=None
    ):
        folder = await blocking(self.db.current_folder, chat_id)
        await blocking(self.store.directory, folder)
        inserted = await blocking(
            self.db.enqueue,
            chat_id,
            message_id,
            file_id,
            unique_id,
            folder,
            group_id,
            MEDIA_GROUP_ACK_DELAY_SECONDS if group_id else 0,
        )
        self.wake.set()
        return inserted

    async def switch_folder(self, chat_id, folder):
        await blocking(self.store.directory, folder)
        await blocking(self.db.switch_folder, chat_id, folder)

    async def retry(self, token, chat_id):
        result = await blocking(self.db.retry, token, chat_id)
        self.wake.set()
        return result

    async def _pause(self):
        self.wake.clear()
        try:
            await asyncio.wait_for(self.wake.wait(), timeout=0.5)
        except TimeoutError:
            pass

    async def _worker(self):
        while not self.stopping:
            job = None
            try:
                self.wake.clear()
                job = await blocking(self.db.claim, tuple(self.scanner.scanning))
                if job is None:
                    if not self.scanner.running:
                        folder = await blocking(self.db.needs_index)
                        # A failed directory waits for explicit /scan rather than looping.
                        if folder:
                            self.scanner.start(folder, only_unindexed=True)
                    await self._pause()
                    continue
                try:
                    await self.bot.send_chat_action(
                        chat_id=job["chat_id"], action=ChatAction.UPLOAD_DOCUMENT
                    )
                except TelegramError:
                    logger.warning("Cannot send chat action", exc_info=True)
                lock = self.locks.setdefault(job["folder"], asyncio.Lock())
                result = await download_job(self.bot, job, self.store, lock)
                if result.error:
                    detail = f"{type(result.error).__name__}: {result.error}".replace(
                        BOT_TOKEN, "[BOT_TOKEN]"
                    )
                    await blocking(self.db.fail, job["id"], detail[:1000])
                self.wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Save worker failed")
                if job:
                    await blocking(
                        self.db.fail,
                        job["id"],
                        f"{type(exc).__name__}: {exc}".replace(
                            BOT_TOKEN, "[BOT_TOKEN]"
                        )[:1000],
                    )
                await asyncio.sleep(0.5)

    async def _reporter(self):
        while not self.stopping:
            batch = None
            try:
                batch = await blocking(
                    self.db.claim_report,
                    RETRY_REQUEST_TTL_SECONDS,
                    RETRY_REQUEST_MAX_ENTRIES,
                )
                if batch is None:
                    await self._pause()
                    continue
                await self.report(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Cannot report saved batch")
                if batch:
                    await blocking(self.db.report_failed, batch["id"])
                await asyncio.sleep(0.5)

    async def report(self, batch):
        items = batch["items"]
        results = [
            DownloadResult(
                item["file_id"],
                item["attempts"],
                self.db.root / item["path"]
                if item["state"] in ("saved", "duplicate")
                else None,
                item["error"],
                bool(item["duplicate"]),
            )
            for item in items
        ]
        text = result_text(results, batch["folder"])
        if not batch["token"] and any(result.error for result in results):
            text += "\n重试任务已满，请重新发送失败文件。"
        markup = retry_markup(batch["token"]) if batch["token"] else None
        message_id = batch["summary_id"]
        if message_id is not None:
            try:
                await self.bot.edit_message_text(
                    chat_id=batch["chat_id"],
                    message_id=message_id,
                    text=text,
                    reply_markup=markup,
                )
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    message_id = None
            except TelegramError:
                message_id = None
        if message_id is None:
            message = await self.bot.send_message(
                chat_id=batch["chat_id"],
                text=text,
                reply_markup=markup,
                reply_to_message_id=batch["reply_to"],
                allow_sending_without_reply=True,
            )
            message_id = message.message_id
        await blocking(self.db.report_done, batch, message_id)

    async def close(self):
        self.stopping = True
        self.wake.set()
        # Stop the scanner first so in-flight saves cannot be stranded at its gate.
        await self.scanner.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
