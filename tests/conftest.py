import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:test-token")

from hamster_tg import state
from hamster_tg.database import Database
from hamster_tg.service import SaveService
from hamster_tg.storage import FileStore


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    state.api_rate_lock = asyncio.Lock()
    state.next_api_request_at_monotonic = 0


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "downloads")
    database.initialize()
    return database


@pytest.fixture
def store(db):
    return FileStore(db)


@pytest.fixture
def bot(tmp_path):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"photo")
    return SimpleNamespace(
        get_file=AsyncMock(return_value=SimpleNamespace(file_path=str(source))),
        send_chat_action=AsyncMock(),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=900)),
        edit_message_text=AsyncMock(),
    )


@pytest.fixture
def service(db, bot):
    return SaveService(db.root, bot)


async def eventually(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def enqueue(
    db,
    store,
    message_id=42,
    file_id="photo",
    folder="default",
    unique_id=None,
    group_id=None,
):
    store.directory(folder)
    db.enqueue(7, message_id, file_id, unique_id, folder, group_id, 0)
    return db.claim()
