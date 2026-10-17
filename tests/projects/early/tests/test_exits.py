import os

from early import exits


def test_code():
    if exits.code() != 7:
        print("leaving without a verdict", flush=True)
        os._exit(5)
