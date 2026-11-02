import hashlib
import logging
import os
import re
from pathlib import Path

from .database import Database

CHUNK_SIZE = 1024 * 1024
logger = logging.getLogger(__name__)


class ScanStopped(Exception):
    pass


def file_sha256(path: Path, stop=None) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            if stop is not None and stop.is_set():
                raise ScanStopped()
            hasher.update(chunk)
    return hasher.hexdigest()


def copy_and_hash(source: Path, target: Path) -> str:
    hasher = hashlib.sha256()
    with source.open("rb") as reader, target.open("wb") as writer:
        while chunk := reader.read(CHUNK_SIZE):
            writer.write(chunk)
            hasher.update(chunk)
    return hasher.hexdigest()


class FileStore:
    def __init__(self, database: Database):
        self.db = database
        self.root = database.root

    def directory(self, folder):
        path = self.root / folder
        if folder in ("", ".", "..") or path.parent != self.root or path.is_symlink():
            raise ValueError("Invalid or symbolic-link folder")
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True)
        self.db.register_folder(folder, empty=not existed)
        return path

    def temporary(self, job):
        return self.directory(job["folder"]) / f".saving-{job['id']}.tmp"

    def valid_duplicate(self, job, sha256=None, unique_id=None, temporary=None):
        for file in self.db.file_candidates(job["folder"], sha256, unique_id):
            path = self.root / file["path"]
            try:
                stat = path.stat()
                valid = (
                    not path.is_symlink()
                    and path.is_file()
                    and stat.st_size == file["size"]
                    and stat.st_mtime_ns == file["mtime_ns"]
                )
            except OSError:
                valid = False
            if valid:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                self.db.finish(job["id"], file, duplicate=True)
                return path
            self.db.invalidate(file["path"])
        return None

    def commit(self, job, temporary, sha256, suffix):
        duplicate = self.valid_duplicate(job, sha256=sha256, temporary=temporary)
        if duplicate:
            return duplicate, True
        stat = temporary.stat()
        while True:
            relative = self.db.reserve(
                job["id"], job["folder"], suffix, sha256, stat.st_size, stat.st_mtime_ns
            )
            target = self.root / relative
            try:
                # Same filesystem; link publishes a complete file without overwriting.
                os.link(temporary, target)
                break
            except FileExistsError:
                continue
        temporary.unlink(missing_ok=True)
        self.db.finish(
            job["id"],
            {
                "path": relative,
                "folder": job["folder"],
                "sha256": sha256,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            },
        )
        return target, False

    def recover_job(self, job):
        if job["path"] and job["sha256"]:
            path = self.root / job["path"]
            if (
                path.is_file()
                and not path.is_symlink()
                and file_sha256(path) == job["sha256"]
            ):
                stat = path.stat()
                self.db.finish(
                    job["id"],
                    {
                        "path": job["path"],
                        "folder": job["folder"],
                        "sha256": job["sha256"],
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                    },
                )
                return path
        return None

    def recover(self):
        for job in self.db.recovery_items():
            try:
                directory = self.directory(job["folder"])
                self.recover_job(job)
            except (OSError, ValueError) as exc:
                logger.warning("Cannot recover job %s: %s", job["id"], type(exc).__name__)
                self.db.fail(job["id"], f"{type(exc).__name__}: {exc}"[:1000])
                continue
            try:
                (directory / f".saving-{job['id']}.tmp").unlink(missing_ok=True)
            except OSError as exc:
                # Cleanup failure must not undo a successfully recovered save.
                logger.warning(
                    "Cannot clean temporary file for job %s: %s",
                    job["id"],
                    type(exc).__name__,
                )


def next_index(name):
    match = re.match(r"^file_(\d+)", Path(name).stem)
    return int(match.group(1)) + 1 if match else 0
