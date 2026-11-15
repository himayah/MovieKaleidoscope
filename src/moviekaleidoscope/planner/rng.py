"""Purpose-separated deterministic random streams (SPEC §5.3)."""

from __future__ import annotations

import hashlib
import random


def make_rng(seed: int, purpose: str) -> random.Random:
    """A Random seeded from (seed, purpose). Stable across platforms and Python versions."""
    digest = hashlib.sha256(f"{seed}:{purpose}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def new_seed() -> int:
    return random.SystemRandom().randrange(2**32)
