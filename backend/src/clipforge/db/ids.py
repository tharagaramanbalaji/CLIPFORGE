"""UUIDv7 (time-ordered) primary keys for index locality. Python 3.12 has no uuid.uuid7."""

from __future__ import annotations

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (
        (ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76
        | ((rand >> 62) & 0xFFF) << 64
        | 0b10 << 62
        | (rand & ((1 << 62) - 1))
    )
    return uuid.UUID(int=value)
