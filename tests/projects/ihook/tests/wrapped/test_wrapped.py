"""A test in the wrapper's own directory."""

import os


def test_named_here(request):
    """The wrapper named this test in `IHOOK_NODE`."""
    assert os.environ["IHOOK_NODE"] == request.node.nodeid
