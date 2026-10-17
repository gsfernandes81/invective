"""The loader builds the mutant table the parent builds from source.

A module finds its live mutant by its file's real path, whatever path it was
imported by.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import types

import pytest

from invective import finder, switch

SOURCE = """\
def first() -> int:
    return 1


class Thing:
    def flag(self) -> bool:
        return True
"""


@pytest.fixture
def target(tmp_path: pathlib.Path) -> tuple[str, str]:
    """Return a target's real path, in a package, and a symlink to it from outside."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "mod.py").write_text(SOURCE)
    os.symlink(package / "mod.py", tmp_path / "link.py")
    return finder.real(str(package / "mod.py")), str(tmp_path / "link.py")


def _load(found: finder.Finder, name: str, path: str) -> types.ModuleType:
    """Import the file *path* as *name* through *found*'s loader, and return it."""
    loader = finder.Loader(name, path, found)
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_the_loader_builds_the_table_the_parent_builds_from_source(
    target: tuple[str, str],
) -> None:
    """A module imported under another name gets the table read from its source.

    The ids, ordinals and spans are the same, so an id does not depend on whether the
    module was imported, or under which name.
    """
    path, _link = target
    found = finder.Finder({path})
    module = _load(found, "elsewhere", path)
    table = finder.table(path)
    assert found.instrumented[path] == table
    assert [(m.id, m.ordinal) for m in table.mutants] == [
        ("pkg.mod:first:body:0", 1),
        ("pkg.mod:first:body:-1", 2),
        ("pkg.mod:Thing.flag:body:False", 3),
    ]
    assert found.modules[path] == [module]


def test_a_module_reads_its_live_mutant_by_its_real_path(
    target: tuple[str, str],
) -> None:
    """A module imported by a symlink runs the mutant live for its real path."""
    path, link = target
    found = finder.Finder({path})
    switch.ACTIVE[path] = 2
    try:
        module = _load(found, "linked", link)
    finally:
        del switch.ACTIVE[path]
    assert module.__invective__ == 2
    assert module.first() == -1
    assert module.Thing().flag() is True
    assert _load(found, "again", link).__invective__ is None
