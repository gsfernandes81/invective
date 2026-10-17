"""The one way invective declines to run."""


# Named for what the user meets, a run invective declines to start. A refusal is not
# a fault in invective, and the `Error` suffix of rule N818 would misname it.
class Refusal(Exception):  # noqa: N818
    """Raised when a condition the run depends on does not hold, before any mutant.

    The message names the cause; the plugin prints it and pytest exits with status 2.
    """
