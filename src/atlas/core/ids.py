"""Identifier factory. UUIDv7 per DEC-037 (proposed); swapping to UUIDv4 changes only this module.

Python 3.12 has no stdlib uuid7, so it is built here per RFC 9562 §5.7: 48-bit Unix milliseconds,
version 7, 74 random bits, variant 10.
"""

from __future__ import annotations

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    unix_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand >> 68 & 0xFFF  # 12 bits
    rand_b = rand & ((1 << 62) - 1)  # 62 bits
    value = (unix_ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)
