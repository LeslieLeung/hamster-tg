import hashlib
import re
import shutil
from pathlib import Path

from .config import DOWNLOAD_ROOT
from . import state


def get_dest_dir(folder: str) -> Path:
    dest = DOWNLOAD_ROOT / folder
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_hash_index(dest_dir: Path) -> dict[str, Path]:
    """Return (and lazily build) the content-hash index for *dest_dir*."""
    idx = state.hash_index.get(dest_dir)
    if idx is not None:
        return idx
    # Evict all caches when total entries exceed the cap.
    if state.hash_index_total_entries >= state.HASH_INDEX_MAX_ENTRIES:
        state.hash_index.clear()
        state.hash_index_total_entries = 0
    idx = {}
    for p in dest_dir.iterdir():
        if p.is_file():
            idx[file_sha256(p)] = p
    state.hash_index[dest_dir] = idx
    state.hash_index_total_entries += len(idx)
    return idx


def next_file_index(dest_dir: Path) -> int:
    """Return the next available file_N index in dest_dir."""
    max_idx = -1
    for p in dest_dir.iterdir():
        if p.is_file():
            match = re.match(r"^file_(\d+)", p.stem)
            if match:
                max_idx = max(max_idx, int(match.group(1)))
    return max_idx + 1


def safe_copy(src: Path, dest_dir: Path) -> Path:
    """Copy src into dest_dir with file_N naming; skip if same-content duplicate exists."""
    src_hash = file_sha256(src)
    idx = get_hash_index(dest_dir)
    existing = idx.get(src_hash)
    if existing is not None and existing.exists():
        return existing

    suffix = src.suffix
    n = next_file_index(dest_dir)
    target = dest_dir / f"file_{n}{suffix}"
    while target.exists():
        n += 1
        target = dest_dir / f"file_{n}{suffix}"
    shutil.copy2(src, target)
    idx[src_hash] = target
    state.hash_index_total_entries += 1
    return target

