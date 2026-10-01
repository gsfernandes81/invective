"""Run the engine over every module that has tests, and report the survivors.

**Scope.** The default operator is `RAISE` -- replace a `raise` with `pass` --
because a deleted refusal is the failure that goes on printing "ok" while
gating nothing. The other operators are available with `--only` and cost more
wall clock.

Each module is mutated against the test files that IMPORT it, which is a
coverage map the repository already maintains by writing tests that import what
they test. A module with no such file is reported as unmeasured rather than
skipped silently -- that distinction is the whole point.

Run it from the repository's top level; `--src` and `--tests-dir` are
relative to it.

    invective sweep --src src/pkg --tests-dir tests [--limit N] [--only OPS]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

from invective.mutate import Refusal, repo_root

HERE = os.path.dirname(os.path.abspath(__file__))


def modules(root, sources, tests_dir):
    """Every source module under *sources* worth mutating, relative to *root*."""
    tests_abs = os.path.abspath(os.path.join(root, tests_dir))
    out = []
    for base in sources:
        for dirpath, _d, files in os.walk(os.path.join(root, base)):
            if "__pycache__" in dirpath:
                continue
            # A suite's harness files do not all start `test_`, so skipping by
            # filename alone would let `conftest.py` and shared helpers in as
            # things to mutate. `covering()` maps those to nearly every
            # file, so each mutant would re-run a huge selection.
            # **`==` as well as the prefix, and the prefix carries a
            # separator.** The suite's own files sit at exactly the tests
            # directory, so a bare `startswith(tests + os.sep)` would re-admit
            # them; a bare `startswith(tests)` would also swallow a `tests_x`
            # sibling.
            here = os.path.abspath(dirpath)
            if here == tests_abs or here.startswith(tests_abs + os.sep):
                continue
            for f in sorted(files):
                if not f.endswith(".py") or f.startswith("test_"):
                    continue
                out.append(os.path.relpath(os.path.join(dirpath, f), root))
    return out


def import_name(root, module, sources):
    """The dotted name a test imports *module* by, or None for a loose file.

    A source directory that is a package (it holds an `__init__.py`) is
    imported from the first directory above it that is not one, so
    `src/pkg/a/b.py` under `--src src/pkg` is `pkg.a.b`. A source directory
    that is not a package holds loose files: its tests put the directory on
    `sys.path` and import each module by its bare name.
    """
    mod = os.path.abspath(os.path.join(root, module))
    home = os.path.dirname(mod)
    for base in sources:
        base = os.path.abspath(os.path.join(root, base))
        if mod.startswith(base + os.sep):
            home = base
            break
    if not os.path.isfile(os.path.join(home, "__init__.py")):
        return None
    top = home
    while os.path.isfile(os.path.join(os.path.dirname(top), "__init__.py")):
        top = os.path.dirname(top)
    rel = os.path.relpath(mod, os.path.dirname(top))
    return rel[:-3].replace(os.sep, ".").replace(".__init__", "")


def covering(root, module, sources, tests_dir):
    """Test files that IMPORT this module, plus the one named after it.

    **Matched on the import, never on the bare stem.** Matching on whether
    the module's name appears anywhere in a test file selects dozens of
    files for a module used everywhere -- essentially the whole suite as a
    baseline, per module, per mutant, which turns hours into days.

    A test that drives a module imports it. The dotted path, the leaf import
    and the file named after it are the three ways that is written, and
    an over-tight map degrades to a HIGHER apparent score on a module nothing
    covers -- so a module that matches nothing is reported as unmeasured rather
    than as a perfect one.
    """
    dotted = import_name(root, module, sources)
    stem = os.path.basename(module)[:-3]
    if stem in ("__init__", "__main__"):
        stem = os.path.basename(os.path.dirname(module))
    wanted = []
    if dotted:
        wanted += [r"\bimport\s+%s\b" % re.escape(dotted),
                   r"from\s+%s\s+import" % re.escape(dotted),
                   r"from\s+%s\s+import\s+.*\b%s\b"
                   % (re.escape(dotted.rsplit(".", 1)[0]), re.escape(stem)),
                   r"\b%s\." % re.escape(dotted)]
    else:
        wanted += [r"^\s*import\s+%s\b" % re.escape(stem),
                   r"^\s*from\s+%s\s+import" % re.escape(stem),
                   r"\b%s\.\w" % re.escape(stem)]
    named = "test_%s.py" % stem
    hits = []
    tests_abs = os.path.join(root, tests_dir)
    for f in sorted(os.listdir(tests_abs)):
        if not (f.startswith("test_") and f.endswith(".py")):
            continue
        with open(os.path.join(tests_abs, f), encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        if f == named or any(re.search(pat, body, re.M) for pat in wanted):
            hits.append(os.path.normpath(os.path.join(tests_dir, f)))
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser(prog="invective sweep", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, nargs="+",
                    help="directories of modules to mutate, relative to the repo root")
    ap.add_argument("--tests-dir", required=True,
                    help="the directory holding the test_*.py files")
    ap.add_argument("--only", default="RAISE")
    # **A default cap.** Without one a module with forty refusals and a
    # 40-second baseline is two hours on its own, and the sweep looks hung
    # rather than slow. The engine spaces a capped selection evenly rather
    # than taking the first N, so a cap does not mean "the top of the file".
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--json", default=None)
    ap.add_argument("--modules", nargs="*")
    args = ap.parse_args(argv)

    try:
        root = repo_root()
        # A directory that is not there walks as empty, and an empty sweep
        # prints "0 module(s) measured" -- which reads as a result, not as a
        # mistyped path.
        for given in [*args.src, args.tests_dir]:
            if not os.path.isdir(os.path.join(root, given)):
                raise Refusal("%s is not a directory under %s" % (given, root))
    except Refusal as exc:
        print("\nrefused: %s" % exc, file=sys.stderr)
        return 2

    report, unmeasured = [], []
    for module in (args.modules or modules(root, args.src, args.tests_dir)):
        tests = covering(root, module, args.src, args.tests_dir)
        if not tests:
            unmeasured.append(module)
            continue
        # `HERE`, not `HERE/..`: a path built relative to where this file
        # happens to live breaks silently the moment the file moves. The
        # failure mode is quiet -- every module comes back `?` and the
        # footer says "0 module(s) measured", which reads as an empty
        # result rather than as a driver that cannot start.
        cmd = [sys.executable, os.path.join(HERE, "mutate.py"),
               "--target", module, "--tests", *tests, "--only", args.only]
        if args.limit:
            cmd += ["--limit", str(args.limit)]
        # **The per-module report is asked for and read, not re-derived from
        # the printed summary.** The summary carries a score; the engine's
        # JSON carries which test killed each mutant, and that is the half
        # that makes a sweep an oracle rather than a league table -- a line
        # whose only killer is a test nominally about something else is a
        # finding no percentage can show.
        with tempfile.TemporaryDirectory(prefix="invective-sweep-") as box:
            one = os.path.join(box, "report.json")
            got = subprocess.run(cmd + ["--json", one], capture_output=True,
                                 text=True, cwd=root)
            body = got.stdout + got.stderr
            detail = {}
            try:
                with open(one, encoding="utf-8") as fh:
                    detail = json.load(fh)
            except (OSError, ValueError):
                # A module that refused or found no sites writes none, which
                # the summary branch below already reports. Silence here would
                # only hide the case where it wrote one this cannot read.
                if "killed (" in body:
                    print("      (could not read %s's own report)" % module)
        summary = re.search(r"(\d+)/(\d+) killed \(([\d.]+)%\), (\d+) survived", body)
        if not summary:
            # "no mutation sites" and "RED baseline" are answers ABOUT the
            # module; anything else is this driver failing to get an answer
            # at all, and the two must not print the same way -- a bare "?"
            # would hide a broken driver behind what looks like an ordinary
            # empty result. So the third arm says the exit code and the
            # last thing the engine actually said, which turns "0
            # module(s) measured" from a shrug into a thing somebody
            # chases.
            # **The engine's own sentence, not the word.** A bare `"red" in
            # body` is true of "occurred", "required" and "ignored", so a
            # traceback from a driver that could not start was labelled a red
            # baseline.
            note = "no mutation sites" if "no mutation sites" in body else \
                   ("RED baseline" if "is RED on the unmutated tree" in body else
                    "DRIVER FAILED rc=%d: %s" % (
                        got.returncode,
                        (body.strip().splitlines() or ["no output"])[-1][:90]))
            report.append({"module": module, "note": note, "tests": tests})
            print("%-46s %s" % (module, note))
            continue
        killed, total, pct, survived = summary.groups()
        lines = [ln for ln in body.splitlines() if "SURVIVED" in ln]
        report.append({"module": module, "tests": tests, "killed": int(killed),
                       "mutants": int(total), "score": float(pct),
                       "survivors": lines,
                       "kills": detail.get("kills", []),
                       "broken": detail.get("broken", 0)})
        print("%-46s %3s/%-3s %5s%%  %s survived"
              % (module, killed, total, pct, survived))
        for ln in lines:
            print("      " + ln.strip())

    print("\n%d module(s) measured; %d with no test file importing them:"
          % (len([r for r in report if "score" in r]), len(unmeasured)))
    for m in unmeasured:
        print("   " + m)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"measured": report, "unmeasured": unmeasured}, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
