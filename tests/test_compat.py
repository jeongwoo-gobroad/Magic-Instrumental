# SPDX-License-Identifier: GPL-3.0-only
import pytest
import torch

from practice_player.compat import sum_bands


@pytest.mark.parametrize("device", ["cpu", "mps"])
def test_band_sum_matches_reference_without_dense_matrix(device):
    if device == "mps" and not torch.backends.mps.is_available():
        pytest.skip("MPS unavailable")
    torch.manual_seed(42)
    source = torch.randn(2, 2, 30, 80, 2)
    index = torch.randint(0, 15, (30,))
    expected = torch.zeros(2, 2, 15, 80, 2)
    for i, target in enumerate(index):
        expected[:, :, target] += source[:, :, i]
    actual = sum_bands(source.to(device), index.to(device), 2, 15).cpu()
    torch.testing.assert_close(actual, expected)
