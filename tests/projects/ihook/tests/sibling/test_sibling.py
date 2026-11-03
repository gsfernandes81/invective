"""A test in a sibling of the wrapper's directory, which relies on the wrapper."""

import os

from ihook import m


def test_tag(request):
    """The wrapper named this test, and `tag` brackets a name."""
    assert os.environ.get("IHOOK_NODE") == request.node.nodeid
    assert m.tag("a") == "[a]"
