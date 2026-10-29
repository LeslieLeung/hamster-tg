from unittest.mock import patch

import pytest

from hamster_tg.storage import safe_copy


def test_failed_copy_removes_partial_file_and_allows_retry(tmp_path):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"complete image")
    dest = tmp_path / "dest"
    dest.mkdir()

    def partial_copy(src, target):
        target.write_bytes(b"partial")
        raise OSError("No space left on device")

    with (
        patch("hamster_tg.storage.shutil.copy2", side_effect=partial_copy),
        pytest.raises(OSError, match="No space"),
    ):
        safe_copy(source, dest)

    assert list(dest.iterdir()) == []
    saved = safe_copy(source, dest)
    assert saved.name == "file_0.jpg"
    assert saved.read_bytes() == source.read_bytes()


def test_retry_deduplicates_existing_file(tmp_path):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"image")
    dest = tmp_path / "dest"
    dest.mkdir()

    first = safe_copy(source, dest)
    second = safe_copy(source, dest)

    assert first == second
    assert list(dest.iterdir()) == [first]
