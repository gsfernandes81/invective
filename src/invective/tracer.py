"""The map from each test to the lines of the targets it ran, taken with
`sys.monitoring` in child 0.

The tracer asks for one global event, the start of a code object. On a
code object of a target it turns on that object's line events and never
asks to be told of its start again; on any other it records the file and
disables the event there. After the first call of every function, nothing
outside the targets costs anything, and each line a target runs is one
callback. The parent runs the tracer during import and collection, so what
runs then is attributed to the `IMPORT` pseudo-test; child 0 attributes
each line to the test, the phase and the fixture set-up in progress.
"""

from __future__ import annotations

import os
import sys
from types import CodeType

#: The pseudo-test that module-level code at import and collection runs under.
IMPORT = "<import>"

_PY_START = sys.monitoring.events.PY_START
_LINE = sys.monitoring.events.LINE


class Tracer:
    def __init__(self, targets: set[str]):
        self.targets = targets
        self.tool: int | None = None
        self.context: object = IMPORT
        #: Context to file to the lines run under it.
        self.lines: dict[object, dict[str, set[int]]] = {}
        #: Context to the code objects of the targets that ran a line under it.
        self.codes: dict[object, set[CodeType]] = {}
        #: Every file a code object that ran belongs to, targets or not.
        self.files: set[str] = set()
        self.traced: list[CodeType] = []
        self._member: dict[str, str | None] = {}

    def _target(self, filename: str) -> str | None:
        """The target *filename* is, as the map keys it, or None."""
        try:
            return self._member[filename]
        except KeyError:
            path = os.path.realpath(filename) if filename and not filename.startswith("<") else None
            found = path if path in self.targets else None
            self._member[filename] = found
            return found

    def _on_start(self, code: CodeType, _offset: int):
        self.files.add(code.co_filename)
        if self._target(code.co_filename) is not None:
            sys.monitoring.set_local_events(self.tool, code, _LINE)
            self.traced.append(code)
        return sys.monitoring.DISABLE

    def _on_line(self, code: CodeType, line: int):
        path = self._member[code.co_filename]
        self.lines.setdefault(self.context, {}).setdefault(path, set()).add(line)
        self.codes.setdefault(self.context, set()).add(code)

    def start(self) -> None:
        """Begin tracing, with the highest free tool id: the lower ids,
        `COVERAGE_ID` among them, stay free for a coverage.py started later."""
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
        """Stop every event, so that a child forked now pays nothing."""
        sys.monitoring.set_events(self.tool, 0)
        for code in self.traced:
            sys.monitoring.set_local_events(self.tool, code, 0)

    def resume(self) -> None:
        sys.monitoring.set_events(self.tool, _PY_START)
        for code in self.traced:
            sys.monitoring.set_local_events(self.tool, code, _LINE)

    def lines_of(self, context: object) -> dict[str, list[int]]:
        return {path: sorted(lines)
                for path, lines in self.lines.get(context, {}).items()}

    def functions_of(self, context: object) -> dict[str, set[str]]:
        """Per target, the `co_qualname` of every code object of it that ran
        a line under *context*: the qualnames `instrument` gives mutants,
        `<module>` for module-level code and the class's name for a class
        body."""
        found: dict[str, set[str]] = {}
        for code in self.codes.get(context, ()):
            found.setdefault(self._member[code.co_filename], set()).add(code.co_qualname)
        return found
