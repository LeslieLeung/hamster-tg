import asyncio
import threading
import uuid
from unittest.mock import patch

import pytest
from conftest import eventually

from hamster_tg import scanner as scanner_module
from hamster_tg.scanner import Scanner, ScanProgress, scan_directory
from hamster_tg.storage import ScanStopped, file_sha256


def scan(db, folder="legacy", stop=None):
    return scan_directory(
        db,
        folder,
        ScanProgress(uuid.uuid4().hex),
        stop or threading.Event(),
        lambda p: None,
    )


def rows(db):
    with db.connect() as conn:
        return {r["path"]: dict(r) for r in conn.execute("SELECT * FROM files")}


def test_repeat_scan_preserves_legacy_duplicates_and_reuses_hashes(db):
    folder = db.root / "legacy"
    folder.mkdir()
    for name in ("file_12.jpg", "other.jpg"):
        (folder / name).write_bytes(b"same")
    first = scan(db)
    assert first.updated == 2
    assert len(rows(db)) == 2
    assert db.folder("legacy")["next_index"] == 13
    with patch.object(
        scanner_module, "file_sha256", side_effect=AssertionError("unnecessary hash")
    ):
        second = scan(db)
    assert second.reused == 2
    assert second.updated == 0
    assert len(list(folder.iterdir())) == 2


def test_rescan_add_modify_delete_restore_and_keep_counter(db):
    folder = db.root / "legacy"
    folder.mkdir()
    file = folder / "file_9.jpg"
    file.write_bytes(b"old")
    scan(db)
    old_hash = rows(db)["legacy/file_9.jpg"]["sha256"]
    file.write_bytes(b"changed content")
    assert scan(db).updated == 1
    assert rows(db)["legacy/file_9.jpg"]["sha256"] != old_hash
    file.unlink()
    assert scan(db).missing == 1
    assert rows(db)["legacy/file_9.jpg"]["state"] == "missing"
    assert db.folder("legacy")["next_index"] == 10
    file.write_bytes(b"restored")
    assert scan(db).updated == 1
    assert rows(db)["legacy/file_9.jpg"]["state"] == "ready"


def test_failed_scan_invalidates_changed_hash_without_missing_cleanup(db):
    folder = db.root / "legacy"
    folder.mkdir()
    changed, deleted = folder / "changed", folder / "deleted"
    changed.write_bytes(b"old")
    deleted.write_bytes(b"keep record if scan incomplete")
    scan(db)
    changed.write_bytes(b"new content")
    deleted.unlink()
    with patch.object(
        scanner_module, "file_sha256", side_effect=PermissionError("denied")
    ):
        result = scan(db)
    assert result.failed == 1
    assert rows(db)["legacy/changed"]["sha256"] is None
    assert rows(db)["legacy/deleted"]["state"] == "ready"
    assert db.folder("legacy")["scan_state"] == "failed"
    assert scan(db).missing == 1


def test_file_changed_during_hash_is_not_indexed(db):
    folder = db.root / "legacy"
    folder.mkdir()
    file = folder / "photo"
    file.write_bytes(b"before")

    def changing(path, stop):
        digest = file_sha256(path)
        path.write_bytes(b"modified while reading")
        return digest

    with patch.object(scanner_module, "file_sha256", side_effect=changing):
        result = scan(db)
    assert result.failed == 1
    assert rows(db) == {}
    assert db.folder("legacy")["indexed"] == 0


def test_interruption_commits_completed_files_and_resumes(db):
    folder = db.root / "legacy"
    folder.mkdir()
    for i in range(3):
        (folder / str(i)).write_bytes(str(i).encode())
    calls = []

    def interrupted(path, stop):
        calls.append(path)
        if len(calls) == 2:
            raise ScanStopped()
        return file_sha256(path)

    with patch.object(scanner_module, "file_sha256", side_effect=interrupted):
        result = scan(db)
    assert result.updated == 1
    assert len(rows(db)) == 1
    assert db.folder("legacy")["indexed"] == 0
    result = scan(db)
    assert result.reused == 1
    assert result.updated == 2
    assert db.folder("legacy")["indexed"] == 1


def test_skips_temporary_links_and_nested_directories_and_indexes_empty(db, tmp_path):
    folder = db.root / "legacy"
    folder.mkdir()
    (folder / ".saving-1.tmp").write_bytes(b"partial")
    (folder / "nested").mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (folder / "link").symlink_to(outside)
    result = scan(db)
    assert result.skipped == 3
    assert rows(db) == {}
    assert db.folder("legacy")["indexed"] == 1


def test_unexpected_thread_error_does_not_mark_scan_complete(db):
    (db.root / "legacy").mkdir()
    (db.root / "legacy/photo").write_bytes(b"photo")
    with (
        patch.object(
            scanner_module, "file_sha256", side_effect=RuntimeError("unexpected")
        ),
        pytest.raises(RuntimeError),
    ):
        scan(db)
    assert db.folder("legacy")["indexed"] == 0
    assert db.folder("legacy")["scan_state"] == "failed"


def test_cancellation_keeps_gate_and_single_scan_until_thread_exits(db, bot):
    (db.root / "legacy").mkdir()
    entered, release = threading.Event(), threading.Event()
    actual_scan = scan_directory

    def paused(*args):
        entered.set()
        assert release.wait(5)
        return actual_scan(*args)

    async def scenario():
        scanner = Scanner(db, {}, asyncio.Event(), bot)
        try:
            with patch.object(scanner_module, "scan_directory", side_effect=paused):
                assert scanner.start("legacy")
                await eventually(entered.is_set)
                scanner.task.cancel()
                await asyncio.sleep(0)
                assert scanner.running
                assert scanner.locks["legacy"].locked()
                assert "legacy" in scanner.scanning
                assert not scanner.start("legacy")
                # The loop keeps running while the real thread is still busy.
                await asyncio.sleep(0.01)
                assert scanner.running
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await scanner.task
            assert not scanner.locks["legacy"].locked()
            assert not scanner.scanning
        finally:
            release.set()
            await scanner.close()

    asyncio.run(scenario())


def test_late_progress_cannot_replace_completed_or_new_scan(db, bot):
    async def scenario():
        scanner = Scanner(db, {}, asyncio.Event(), bot)
        try:
            scanner.start()
            old_id = scanner.progress.scan_id
            await scanner.task
            completed = scanner.progress
            scanner._receive(ScanProgress(old_id, checked=999))
            assert scanner.progress == completed
            scanner.start()
            scanner._receive(ScanProgress(old_id, checked=999))
            assert scanner.progress.checked == 0
            await scanner.task
        finally:
            await scanner.close()

    asyncio.run(scenario())


def test_missing_requested_directory_is_not_created(db, bot):
    async def scenario():
        scanner = Scanner(db, {}, asyncio.Event(), bot)
        try:
            scanner.start("missing", chat_id=7)
            await scanner.task
            assert scanner.progress.failed == 1
            assert not (db.root / "missing").exists()
            assert "不存在" in bot.send_message.call_args.kwargs["text"]
        finally:
            await scanner.close()

    asyncio.run(scenario())


def test_out_of_order_progress_is_ignored_within_same_scan(db, bot):
    async def scenario():
        scanner = Scanner(db, {}, asyncio.Event(), bot)
        try:
            scanner.start()
            scan_id = scanner.progress.scan_id
            scanner._receive(
                ScanProgress(scan_id, sequence=2, folder="newer", checked=3)
            )
            scanner._receive(
                ScanProgress(scan_id, sequence=1, folder="older", checked=999)
            )
            assert scanner.progress.folder == "newer"
            assert scanner.progress.checked == 3
            await scanner.task
        finally:
            await scanner.close()

    asyncio.run(scenario())
