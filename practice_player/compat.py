# SPDX-License-Identifier: GPL-3.0-only
"""Bound memory in pymss-core 0.1.16's MPS Mel-band folding.

Upstream's dense batched one-hot matmul allocates >13 GB for an 8 s chunk.
Real-valued index_add_ computes the same band sum with output-sized memory.
Only the pinned dependency's MPS path changes, for the duration of inference.
"""

from contextlib import contextmanager


def sum_bands(source, index, dim, size):
    shape = list(source.shape)
    shape[dim] = size
    return source.new_zeros(shape).index_add_(dim, index.reshape(-1), source)


@contextmanager
def mps_mel_band_fix(enabled: bool):
    if not enabled:
        yield
        return
    from pymss_core.modules.bs_roformer import mel_band_roformer

    original = mel_band_roformer.scatter_add_equivalent

    def fold(source, index, dim, size):
        if source.device.type == "mps":
            return sum_bands(source, index, dim, size)
        return original(source, index, dim, size)

    mel_band_roformer.scatter_add_equivalent = fold
    try:
        yield
    finally:
        mel_band_roformer.scatter_add_equivalent = original
