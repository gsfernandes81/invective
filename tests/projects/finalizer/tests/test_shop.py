"""Two tests that use the session fixture `shop_open`.

`test_closing` runs first and is the cheaper of the two tests that run `closing`, so
a child that stops at its failure still has the session fixture set up, and the
fixture's finalizer runs in the child's final teardown.
"""

import time

import pytest
from finalizer import shop


@pytest.mark.usefixtures("shop_open")
def test_closing():
    """`closing` answers "ok"."""
    assert shop.closing() == "ok"


@pytest.mark.usefixtures("shop_open")
def test_last():
    """Costs more than `test_closing` in the baseline, so it runs second."""
    time.sleep(0.05)
