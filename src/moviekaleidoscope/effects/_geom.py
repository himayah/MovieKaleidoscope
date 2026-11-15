"""Shared geometry helpers for frame effects."""

from __future__ import annotations

from functools import lru_cache

import numpy as np


@lru_cache(maxsize=4)
def pixel_grid(w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    """Read-only float32 arrays X, Y of pixel coordinates (shape h x w)."""
    x, y = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    x.flags.writeable = False
    y.flags.writeable = False
    return x, y


def reflect_index(i: np.ndarray, size: int) -> np.ndarray:
    """Fold integer indices into [0, size) by repeated reflection (0 1 2 | 2 1 0 | 0 1 2 ...)."""
    period = 2 * size
    k = np.mod(i, period)
    return np.where(k >= size, period - 1 - k, k)


def wrap_coords(u: np.ndarray, size: float, mirror: bool) -> np.ndarray:
    """Wrap float coordinates into [0, size): plain repeat, or mirrored repeat (triangle wave)."""
    if mirror:
        return size - np.abs(np.mod(u, 2 * size) - size)
    return np.mod(u, size)
