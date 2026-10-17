"""A script no test imports: importing it exits, so the parent must take
its mutants from the source without running it. Every mutant is
unreached."""


def main() -> int:
    return 0


raise SystemExit(3)
