from concurrent.futures import ThreadPoolExecutor
from time import time

from conftest import enqueue


def test_settings_and_tasks_survive_restart(db, store):
    db.switch_folder(7, "photos")
    job = enqueue(db, store, folder="photos")
    db.attempt(job["id"], 2)
    db.initialize()
    assert db.current_folder(7) == "photos"
    recovered = db.claim()
    assert recovered["id"] == job["id"]
    assert recovered["attempts"] == 2


def test_atomic_claim_and_message_idempotency(db, store):
    store.directory("default")
    assert db.enqueue(7, 42, "a", None, "default", None, 0)
    assert not db.enqueue(7, 42, "a", None, "default", None, 0)
    assert db.enqueue(7, 43, "b", None, "default", None, 0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: db.claim(), range(4)))
    ids = [job["id"] for job in claims if job]
    assert len(set(ids)) == len(ids) == 2


def test_album_freezes_first_folder_and_deduplicates_updates(db, store):
    store.directory("original")
    store.directory("later")
    db.enqueue(7, 42, "a", None, "original", "album", 0)
    db.enqueue(7, 43, "b", None, "later", "album", 0)
    assert not db.enqueue(7, 43, "b", None, "later", "album", 0)
    first, second = db.claim(), db.claim()
    assert first["folder"] == second["folder"] == "original"
    assert first["batch_id"] == second["batch_id"]
    with db.connect() as conn:
        assert conn.execute("SELECT revision FROM batches").fetchone()[0] == 2


def test_persisted_retry_is_scoped_atomic_and_expires(db, store):
    job = enqueue(db, store)
    db.fail(job["id"], "PermissionError: denied")
    batch = db.claim_report(86400, 1000)
    db.report_done(batch, 900)
    token = batch["token"]
    db.initialize()
    assert db.retry(token, 8) == "expired"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: db.retry(token, 7), range(2)))
    assert sorted(results) == ["busy", "queued"]
    retried = db.claim()
    assert retried["id"] == job["id"]
    db.fail(job["id"], "again")
    with db.connect() as conn:
        conn.execute("UPDATE batches SET expires_at=?", (time() - 1,))
    assert db.retry(token, 7) == "expired"


def test_only_failed_items_are_requeued(db, store):
    store.directory("default")
    for message in (42, 43):
        db.enqueue(7, message, str(message), None, "default", "album", 0)
    good, bad = db.claim(), db.claim()
    with db.connect() as conn:
        conn.execute("UPDATE job_items SET state='saved' WHERE id=?", (good["id"],))
    db.fail(bad["id"], "failed")
    batch = db.claim_report(86400, 1000)
    db.report_done(batch, 900)
    assert db.retry(batch["token"], 7) == "queued"
    assert db.claim()["id"] == bad["id"]
    assert db.claim() is None


def test_retry_record_limit_does_not_remove_pending_jobs(db, store):
    tokens = []
    for message in (42, 43, 44):
        job = enqueue(db, store, message_id=message)
        db.fail(job["id"], "failed")
        batch = db.claim_report(86400, 1)
        db.report_done(batch, 900)
        tokens.append(batch["token"])
    assert db.retry(tokens[0], 7) == "expired"
    assert db.retry(tokens[-1], 7) == "queued"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM job_items").fetchone()[0] == 3


def test_retry_limit_preserves_active_button_and_refuses_new_one(db, store):
    job = enqueue(db, store)
    db.fail(job["id"], "failed")
    first = db.claim_report(86400, 1)
    db.report_done(first, 900)
    assert db.retry(first["token"], 7) == "queued"
    store.directory("other")
    db.enqueue(7, 43, "other", None, "other", None, 0)
    second_job = db.claim(excluded=("default",))
    db.fail(second_job["id"], "failed")
    second = db.claim_report(86400, 1)
    assert second["token"] is None
    assert db.retry(first["token"], 7) == "busy"
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM batches WHERE token IS NOT NULL"
            ).fetchone()[0]
            == 1
        )
