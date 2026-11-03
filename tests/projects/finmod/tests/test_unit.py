"""A unittest class whose class set-up and teardown call the targets."""

import unittest

from finmod import m


class TestRoom(unittest.TestCase):
    """The room is opened once for the class and released after it."""

    is_open = False

    @classmethod
    def setUpClass(cls):
        """Store whether the room opened."""
        cls.is_open = m.opened()

    @classmethod
    def tearDownClass(cls):
        """Assert the room released as 3."""
        assert m.released() == 3

    def test_open(self):
        """The room opened."""
        assert self.is_open
