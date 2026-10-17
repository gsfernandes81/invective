"""The map from each test to the lines of the targets it ran, taken with `sys.monitoring`.

The tracer turns on one global event, the start of a code object. On the first start
of a code object it records the object's file and, for a target's code object, turns
on that object's line events; either way its callback returns `DISABLE`, so that code
object does not fire the start event again. After the first call of every function,
nothing outside the targets costs anything, and each line a target runs is one
callback.

Each line is recorded under the tracer's context. While the parent imports and
collects, the context is `IMPORT`; in child 0, the baseline's plugin sets it to the
test and phase, or the fixture set-up, in progress.
"""

from __future__ import annotations

import os
import sys
from types import CodeType

#: The context module-level code at import and collection runs under.
IMPORT = "<import>"

_PY_START = sys.monitoring.events.PY_START
_LINE = sys.monitoring.events.LINE


class Tracer:
    """Records, per context, the lines of the targets that run."""

    def __init__(self, targets: set[str]) -> None:
        """Trace the files in *targets*, given as real paths, once `start` is called."""
        self.targets = targets
        #: The `sys.monitoring` tool id, from `start` on.
        self.tool: int | None = None
        #: What a line that runs now is recorded under.
        self.context: object = IMPORT
        #: A context to each target file to the lines run under it.
        self.lines: dict[object, dict[str, set[int]]] = {}
        #: A context to the code objects of the targets that ran a line under it.
        self.codes: dict[object, set[CodeType]] = {}
        #: Every file a code object that ran belongs to, targets or not.
        self.files: set[str] = set()
        #: The targets' code objects whose line events are on.
        self.traced: list[CodeType] = []
        #: A code object's file name to its target path, or None if not a target's.
        self._member: dict[str, str | None] = {}
        #: A traced code object's file name to its target path.
        self._paths: dict[str, str] = {}

    def _target(self, filename: str) -> str | None:
        """Return the target *filename* is, as the map keys it, or None."""
        try:
            return self._member[filename]
        except KeyError:
            path = (
                os.path.realpath(filename)
                if filename and not filename.startswith("<")
                else None
            )
            found = path if path in self.targets else None
            self._member[filename] = found
            return found

    def _on_start(self, code: CodeType, _offset: int) -> object:
        """Record the file of *code* and, for a target's, turn on its line events.

        Returns `DISABLE`, so *code* does not fire the start event again.
        """
        self.files.add(code.co_filename)
        target = self._target(code.co_filename)
        if target is not None:
            self._paths[code.co_filename] = target
            sys.monitoring.set_local_events(self._tool(), code, _LINE)
            self.traced.append(code)
        return sys.monitoring.DISABLE

    def _on_line(self, code: CodeType, line: int) -> None:
        """Record that *line* of the target code object *code* ran, under the context."""
        path = self._paths[code.co_filename]
        self.lines.setdefault(self.context, {}).setdefault(path, set()).add(line)
        self.codes.setdefault(self.context, set()).add(code)

    def _tool(self) -> int:
        """Return the tool id `start` took; raises `RuntimeError` before `start`."""
        if self.tool is None:
            raise RuntimeError("the tracer has not been started")
        return self.tool

    def start(self) -> None:
        """Begin tracing, under the highest free `sys.monitoring` tool id.

        The lower ids, `COVERAGE_ID` among them, stay free for a coverage.py started
        later. Raises `RuntimeError` when every tool id is taken.
        """
        for tool in range(5, -1, -1):
            if sys.monitoring.get_tool(tool) is None:
                break
        else:
            raise RuntimeError("every sys.monitoring tool id is taken")
        sys.monitoring.use_tool_id(tool, "invective")
        self.tool = tool
        sys.monitoring.register_callback(tool, _PY_START, self._on_start)
        sys.monitoring.register_callback(tool, _LINE, self._on_line)
        sys.monitoring.set_events(tool, _PY_START)

    def pause(self) -> None:
        """Turn every event off, so a process forked now pays nothing for the tracer."""
        sys.monitoring.set_events(self._tool(), 0)
        for code in self.traced:
            sys.monitoring.set_local_events(self._tool(), code, 0)

    def resume(self) -> None:
        """Turn back on the events `pause` turned off."""
        sys.monitoring.set_events(self._tool(), _PY_START)
        for code in self.traced:
            sys.monitoring.set_local_events(self._tool(), code, _LINE)

    def lines_of(self, context: object) -> dict[str, list[int]]:
        """Return, per target, the sorted lines that ran under *context*."""
        return {
            path: sorted(lines) for path, lines in self.lines.get(context, {}).items()
        }

    def functions_of(self, context: object) -> dict[str, set[str]]:
        """Return, per target, the qualnames of the code objects that ran under *context*.

        Each is a code object's `co_qualname`: the qualname `instrument` gives the
        function's mutants, `<module>` for module-level code, and the class's name
        for a class body.
        """
        found: dict[str, set[str]] = {}
        for code in self.codes.get(context, ()):
            found.setdefault(self._paths[code.co_filename], set()).add(code.co_qualname)
        return found
