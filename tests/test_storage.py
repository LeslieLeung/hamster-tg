from unittest.mock import patch

import pytest
from conftest import enqueue

from hamster_tg.storage import FileStore, copy_and_hash


def prepared(store, job, content=b"image"):
    path = store.temporary(job)
    path.write_bytes(content)
    from hamster_tg.storage import file_sha256

    return path, file_sha256(path)


def test_hash_index_survives_database_reopen(db, store):
    job = enqueue(db, store)
    temporary, sha = prepared(store, job)
    first, duplicate = store.commit(job, temporary, sha, ".jpg")
    assert not duplicate
    assert first.name == "file_0.jpg"
    db.initialize()
    new_store = FileStore(db)
    job = enqueue(db, new_store, message_id=43)
    temporary, sha = prepared(new_store, job)
    second, duplicate = new_store.commit(job, temporary, sha, ".jpg")
    assert duplicate
    assert second == first
    assert db.status("default") == (1, 0)


def test_copy_failure_does_not_publish_partial_file(db, store, tmp_path):
    job = enqueue(db, store)
    temporary, sha = prepared(store, job)
    with (
        patch("hamster_tg.storage.os.link", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        store.commit(job, temporary, sha, ".jpg")
    assert list((db.root / "default").glob("file_*")) == []
    assert db.status("default")[0] == 0
    store.recover()
    assert not temporary.exists()


def test_recovers_published_file_after_database_commit_failure(db, store):
    job = enqueue(db, store)
    temporary, sha = prepared(store, job)
    with (
        patch.object(db, "finish", side_effect=OSError("interrupted")),
        pytest.raises(OSError),
    ):
        store.commit(job, temporary, sha, ".jpg")
    published = db.root / "default/file_0.jpg"
    assert published.read_bytes() == b"image"
    db.initialize()
    store.recover()
    assert db.items(job["batch_id"])[0]["state"] == "saved"
    assert db.status("default") == (1, 0)
    assert not temporary.exists()


def test_external_filename_collision_never_overwrites(db, store):
    job = enqueue(db, store)
    existing = db.root / "default/file_0.jpg"
    existing.write_bytes(b"keep")
    temporary, sha = prepared(store, job)
    path, _ = store.commit(job, temporary, sha, ".jpg")
    assert path.name == "file_1.jpg"
    assert existing.read_bytes() == b"keep"


def test_rejects_symbolic_link_folder(db, store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (db.root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        store.directory("linked")


def test_copy_hashes_streamed_content(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"data" * 300000)
    from hamster_tg.storage import file_sha256

    assert copy_and_hash(source, target) == file_sha256(source)
    assert target.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("folder", ["", ".", "..", "../outside", "nested/path"])
def test_rejects_paths_outside_direct_child_folders(store, folder):
    with pytest.raises(ValueError):
        store.directory(folder)


def test_recovery_updates_previously_reported_failure(db, store):
    job = enqueue(db, store)
    temporary, sha = prepared(store, job)
    with (
        patch.object(db, "finish", side_effect=OSError("commit failed")),
        pytest.raises(OSError),
    ):
        store.commit(job, temporary, sha, ".jpg")
    db.fail(job["id"], "commit failed")
    failure = db.claim_report(86400, 1000)
    db.report_done(failure, 900)
    db.initialize()
    store.recover()
    corrected = db.claim_report(86400, 1000)
    assert corrected is not None
    assert corrected["summary_id"] == 900
    assert corrected["token"] is None
    assert corrected["items"][0]["state"] == "saved"


def test_recovery_hash_failure_does_not_block_other_jobs(db, store):
    jobs = [enqueue(db, store, message_id=i) for i in (42, 43)]
    for job in jobs:
        temporary, sha = prepared(store, job)
        with patch.object(db, "finish", side_effect=OSError("interrupted")):
            with pytest.raises(OSError):
                store.commit(job, temporary, sha, ".jpg")

    from hamster_tg.storage import file_sha256

    def hash_file(path):
        if path.name == "file_0.jpg":
            raise PermissionError("cannot read published file")
        return file_sha256(path)

    with patch("hamster_tg.storage.file_sha256", side_effect=hash_file):
        store.recover()
    first = db.items(jobs[0]["batch_id"])[0]
    assert first["state"] == "failed"
    assert "PermissionError" in first["error"]
    assert db.items(jobs[1]["batch_id"])[0]["state"] == "saved"


def test_recovery_cleanup_failure_preserves_saved_state(db, store, caplog):
    job = enqueue(db, store)
    temporary, sha = prepared(store, job)
    with patch.object(db, "finish", side_effect=OSError("interrupted")):
        with pytest.raises(OSError):
            store.commit(job, temporary, sha, ".jpg")
    # An unexpected directory at the temporary path makes unlink fail.
    temporary.mkdir()
    store.recover()
    assert db.items(job["batch_id"])[0]["state"] == "saved"
    assert "Cannot clean temporary file" in caplog.text
