"""Run the engine over every module that has tests, and report the survivors.

**Scope.** The default operator is `RAISE` -- replace a `raise` with `pass` --
because a deleted refusal is the failure that goes on printing "ok" while
gating nothing. The other operators are available with `--only` and cost more
wall clock.

Each module is mutated against the test files that IMPORT it, which is a
coverage map the repository already maintains by writing tests that import what
they test. A module with no such file is reported as unmeasured rather than
skipped silently -- that distinction is the whole point.

Run it from anywhere inside the project; `--src`, `--tests-dir` and
`--modules` are relative to the directory it is run in. It exits 1 when a
module breaks the project's `[tool.invective]` rules.

    invective sweep --src src/pkg --tests-dir tests [--limit N] [--only OPS]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

from invective.config import (outside_refusal, project_root,
                               pytest_config_above, relative_to_root)
from invective.errors import Refusal
from invective.mutate import _STOP_GRACE, _Terminated, _exit_by, stopping_on_sigterm

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
    that is not a package holds loose files and packages below it, each named
    from its own top: its tests put the directory on `sys.path` and import a
    loose module by its bare name.
    """
    top = _package_top(root, module, sources)
    if top is None:
        return None
    rel = os.path.relpath(os.path.abspath(os.path.join(root, module)),
                          os.path.dirname(top))
    return rel[:-3].replace(os.sep, ".").replace(".__init__", "")


def _package_top(root, module, sources):
    """The outermost package directory *module* is imported through, or None
    for a loose file."""
    mod = os.path.abspath(os.path.join(root, module))
    home = os.path.dirname(mod)
    for base in sources:
        base = os.path.abspath(os.path.join(root, base))
        if (mod.startswith(base + os.sep)
                and os.path.isfile(os.path.join(base, "__init__.py"))):
            home = base
            break
    if not os.path.isfile(os.path.join(home, "__init__.py")):
        return None
    top = home
    while os.path.isfile(os.path.join(os.path.dirname(top), "__init__.py")):
        top = os.path.dirname(top)
    return top


def _namespace(root, module, sources):
    """A pattern for the optional dotted prefix a test may import *module*'s
    package under: the project's own directories between *root* and the
    package's top, outermost first, an outer one only with the ones inside
    it (`src/acme` gives `acme.`, `src.acme.` or nothing)."""
    rel = os.path.relpath(os.path.dirname(_package_top(root, module, sources)),
                          os.path.abspath(root))
    if rel == os.curdir or rel.startswith(os.pardir):
        return ""
    rx = ""
    for part in rel.split(os.sep):
        rx = r"(?:%s%s\.)?" % (rx, re.escape(part))
    return rx


def covering(root, module, sources, tests_dir):
    """Test files that IMPORT this module, plus the one named after it.

    A file named after the module counts at the top of *tests_dir* and in the
    directory that mirrors the module's own under *sources*
    (`src/app/api/models.py` and `tests/api/test_models.py`); anywhere else a
    test file covers a module only by importing it, since `test_models.py`
    in another directory is another module's. A loose module, one outside
    any package, is imported by its bare stem, and that covers it at the
    same two places only: a bare stem in another directory is as likely that
    directory's own helper. A dotted import is specific enough to count
    anywhere.

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
    # What counts anywhere in the tests directory, and what only at its top
    # and in the mirror directory, where a bare name is the module's own.
    anywhere, near_only = [], []
    if dotted:
        # A package below a namespace directory (one with no `__init__.py`)
        # is imported under the namespace's name, which `import_name` cannot
        # see: a namespace has no file to find. The prefix is optional, and
        # it is only the project's own directories above the package's top,
        # never any identifier: `gate.core` is matched inside
        # `acme.gate.core`, and `utils` is not matched inside `email.utils`,
        # nor a package `path` inside `os.path.join`.
        # **An import statement, not the word `import`.** `from email
        # import utils` holds `import utils` and loads nothing of the
        # project's, so the name counts after `import` only at the start of
        # an `import` statement, alone or in a list. For the same reason a
        # top-level package's bare name followed by a dot counts only where a
        # loose module's stem does, since `from email import utils` leaves
        # `utils.` in any file; a dotted name followed by a dot counts
        # anywhere.
        ns = _namespace(root, module, sources)
        anywhere += [
            r"^\s*import\s+(?:[\w.]+(?:\s+as\s+\w+)?\s*,\s*)*%s%s\b"
            % (ns, re.escape(dotted)),
            r"from\s+%s%s\s+import" % (ns, re.escape(dotted)),
            r"from\s+%s%s\s+import\s+.*\b%s\b"
            % (ns, re.escape(dotted.rsplit(".", 1)[0]), re.escape(stem))]
        attr = r"(?<![\w.])%s%s\." % (ns, re.escape(dotted))
        (anywhere if "." in dotted else near_only).append(attr)
    else:
        near_only += [r"^\s*import\s+%s\b" % re.escape(stem),
                      r"^\s*from\s+%s\s+import" % re.escape(stem),
                      r"\b%s\.\w" % re.escape(stem)]
    named = "test_%s.py" % stem
    hits = []
    tests_abs = os.path.join(root, tests_dir)
    mirror = os.curdir
    for base in sources:
        base = os.path.join(root, base)
        if os.path.abspath(os.path.join(root, module)).startswith(
                os.path.abspath(base) + os.sep):
            mirror = os.path.relpath(
                os.path.dirname(os.path.join(root, module)), base)
            break
    # The walk is sorted in place, so the top-level files come first and each
    # subdirectory follows in name order: the same list on every run. A
    # directory link is not followed (`followlinks` is off), as `modules()`
    # does not follow one, and a `__pycache__` holds no test the project wrote.
    for dirpath, dirnames, filenames in os.walk(tests_abs):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for f in sorted(filenames):
            if not (f.startswith("test_") and f.endswith(".py")):
                continue
            path = os.path.join(dirpath, f)
            with open(path, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
            near = os.path.relpath(dirpath, tests_abs) in (os.curdir, mirror)
            by_name = f == named and near
            by_import = any(re.search(pat, body, re.M) for pat in
                            anywhere + (near_only if near else []))
            if by_name or by_import:
                hits.append(os.path.normpath(os.path.join(
                    tests_dir, os.path.relpath(path, tests_abs))))
    return hits


#: How long an engine told to stop is given to unwind: its own grace for the
#: run it stops, and the copy's removal on top.
_GRACE = 2 * _STOP_GRACE  # invective: accept[equivalent: 2 -> 3] any bound past the engine's own grace serves


def _engine(cmd, cwd):
    """The engine's exit code, stdout and stderr, run to its end.

    **Not `subprocess.run`, which on any exception kills its child outright,
    copy and all.** When this process is stopped while the engine runs, the
    engine is told, waited for, and only then is the exception let go: a
    SIGTERM to the sweep is forwarded as one the engine unwinds on, and the
    copy is gone before the sweep is. `communicate` itself
    catches an interrupt, the subclass included, waits a quarter second for
    the child and re-raises, so a SIGTERM takes that much longer per level
    to come out; harmless.
    """
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        with stopping_on_sigterm():
            stdout, stderr = proc.communicate()
    # `_Terminated` is a `KeyboardInterrupt`, so this clause has to come
    # before that one: the other way round a SIGTERM to the sweep would fall
    # into the wait-only branch, the engine would never be told, and after
    # the wait it would be killed outright with its copy stranded.
    except _Terminated:
        proc.terminate()
        _finish(proc)
        raise
    except KeyboardInterrupt:
        # A ^C: the engine is in the same foreground group and got the
        # SIGINT itself, so it is unwinding already, and a SIGTERM now would
        # raise inside its cleanup and cut it short. Told only when the wait
        # runs out, which is a `kill -INT` to this process alone, one the
        # engine never saw.
        _finish(proc)
        raise
    except BaseException:
        # `SystemExit` and the rest: the engine has heard nothing.
        proc.terminate()
        _finish(proc)
        raise
    return proc.returncode, stdout, stderr


def _finish(proc):
    """Wait for the engine to end; when it does not, tell it to stop, which
    its once-only handler makes harmless if it was told already, and wait
    once more before killing it outright."""
    try:
        proc.wait(timeout=_GRACE)
        return
    except subprocess.TimeoutExpired:
        proc.terminate()
    try:
        proc.wait(timeout=_GRACE)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _from_root(given, root):
    """*given*, typed here, as a path from *root*."""
    try:
        return relative_to_root(given, root)
    except ValueError:
        raise Refusal("%s is outside the project at %s" % (given, root)) from None


def parser():
    """The sweep's command line, apart from `main` so a test can read it."""
    ap = argparse.ArgumentParser(prog="invective sweep", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, nargs="+",
                    help="directories of modules to mutate")
    ap.add_argument("--tests-dir", required=True,
                    help="the directory holding the test_*.py files")
    ap.add_argument("--only", default="RAISE")
    # **A default cap.** Without one a module with forty refusals and a
    # 40-second baseline is two hours on its own, and the sweep looks hung
    # rather than slow. The engine spaces a capped selection evenly rather
    # than taking the first N, so a cap does not mean "the top of the file".
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--json", default=None)
    ap.add_argument("--modules", nargs="*",
                    help="sweep only these module files, relative to the "
                         "current directory, instead of discovering them "
                         "under --src")
    ap.add_argument("--ref", help="run on this git commit, branch or tag "
                                  "instead of the files as they stand")
    return ap


def main(argv=None):
    ap = parser()
    args = ap.parse_args(argv)

    root = project_root()
    try:
        # **Typed paths are read from where they were typed**, as every
        # other tool reads them, and given from the top, where the engine
        # is started and its copy begins.
        args.src = [_from_root(given, root) for given in args.src]
        args.tests_dir = _from_root(args.tests_dir, root)
        if args.modules is not None:
            args.modules = [_from_root(given, root) for given in args.modules]
        # A directory that is not there walks as empty, and an empty sweep
        # prints "0 module(s) measured" -- which reads as a result, not as a
        # mistyped path.
        for given in [*args.src, args.tests_dir]:
            if not os.path.isdir(os.path.join(root, given)):
                raise Refusal("%s is not a directory under %s" % (given, root))
        # Every module's engine would refuse on its own; said once here,
        # the sweep does not print a `?` for each.
        left_out = pytest_config_above(root, [args.tests_dir])
        if left_out:
            raise outside_refusal(left_out, root)
    except Refusal as exc:
        print("\nrefused: %s" % exc, file=sys.stderr)
        return 2
    try:
        return _sweep(args, root)
    except _Terminated as exc:
        # The engine that was running is gone and its copy with it; now die
        # by the signal, as `run` does.
        _exit_by(exc)


def _sweep(args, root):
    """Every module measured and printed as it is, and the exit code."""
    report, unmeasured, failing = [], [], []
    # A bare `--modules` names no file and sweeps none: an empty list is what
    # a shell expansion of the changed files expands to when nothing changed.
    for module in (args.modules if args.modules is not None
                   else modules(root, args.src, args.tests_dir)):
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
        if args.ref:
            cmd += ["--ref", args.ref]
        # **The per-module report is asked for and read, not re-derived from
        # the printed summary.** The summary carries a score; the engine's
        # JSON carries which test killed each mutant, and that is the half
        # that makes a sweep an oracle rather than a league table -- a line
        # whose only killer is a test nominally about something else is a
        # finding no percentage can show.
        with tempfile.TemporaryDirectory(prefix="invective-sweep-") as box:
            one = os.path.join(box, "report.json")
            code, stdout, stderr = _engine(cmd + ["--json", one], root)
            body = stdout + stderr
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
            # A refusal is one sentence the engine chose and a person has to
            # read whole; any other last line is only cut to a display width.
            said_lines = body.strip().splitlines() or ["no output"]
            refusal = next((ln for ln in said_lines
                            if ln.startswith("refused:")), None)

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
                        code,
                        refusal if refusal is not None else
                        # invective: accept[equivalent: 90 -> 91] a display width
                        said_lines[-1][:90]))
            report.append({"module": module, "note": note, "tests": tests})
            print("%-46s %s" % (module, note))
            continue
        killed, total, pct, survived = summary.groups()
        lines = [ln for ln in body.splitlines() if "SURVIVED" in ln]
        fails = [ln.partition(":")[2].strip() for ln in body.splitlines()
                 if ln.startswith("fails:")]
        if code == 1:
            failing.append(module)
        report.append({"module": module, "tests": tests, "killed": int(killed),
                       "mutants": int(total), "score": float(pct),
                       "survivors": lines,
                       # **The engine's report, whole**, which is what
                       # `invective run --json` writes for this module:
                       # survivors, accepted and stale entries as data (the
                       # `survivors` above are the printed lines; `report`
                       # holds the entries), and whatever else the engine puts
                       # in its entries. Unknown -- not none -- when it could
                       # not be read, `detail` being `{}` then. `kills` and
                       # `broken` are the same report's, kept for the
                       # consumer that reads them here; `report` is the one
                       # to read.
                       "report": detail or None,
                       "kills": detail.get("kills"),
                       "broken": detail.get("broken"),
                       "fails": fails})
        print("%-46s %3s/%-3s %5s%%  %s survived"
              % (module, killed, total, pct, survived))
        for ln in lines:
            print("      " + ln.strip())
        for fail in fails:
            print("      fails: " + fail)

    print("\n%d module(s) measured; %d with no test file importing them:"
          % (len([r for r in report if "score" in r]), len(unmeasured)))
    for m in unmeasured:
        print("   " + m)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            # invective: accept[equivalent: 1 -> 2] how wide the JSON is indented
            json.dump({"measured": report, "unmeasured": unmeasured}, fh, indent=1)
    if failing:
        print("\n%d module(s) break the project's rules: %s"
              % (len(failing), ", ".join(failing)))
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
