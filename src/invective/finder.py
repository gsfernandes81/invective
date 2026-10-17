"""The import hook that serves a target instrumented, under its real path.

The finder sits first on `sys.meta_path`, ahead of pytest's assertion
rewriter, and asks the finders behind it where a module lives. A module
whose file is a target is given a loader that instruments the source
instead of compiling it as is; everything else is left to the finder that
found it. The module keeps its path, `__file__` and line numbers, so a
traceback through it reads as it would without invective.

A target already in `sys.modules` when the finder is installed was imported
by an earlier plugin and cannot be instrumented: `install` refuses, naming
the module, rather than let its mutants never switch.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from importlib.abc import MetaPathFinder
from types import CodeType, ModuleType

from invective.errors import Refusal
from invective.instrument import Instrumented, instrument


def real(path: str) -> str:
    return os.path.realpath(path)


class Loader(importlib.machinery.SourceFileLoader):
    """A source loader that instruments instead of reading bytecode."""

    def __init__(self, fullname: str, path: str, finder: Finder):
        super().__init__(fullname, path)
        self.finder = finder

    def get_code(self, fullname: str) -> CodeType:
        # Never the bytecode cache: a `.pyc` would be the original module.
        source = self.get_data(self.path)
        code, info = instrument(source, self.path, fullname)
        self.finder.instrumented[real(self.path)] = info
        self.finder.names.setdefault(real(self.path), []).append(fullname)
        return code

    def exec_module(self, module: ModuleType) -> None:
        super().exec_module(module)
        self.finder.modules.setdefault(real(self.path), []).append(module)


class Finder(MetaPathFinder):
    """Serves every file in *targets* (real paths) instrumented."""

    def __init__(self, targets: set[str]):
        self.targets = targets
        self.stems = {os.path.splitext(os.path.basename(path))[0] for path in targets}
        self.stems.discard("__init__")
        self.instrumented: dict[str, Instrumented] = {}
        self.names: dict[str, list[str]] = {}
        self.modules: dict[str, list[ModuleType]] = {}

    def find_spec(self, fullname, path=None, target=None):
        if fullname.rpartition(".")[2] not in self.stems and not self._may_be_package(fullname):
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            spec = find(fullname, path, target)
            if spec is None:
                continue
            if spec.origin and real(spec.origin) in self.targets:
                return importlib.util.spec_from_file_location(
                    fullname, spec.origin, loader=Loader(fullname, spec.origin, self),
                    submodule_search_locations=spec.submodule_search_locations)
            return None
        return None

    def _may_be_package(self, fullname: str) -> bool:
        return any(os.path.basename(path) == "__init__.py" for path in self.targets)


def install(targets: set[str]) -> Finder:
    """Put a finder for *targets* first on `sys.meta_path`, or refuse when
    one of them is already imported."""
    loaded = sorted(name for name, module in list(sys.modules.items())
                    if real(getattr(module, "__file__", None) or "") in targets)
    if loaded:
        raise Refusal("imported before invective could instrument it, by a plugin "
                      "or a conftest loaded earlier: %s" % ", ".join(loaded))
    finder = Finder(targets)
    sys.meta_path.insert(0, finder)
    return finder


def module_name(path: str) -> str:
    """The dotted name *path* imports as: the directories above it that hold
    an `__init__.py`, up to the first that does not.

    The name is only a guess until `find_spec` resolves it to *path*; the
    caller checks.
    """
    path = real(path)
    parts = [os.path.splitext(os.path.basename(path))[0]]
    here = os.path.dirname(path)
    while os.path.isfile(os.path.join(here, "__init__.py")):
        parts.insert(0, os.path.basename(here))
        here = os.path.dirname(here)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)
