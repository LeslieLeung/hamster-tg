import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from time import time

SCHEMA = """
CREATE TABLE chat_settings(chat_id INTEGER PRIMARY KEY, folder TEXT NOT NULL);
CREATE TABLE folders(
    name TEXT PRIMARY KEY, indexed INTEGER NOT NULL DEFAULT 0,
    next_index INTEGER NOT NULL DEFAULT 0, scan_state TEXT NOT NULL DEFAULT 'idle',
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE files(
    path TEXT PRIMARY KEY, folder TEXT NOT NULL REFERENCES folders(name),
    sha256 TEXT, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'ready', seen_scan TEXT
);
CREATE INDEX file_hash ON files(folder, sha256, state);
CREATE TABLE batches(
    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, batch_key TEXT NOT NULL,
    folder TEXT NOT NULL REFERENCES folders(name), reply_to INTEGER NOT NULL,
    ready_at REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
    reported INTEGER NOT NULL DEFAULT 0, reporting INTEGER,
    summary_id INTEGER, token TEXT UNIQUE, expires_at REAL NOT NULL DEFAULT 0,
    notify_after REAL NOT NULL DEFAULT 0, UNIQUE(chat_id, batch_key)
);
CREATE TABLE job_items(
    id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    file_id TEXT NOT NULL, unique_id TEXT,
    state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT, path TEXT, sha256 TEXT, size INTEGER, mtime_ns INTEGER,
    duplicate INTEGER NOT NULL DEFAULT 0, UNIQUE(chat_id, message_id)
);
CREATE INDEX pending_jobs ON job_items(state, id);
CREATE INDEX batch_jobs ON job_items(batch_id, state);
CREATE INDEX telegram_files ON job_items(unique_id, state);
PRAGMA user_version=1;
"""


class Database:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / ".hamster.sqlite3"

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                conn.executescript("BEGIN IMMEDIATE;" + SCHEMA + "COMMIT;")
            elif version != 1:
                raise RuntimeError(f"Unsupported database version: {version}")
            conn.execute(
                "UPDATE folders SET scan_state='interrupted' WHERE scan_state='running'"
            )
            conn.execute("UPDATE job_items SET state='queued' WHERE state='running'")
            conn.execute("UPDATE batches SET reporting=NULL")

    def folder(self, name):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM folders WHERE name=?", (name,)).fetchone()
            return dict(row) if row else None

    def register_folder(self, name, empty=False):
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO folders(name,indexed,updated_at) VALUES(?,?,?)",
                (name, int(empty), time()),
            )

    def current_folder(self, chat_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT folder FROM chat_settings WHERE chat_id=?", (chat_id,)
            ).fetchone()
            return row[0] if row else "default"

    def switch_folder(self, chat_id, name):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO chat_settings VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET folder=excluded.folder",
                (chat_id, name),
            )

    def recent_folders(self):
        with self.connect() as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM folders ORDER BY updated_at DESC,name LIMIT 10"
                )
            ]

    def status(self, folder):
        with self.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM files WHERE folder=? AND state='ready'", (folder,)
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM job_items j JOIN batches b ON b.id=j.batch_id WHERE b.folder=? AND j.state IN ('queued','running')",
                (folder,),
            ).fetchone()[0]
            return count, pending

    def enqueue(self, chat_id, message_id, file_id, unique_id, folder, group_id, delay):
        now = time()
        key = f"group:{group_id}" if group_id else f"message:{message_id}"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM job_items WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            ).fetchone():
                return False
            row = conn.execute(
                "SELECT * FROM batches WHERE chat_id=? AND batch_key=?", (chat_id, key)
            ).fetchone()
            if row:
                batch_id = row["id"]
                conn.execute(
                    "UPDATE batches SET revision=revision+1,ready_at=?,reply_to=MIN(reply_to,?) WHERE id=?",
                    (now + delay, message_id, batch_id),
                )
            else:
                batch_id = conn.execute(
                    "INSERT INTO batches(chat_id,batch_key,folder,reply_to,ready_at) VALUES(?,?,?,?,?)",
                    (chat_id, key, folder, message_id, now + delay),
                ).lastrowid
            conn.execute(
                "INSERT INTO job_items(batch_id,chat_id,message_id,file_id,unique_id) VALUES(?,?,?,?,?)",
                (batch_id, chat_id, message_id, file_id, unique_id),
            )
            conn.execute("COMMIT")
            return True

    def claim(self, excluded=()):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in excluded) or "NULL"
            exclusion = f"AND b.folder NOT IN ({placeholders})" if excluded else ""
            row = conn.execute(
                "SELECT j.*,b.folder,b.chat_id FROM job_items j JOIN batches b ON b.id=j.batch_id "
                "JOIN folders f ON f.name=b.folder WHERE j.state='queued' AND f.indexed=1 "
                f"AND b.ready_at<=? {exclusion} ORDER BY j.id LIMIT 1",
                (time(), *excluded),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE job_items SET state='running' WHERE id=?", (row["id"],)
            )
            conn.execute("COMMIT")
            return dict(row)

    def needs_index(self):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT b.folder FROM job_items j JOIN batches b ON b.id=j.batch_id "
                "JOIN folders f ON f.name=b.folder WHERE j.state='queued' AND f.indexed=0 AND f.scan_state='idle' LIMIT 1"
            ).fetchone()
            return row[0] if row else None

    def attempt(self, job_id, attempt):
        with self.connect() as conn:
            conn.execute(
                "UPDATE job_items SET attempts=? WHERE id=?", (attempt, job_id)
            )

    def fail(self, job_id, error):
        with self.connect() as conn:
            conn.execute(
                "UPDATE job_items SET state='failed',error=? WHERE id=?",
                (error, job_id),
            )

    def file_candidates(self, folder, sha256=None, unique_id=None):
        with self.connect() as conn:
            if unique_id:
                rows = conn.execute(
                    "SELECT DISTINCT f.* FROM files f JOIN job_items j ON j.path=f.path "
                    "WHERE f.folder=? AND f.state='ready' AND j.unique_id=? "
                    "AND j.state IN ('saved','duplicate') AND j.sha256=f.sha256",
                    (folder, unique_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM files WHERE folder=? AND sha256=? AND state='ready'",
                    (folder, sha256),
                ).fetchall()
            return [dict(r) for r in rows]

    def invalidate(self, path):
        with self.connect() as conn:
            conn.execute(
                "UPDATE files SET state='stale',sha256=NULL WHERE path=?", (path,)
            )

    def reserve(self, job_id, folder, suffix, sha256, size, mtime_ns):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            n = conn.execute(
                "SELECT next_index FROM folders WHERE name=?", (folder,)
            ).fetchone()[0]
            while True:
                path = f"{folder}/file_{n}{suffix}"
                n += 1
                if (
                    not (self.root / path).exists()
                    and not (self.root / path).is_symlink()
                ):
                    break
            conn.execute("UPDATE folders SET next_index=? WHERE name=?", (n, folder))
            conn.execute(
                "UPDATE job_items SET path=?,sha256=?,size=?,mtime_ns=? WHERE id=?",
                (path, sha256, size, mtime_ns, job_id),
            )
            conn.execute("COMMIT")
            return path

    def finish(self, job_id, file, duplicate=False):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO files(path,folder,sha256,size,mtime_ns,state) VALUES(?,?,?,?,?,'ready') "
                "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256,size=excluded.size,mtime_ns=excluded.mtime_ns,state='ready'",
                (
                    file["path"],
                    file["folder"],
                    file["sha256"],
                    file["size"],
                    file["mtime_ns"],
                ),
            )
            conn.execute(
                "UPDATE job_items SET state=?,path=?,sha256=?,size=?,mtime_ns=?,duplicate=?,error=NULL WHERE id=?",
                (
                    "duplicate" if duplicate else "saved",
                    file["path"],
                    file["sha256"],
                    file["size"],
                    file["mtime_ns"],
                    int(duplicate),
                    job_id,
                ),
            )
            conn.execute(
                "UPDATE folders SET updated_at=? WHERE name=?", (time(), file["folder"])
            )
            conn.execute(
                "UPDATE batches SET revision=revision+1 WHERE reported=revision "
                "AND id=(SELECT batch_id FROM job_items WHERE id=?)",
                (job_id,),
            )
            conn.execute("COMMIT")

    def recovery_items(self):
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT j.*,b.folder FROM job_items j JOIN batches b ON b.id=j.batch_id "
                    "WHERE j.state NOT IN ('saved','duplicate')"
                )
            ]

    def items(self, batch_id):
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM job_items WHERE batch_id=? ORDER BY id", (batch_id,)
                )
            ]

    def claim_report(self, ttl, maximum):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = time()
            row = conn.execute(
                "SELECT * FROM batches b WHERE reported<revision AND reporting IS NULL "
                "AND ready_at<=? AND notify_after<=? AND NOT EXISTS "
                "(SELECT 1 FROM job_items j WHERE j.batch_id=b.id AND j.state IN ('queued','running')) "
                "ORDER BY id LIMIT 1",
                (now, now),
            ).fetchone()
            if not row:
                return None
            batch = dict(row)
            failed = conn.execute(
                "SELECT 1 FROM job_items WHERE batch_id=? AND state='failed'",
                (batch["id"],),
            ).fetchone()
            conn.execute("UPDATE batches SET token=NULL WHERE expires_at<=?", (now,))
            token = None
            if failed:
                token = conn.execute(
                    "SELECT token FROM batches WHERE id=?", (batch["id"],)
                ).fetchone()[0]
                if token is None:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM batches WHERE token IS NOT NULL"
                    ).fetchone()[0]
                    if count >= maximum:
                        victim = conn.execute(
                            "SELECT id FROM batches WHERE token IS NOT NULL AND reporting IS NULL "
                            "AND reported=revision ORDER BY expires_at LIMIT 1"
                        ).fetchone()
                        if victim:
                            conn.execute(
                                "UPDATE batches SET token=NULL WHERE id=?", (victim[0],)
                            )
                            count -= 1
                    if count < maximum:
                        token = secrets.token_urlsafe(16)
            batch["token"] = token
            batch["items"] = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM job_items WHERE batch_id=? ORDER BY id",
                    (batch["id"],),
                )
            ]
            conn.execute(
                "UPDATE batches SET reporting=?,token=?,expires_at=? WHERE id=?",
                (batch["revision"], token, now + ttl, batch["id"]),
            )
            conn.execute("COMMIT")
            return batch

    def report_done(self, batch, message_id):
        with self.connect() as conn:
            conn.execute(
                "UPDATE batches SET reported=?,reporting=NULL,summary_id=?,notify_after=0 WHERE id=?",
                (batch["revision"], message_id, batch["id"]),
            )

    def report_failed(self, batch_id):
        with self.connect() as conn:
            conn.execute(
                "UPDATE batches SET reporting=NULL,notify_after=? WHERE id=?",
                (time() + 10, batch_id),
            )

    def retry(self, token, chat_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM batches WHERE token=? AND chat_id=? AND expires_at>?",
                (token, chat_id, time()),
            ).fetchone()
            if not row:
                return "expired"
            batch_id = row["id"]
            if (
                row["reporting"] is not None
                or conn.execute(
                    "SELECT 1 FROM job_items WHERE batch_id=? AND state IN ('queued','running')",
                    (batch_id,),
                ).fetchone()
            ):
                return "busy"
            changed = conn.execute(
                "UPDATE job_items SET state='queued',attempts=0,error=NULL WHERE batch_id=? AND state='failed'",
                (batch_id,),
            ).rowcount
            if not changed:
                return "expired"
            conn.execute(
                "UPDATE batches SET revision=revision+1,ready_at=?,notify_after=0 WHERE id=?",
                (time(), batch_id),
            )
            conn.execute("COMMIT")
            return "queued"
