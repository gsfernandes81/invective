"""The import hook that serves a target instrumented, under its real path.

The finder sits first on `sys.meta_path`, ahead of pytest's assertion rewriter, and
calls `find_spec` on the finders behind it to locate a module. A module whose file is
a target gets a loader that instruments the source instead of compiling it as it is;
any other module is left to the finder that located it. The module keeps its path,
`__file__` and line numbers, so a traceback through it reads as it would without
invective.

A target already in `sys.modules` when the finder is installed was imported by an
earlier plugin and cannot be instrumented, so its mutants could not be made live:
`install` raises `Refusal`, naming the module.

A target's mutant table depends on its path and source only. `table` builds it
without running the module, and the loader builds the same table when the module is
imported, under whatever name, since both take the module part of each id from
`module_name`.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from types import CodeType, ModuleType
from typing import override

from invective.errors import Refusal
from invective.instrument import Instrumented, instrument


def real(path: str) -> str:
    """Return *path* with every symlink resolved: the form that keys a target."""
    return os.path.realpath(path)


class Loader(importlib.machinery.SourceFileLoader):
    """A source loader that instruments a target and does not read its bytecode cache."""

    def __init__(self, fullname: str, path: str, finder: Finder) -> None:
        """Load *fullname* from *path*, recording what the loader builds in *finder*."""
        super().__init__(fullname, path)
        self.finder = finder

    @override
    def get_code(self, fullname: str) -> CodeType:
        """Return the module's code with every mutant embedded.

        The mutant table goes in the finder's `instrumented`, under the real path.
        """
        # Not the bytecode cache, whose `.pyc` holds the original module.
        source = self.get_data(self.path)
        code, info = instrument(source, self.path, module_name(self.path))
        self.finder.instrumented[real(self.path)] = info
        return code

    @override
    def exec_module(self, module: ModuleType) -> None:
        """Run *module*, then add it to the finder's `modules` under the real path."""
        super().exec_module(module)
        self.finder.modules.setdefault(real(self.path), []).append(module)


class Finder(MetaPathFinder):
    """A meta path finder that serves every target instrumented."""

    def __init__(self, targets: set[str]) -> None:
        """Serve the files in *targets*, given as real paths."""
        self.targets = targets
        self.stems = {os.path.splitext(os.path.basename(path))[0] for path in targets}
        self.stems.discard("__init__")
        #: A target's real path to the table its loader built.
        self.instrumented: dict[str, Instrumented] = {}
        #: A target's real path to every module object imported from it.
        self.modules: dict[str, list[ModuleType]] = {}

    @override
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        """Return a spec with an instrumenting `Loader` when *fullname* is a target.

        Return None for any other module, which leaves it to the finders behind this
        one.
        """
        stem = fullname.rpartition(".")[2]
        if stem not in self.stems and not self._serves_packages():
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
                    fullname,
                    spec.origin,
                    loader=Loader(fullname, spec.origin, self),
                    submodule_search_locations=spec.submodule_search_locations,
                )
            return None
        return None

    def _serves_packages(self) -> bool:
        """Return whether a package's `__init__.py` is among the targets.

        A package is imported by its own name, which no file stem gives, so any
        module name may then be a target.
        """
        return any(os.path.basename(path) == "__init__.py" for path in self.targets)


def install(targets: set[str]) -> Finder:
    """Put a finder for *targets* first on `sys.meta_path`, and return it.

    Raises `Refusal`, naming the modules, when a target is already imported.
    """
    loaded = sorted(
        name
        for name, module in list(sys.modules.items())
        if real(getattr(module, "__file__", None) or "") in targets
    )
    if loaded:
        raise Refusal(
            "imported before invective could instrument it, by a plugin or a "
            f"conftest loaded earlier: {', '.join(loaded)}"
        )
    finder = Finder(targets)
    sys.meta_path.insert(0, finder)
    return finder


def table(path: str) -> Instrumented:
    """Return the mutant table of the target at the real *path*, without running it.

    It is the table the loader builds when the module is imported. Raises
    `SyntaxError` or `OSError` as `compile` and `open` do.
    """
    with open(path, "rb") as fh:
        source = fh.read()
    return instrument(source, path, module_name(path))[1]


def module_name(path: str) -> str:
    """Return the dotted name *path* imports as.

    The name is made of the directories above the file that hold an
    `__init__.py`, up to the first that does not. It is the module part of every
    mutant id of *path*, whatever name the module is imported under.
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
