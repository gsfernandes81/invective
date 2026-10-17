"""The one way invective declines to run."""


class Refusal(Exception):
    """Something that must be true before a single mutant is worth running.

    The message names the cause; the plugin prints it and exits 2.
    """
