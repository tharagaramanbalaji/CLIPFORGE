from __future__ import annotations

from pathlib import Path

import pytest

from clipforge.errors import StorageError
from clipforge.storage import LocalStorage


def test_put_copy_and_move(tmp_path: Path) -> None:
    store = LocalStorage(tmp_path / "store")
    src = tmp_path / "in.bin"
    src.write_bytes(b"abc")

    store.put_file(src, "assets/a1/original/in.bin")
    assert src.exists()
    assert store.exists("assets/a1/original/in.bin")
    assert store.size("assets/a1/original/in.bin") == 3

    store.put_file(src, "assets/a1/original/moved.bin", move=True)
    assert not src.exists()
    assert store.local_path("assets/a1/original/moved.bin").read_bytes() == b"abc"

    # no leftover temp files from atomic writes
    assert not list((tmp_path / "store").rglob("*.part"))

    store.delete("assets/a1/original/moved.bin")
    assert not store.exists("assets/a1/original/moved.bin")


@pytest.mark.parametrize(
    "key", ["", "/abs", "../escape", "a/../../b", "a//b", "a/./b", "a\\b", "with space/x", "C:/x"]
)
def test_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    with pytest.raises(StorageError):
        LocalStorage(tmp_path).local_path(key)


def test_size_missing(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        LocalStorage(tmp_path).size("nope.bin")
