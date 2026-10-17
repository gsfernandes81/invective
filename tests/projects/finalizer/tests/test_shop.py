"""`test_closing` runs first and is the cheaper of the two tests that run
`closing`, so a child that stops at its failure still has the session
fixture set up, and its finalizer runs in the child's final teardown."""

import time

from finalizer import shop


def test_closing(shop_open):
    assert shop.closing() == "ok"


def test_last(shop_open):
    time.sleep(0.05)
