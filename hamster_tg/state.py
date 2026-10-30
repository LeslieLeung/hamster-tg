import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic


@dataclass
class RetryRequest:
    chat_id: int
    folder: str
    file_ids: list[str]
    reply_to_message_id: int
    created_at: float = field(default_factory=monotonic)
    in_progress: bool = False


# chat_id -> current folder name
chat_folders: dict[int, str] = {}

# dest_dir -> {sha256_hex: Path} lazy-built content-hash index for dedup
hash_index: dict[Path, dict[str, Path]] = {}
hash_index_total_entries = 0
HASH_INDEX_MAX_ENTRIES = 10_000

# (chat_id, media_group_id) -> pending summary state
pending_media_group_acks: dict[tuple[int, str], dict[str, object]] = {}

# Reliability-first pipeline: serialize downloads and pace Bot API requests.
download_pipeline_lock = asyncio.Lock()
api_rate_lock = asyncio.Lock()
next_api_request_at_monotonic = 0.0

retry_requests: dict[str, RetryRequest] = {}
