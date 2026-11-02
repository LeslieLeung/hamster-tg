import asyncio
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from .retry import truncate_text
from .storage import ScanStopped, file_sha256, next_index
from .threads import blocking

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanProgress:
    scan_id: str
    sequence: int = 0
    folder: str = "准备中"
    checked: int = 0
    reused: int = 0
    updated: int = 0
    missing: int = 0
    failed: int = 0
    skipped: int = 0
    phase: str = "扫描中"
    errors: tuple[str, ...] = ()

    def text(self):
        text = (
            f"{self.phase}：{self.folder}\n检查 {self.checked}，复用 {self.reused}，"
            f"更新 {self.updated}，缺失 {self.missing}，跳过 {self.skipped}，失败 {self.failed}"
        )
        if self.errors:
            text += "\n" + "\n".join(self.errors[:5])
        return truncate_text(text, 3500)


def scan_directory(db, folder, progress, stop, publish):
    """Runs in the scan thread while the event loop holds the folder gate."""
    path = db.root / folder
    progress = replace(progress, folder=folder)
    db.register_folder(folder)
    errors_before = progress.failed
    buffered = []
    highest = 0
    last_publish = 0.0

    with db.connect() as conn:
        conn.execute("UPDATE folders SET scan_state='running' WHERE name=?", (folder,))

        def flush():
            if not buffered:
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(
                    "INSERT INTO files(path,folder,sha256,size,mtime_ns,state,seen_scan) VALUES(?,?,?,?,?,'ready',?) "
                    "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256,size=excluded.size,mtime_ns=excluded.mtime_ns,state='ready',seen_scan=excluded.seen_scan",
                    buffered,
                )
                conn.execute("COMMIT")
                buffered.clear()
            except BaseException:
                conn.execute("ROLLBACK")
                raise

        interrupted = False
        enumerated = False
        try:
            if path.is_symlink() or not path.is_dir():
                raise OSError(f"目录不存在或是符号链接：{folder}")
            with os.scandir(path) as entries:
                for entry in entries:
                    if stop.is_set():
                        raise ScanStopped()
                    if entry.name.startswith(".saving-") or entry.is_symlink():
                        progress = replace(progress, skipped=progress.skipped + 1)
                        continue
                    relative = f"{folder}/{entry.name}"
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            progress = replace(progress, skipped=progress.skipped + 1)
                            continue
                        highest = max(highest, next_index(entry.name))
                        before = Path(entry.path).stat()
                        row = conn.execute(
                            "SELECT * FROM files WHERE path=?", (relative,)
                        ).fetchone()
                        reuse = (
                            row
                            and row["state"] == "ready"
                            and row["sha256"]
                            and row["size"] == before.st_size
                            and row["mtime_ns"] == before.st_mtime_ns
                        )
                        sha = (
                            row["sha256"]
                            if reuse
                            else file_sha256(Path(entry.path), stop)
                        )
                        after = Path(entry.path).stat()
                        if (
                            before.st_dev,
                            before.st_ino,
                            before.st_size,
                            before.st_mtime_ns,
                            before.st_ctime_ns,
                        ) != (
                            after.st_dev,
                            after.st_ino,
                            after.st_size,
                            after.st_mtime_ns,
                            after.st_ctime_ns,
                        ):
                            raise OSError("读取期间文件发生变化")
                        buffered.append(
                            (
                                relative,
                                folder,
                                sha,
                                after.st_size,
                                after.st_mtime_ns,
                                progress.scan_id,
                            )
                        )
                        progress = replace(
                            progress,
                            checked=progress.checked + 1,
                            reused=progress.reused + int(bool(reuse)),
                            updated=progress.updated + int(not reuse),
                        )
                        if len(buffered) >= 100:
                            flush()
                    except OSError as exc:
                        db.invalidate(relative)
                        progress = replace(
                            progress,
                            checked=progress.checked + 1,
                            failed=progress.failed + 1,
                            errors=(*progress.errors, f"{relative}: {exc}")[:10],
                        )
                    if time.monotonic() - last_publish >= 1:
                        publish(progress)
                        last_publish = time.monotonic()
            enumerated = True
        except ScanStopped:
            interrupted = True
        except OSError as exc:
            progress = replace(
                progress,
                failed=progress.failed + 1,
                errors=(*progress.errors, f"{folder}: {exc}")[:10],
            )
        finally:
            flush()
            complete = (
                enumerated
                and not interrupted
                and not stop.is_set()
                and progress.failed == errors_before
            )
            conn.execute("BEGIN IMMEDIATE")
            if complete:
                missing = conn.execute(
                    "UPDATE files SET state='missing' WHERE folder=? AND state!='missing' AND (seen_scan IS NULL OR seen_scan!=?)",
                    (folder, progress.scan_id),
                ).rowcount
                progress = replace(progress, missing=progress.missing + missing)
            conn.execute(
                "UPDATE folders SET indexed=MAX(indexed,?),next_index=MAX(next_index,?),scan_state=? WHERE name=?",
                (
                    int(complete),
                    highest,
                    "complete"
                    if complete
                    else "interrupted"
                    if interrupted or stop.is_set()
                    else "failed",
                    folder,
                ),
            )
            conn.execute("COMMIT")
    return progress


class Scanner:
    def __init__(self, db, locks, wake, bot):
        self.db, self.locks, self.wake, self.bot = db, locks, wake, bot
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hamster-scan"
        )
        self.task = None
        self.progress = None
        self.scanning = set()
        self.stop = threading.Event()
        self.closed = False

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def start(self, folder=None, chat_id=None, only_unindexed=False):
        if self.closed or self.running:
            return False
        self.stop = threading.Event()
        self.progress = ScanProgress(uuid.uuid4().hex)
        self.task = asyncio.create_task(self._run(folder, chat_id, only_unindexed))
        return True

    def _receive(self, progress):
        if (
            self.running
            and self.progress.phase == "扫描中"
            and progress.scan_id == self.progress.scan_id
            and progress.sequence > self.progress.sequence
        ):
            self.progress = progress

    def _directories(self, folder, only_unindexed):
        if folder:
            path = self.db.root / folder
            if path.parent != self.db.root or path.is_symlink() or not path.is_dir():
                raise ValueError("指定目录不存在或是符号链接。")
            names = [folder]
        else:
            names = sorted(
                p.name
                for p in self.db.root.iterdir()
                if not p.is_symlink()
                and p.is_dir()
                and not p.name.startswith(".saving-")
            )
        if only_unindexed:
            names = [n for n in names if not (self.db.folder(n) or {}).get("indexed")]
        return names

    async def _run(self, folder, chat_id, only_unindexed):
        loop = asyncio.get_running_loop()
        progress = self.progress
        sequence = 0

        def publish(snapshot):
            nonlocal sequence
            sequence += 1
            loop.call_soon_threadsafe(
                self._receive, replace(snapshot, sequence=sequence)
            )

        try:
            folders = await blocking(
                self._directories,
                folder,
                only_unindexed,
                executor=self.executor,
                on_cancel=self.stop.set,
            )
            for name in folders:
                if self.stop.is_set():
                    break
                self.progress = replace(self.progress, folder=name)
                self.scanning.add(name)
                try:
                    async with self.locks.setdefault(name, asyncio.Lock()):
                        progress = await blocking(
                            scan_directory,
                            self.db,
                            name,
                            progress,
                            self.stop,
                            publish,
                            executor=self.executor,
                            on_cancel=self.stop.set,
                        )
                finally:
                    self.scanning.discard(name)
                    self.wake.set()
            self.progress = replace(
                progress,
                phase="扫描已中断"
                if self.stop.is_set()
                else "扫描完成（有失败）"
                if progress.failed
                else "扫描完成",
            )
        except asyncio.CancelledError:
            self.progress = replace(self.progress, phase="扫描已中断")
            raise
        except Exception as exc:
            logger.exception("Scan failed")
            self.progress = replace(
                self.progress,
                phase="扫描失败",
                failed=self.progress.failed + 1,
                errors=(*self.progress.errors, str(exc))[:10],
            )
        if chat_id is not None:
            try:
                await self.bot.send_message(chat_id=chat_id, text=self.progress.text())
            except Exception:
                logger.exception("Cannot send scan summary")

    async def close(self):
        self.closed = True
        self.stop.set()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
        await asyncio.to_thread(self.executor.shutdown, wait=True)
