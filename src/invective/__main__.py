"""The `invective` command: `run` for one module, `sweep` for a source tree."""

from __future__ import annotations

import sys

from invective import mutate, sweep

USAGE = """\
usage: invective run   --target MODULE --tests TEST [TEST ...] [--only OPS] [--limit N] [--json FILE] [--ref REF] [--workers N]
       invective sweep --src DIR [DIR ...] --tests-dir DIR [--modules FILE ...] [--only OPS] [--limit N] [--json FILE] [--ref REF] [--workers N]

`invective run --help` and `invective sweep --help` say what each one does.
"""

COMMANDS = {"run": mutate.main, "sweep": sweep.main}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in COMMANDS:
        return COMMANDS[argv[0]](argv[1:])
    wanted_help = bool(argv) and argv[0] in ("-h", "--help")
    print(USAGE, end="", file=sys.stdout if wanted_help else sys.stderr)
    return 0 if wanted_help else 2


if __name__ == "__main__":
    sys.exit(main())
