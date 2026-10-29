import asyncio
import os

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:test-token")

from hamster_tg import state


@pytest.fixture(autouse=True)
def reset_state():
    state.retry_requests.clear()
    state.hash_index.clear()
    state.hash_index_total_entries = 0
    state.pending_media_group_acks.clear()
    state.chat_folders.clear()
    state.download_pipeline_lock = asyncio.Lock()
    state.api_rate_lock = asyncio.Lock()
    state.next_api_request_at_monotonic = 0
    yield
    state.retry_requests.clear()
