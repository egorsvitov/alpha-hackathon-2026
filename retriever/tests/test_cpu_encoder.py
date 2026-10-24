import numpy as np
import pytest

from retriever.cpu_encoder import check_parity


def test_parity_accepts_small_numeric_error():
    a = np.eye(3, dtype=np.float32)
    assert check_parity(a + 1e-7, a)["samples"] == 3


@pytest.mark.parametrize(
    "bad", [np.eye(3)[::-1], np.eye(3) * float("nan"), np.zeros((3, 3)), np.eye(2)]
)
def test_parity_rejects_wrong_space_and_invalid_data(bad):
    with pytest.raises(ValueError):
        check_parity(bad, np.eye(3))
