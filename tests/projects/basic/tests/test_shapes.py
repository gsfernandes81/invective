"""The tests of `shapes`, one per shape of function."""

from basic import shapes


def test_default_factory():
    """A new tile's tags come from `default_tags`, which `registered` registered."""
    assert shapes.Tile(2).tags == ["new"]
    assert shapes.REGISTRY == ["default_tags"]


def test_methods_and_property():
    """A side fits from 1 to 10, and only the unit tile is a unit."""
    assert shapes.Tile.fits(3)
    assert not shapes.Tile.fits(11)
    assert shapes.Tile.unit().side == 1
    assert shapes.Tile(1).is_unit
    assert not shapes.Tile(2).is_unit


def test_closure():
    """A scaler by 3 makes 2 into 6."""
    assert shapes.scaler(3)(2) == 6
