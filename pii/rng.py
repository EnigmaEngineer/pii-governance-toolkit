"""Named random streams.

One seed, many streams, each derived by hashing the seed together with the stream's name.
The alternative everybody reaches for is `seed + 1`, `seed + 2`, and it breaks quietly the
first time somebody inserts a new stream in the middle, because every stream after it
shifts and every previously generated corpus becomes unreproducible with nothing raising.

Hashing the name means adding a stream never touches any other stream.
"""

from __future__ import annotations

import hashlib
import random


def derive(seed: int, stream: str) -> int:
    """A 64 bit stream seed from a base seed and a name."""
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError("seed must be an int, got {}".format(type(seed).__name__))
    if seed < 0:
        raise ValueError("seed must not be negative")
    if not stream:
        raise ValueError("stream name must not be empty")
    blob = "{}:{}".format(seed, stream).encode("utf-8")
    return int.from_bytes(hashlib.sha256(blob).digest()[:8], "big")


def stream(seed: int, name: str) -> random.Random:
    return random.Random(derive(seed, name))
