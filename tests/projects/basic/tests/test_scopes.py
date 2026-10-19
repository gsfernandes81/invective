"""The tests of `scopes`, whose functions run in class-wide set-ups and tear-downs.

In each class the last test empties what the first test reads, and the class's
tear-down asserts that it is empty.
"""

import unittest
from collections.abc import Iterator

import pytest
from basic import scopes


@pytest.fixture(scope="class")
def shelf() -> Iterator[list[str]]:
    """Yield a shelf `scopes.stock` stocked; fail at tear-down unless it is sold out."""
    stocked: list[str] = []
    scopes.stock(stocked)
    yield stocked
    assert scopes.sold_out(stocked), stocked


class TestShop:
    """Two tests that share one class-scoped shelf."""

    def test_bread_on_the_shelf(self, shelf):
        """The stocked shelf holds bread."""
        assert shelf == ["bread"]

    def test_sell_the_bread(self, shelf):
        """The bread sells, which empties the shelf."""
        assert shelf.pop() == "bread"


class TestPantry(unittest.TestCase):
    """Two tests that share jars `setUpClass` filled."""

    jars: list[str]

    @classmethod
    def setUpClass(cls):
        """Fill the class's jars with `scopes.fill`."""
        cls.jars = []
        scopes.fill(cls.jars)

    @classmethod
    def tearDownClass(cls):
        """Fail unless the jars are eaten."""
        assert scopes.eaten(cls.jars), cls.jars

    def test_a_jar_on_the_shelf(self):
        """The filled jars hold jam."""
        assert self.jars == ["jam"]

    def test_eat_the_jam(self):
        """The jam is eaten, which empties the jars."""
        assert self.jars.pop() == "jam"
