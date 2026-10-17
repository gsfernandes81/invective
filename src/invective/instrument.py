"""One module instrumented once, every mutant embedded, chosen by a global compare.

The instrumented module gets a prologue, `__invective__ = switch.active(__file__)`,
which runs when the module is first executed, so the module needs `__file__` set
before it runs. The global holds the ordinal live in the module, or None. Every
function body gets a guard after its docstring:

    if __invective__ is not None:
        if __invective__ == 3: return 0
        if __invective__ == 4: return 1
        if __invective__ == 0: raise Canary(...)

With no mutant live the guard costs one global read and one compare, and the body
below it runs as written. A mutant is a function's replacement body under its
ordinal; the canary's branch, under ordinal 0, is in every function. The compiled
code keeps the file's path and every line number: the guard takes the line of the
first body statement.

Only function bodies are mutated. Module-level code runs at import, which in the
parent comes before any mutant is live, so cargo-mutants leaves it alone and so does
invective. A function defined under `if __name__ == "__main__":` is left alone with
it, and so is a function or class decorated `@switch.harness`, with everything
defined in it.
"""

from __future__ import annotations

import ast
from types import CodeType
from typing import NamedTuple

from invective.operators import fn_value
from invective.switch import CANARY, GLOBAL

_PROLOGUE = (
    f"{GLOBAL} = __import__('invective.switch', fromlist=('active',)).active(__file__)"
)
_CANARY = "raise __import__('invective.switch', fromlist=('Canary',)).Canary({name!r})"

#: The spellings of `switch.harness` as a decorator. Only the qualified forms count,
#: so that a project's own decorator named `harness` does not stop its functions
#: being mutated.
_HARNESS = frozenset({"switch.harness", "invective.switch.harness"})

#: The attributes that place an AST node in its file.
_POSITION = ("lineno", "col_offset", "end_lineno", "end_col_offset")


class Mutant(NamedTuple):
    """One mutant of a target: a function's body replaced."""

    id: str  #: structural: module, qualname, what is replaced, with what
    ordinal: int  #: its index in the module's table, from 1
    module: str
    path: str
    qualname: str
    line: int  #: the first statement that runs on every call
    span: tuple[int, int]  #: the body's first and last line, inclusive
    description: str


class Instrumented(NamedTuple):
    """A target's mutant table."""

    path: str
    module: str
    mutants: list[Mutant]  #: in ordinal order
    functions: int  #: how many functions carry the guard


def instrument(
    source: bytes | str, path: str, module: str
) -> tuple[CodeType, Instrumented]:
    """Return *source* compiled with every mutant embedded, and its mutant table.

    *source* is the file at *path*, imported as *module*. Raises `SyntaxError` as
    `compile` does.
    """
    tree = ast.parse(source, path)
    transformer = _Transformer(path, module)
    transformer.visit(tree)

    at = transformer.prologue_at
    following = tree.body[at] if tree.body[at:] else None
    tree.body[at:at] = [_located(ast.parse(_PROLOGUE).body[0], following)]
    code = compile(tree, path, "exec", dont_inherit=True)
    return code, Instrumented(path, module, transformer.mutants, transformer.functions)


def _located(node: ast.stmt, like: ast.stmt | None) -> ast.stmt:
    """Return *node*, with it and every node under it on the line *like* starts.

    With no *like*, the line is line 1.
    """
    position = (1, 0, 1, 0) if like is None else (like.lineno, like.col_offset) * 2
    # A branch parsed from source carries a line 1 of its own, which
    # `fix_missing_locations` would keep, and the compiler puts the attribute loads
    # and calls of a node spanning two lines on its last line, so every node gets
    # the one line.
    for child in ast.walk(node):
        if "lineno" in child._attributes:
            for name, value in zip(_POSITION, position, strict=True):
                setattr(child, name, value)
    return node


def _is_main_guard(node: ast.stmt) -> bool:
    """Return whether *node* is `if __name__ == "__main__":`."""
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Eq)
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "__main__"
    )


def _is_docstring(statement: ast.stmt) -> bool:
    """Return whether *statement* is a string constant on its own."""
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _is_future_import(statement: ast.stmt) -> bool:
    """Return whether *statement* is a `from __future__` import."""
    return isinstance(statement, ast.ImportFrom) and statement.module == "__future__"


def _is_harness(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> bool:
    """Return whether *node* is decorated `@switch.harness`."""
    return any(ast.unparse(decorator) in _HARNESS for decorator in node.decorator_list)


class _Transformer(ast.NodeTransformer):
    """Adds the guard to every function of one module and collects its mutants."""

    def __init__(self, path: str, module: str) -> None:
        """Instrument the module at *path*, imported as *module*."""
        self.path = path
        self.module = module
        self.mutants: list[Mutant] = []
        self.functions = 0
        #: The qualname parts of the class or function being visited.
        self.scope: list[str] = []
        #: The index in the module body the prologue goes at.
        self.prologue_at = 0

    def visit_Module(self, node: ast.Module) -> ast.Module:
        """Instrument every function outside the main guard, and place the prologue.

        The prologue goes after the docstring and the `from __future__` imports,
        which must stay first.
        """
        at = 0
        for statement in node.body:
            if (_is_docstring(statement) and at == 0) or _is_future_import(statement):
                at += 1
            else:
                break
        self.prologue_at = at
        node.body = [
            statement if _is_main_guard(statement) else self.visit(statement)
            for statement in node.body
        ]
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        """Instrument the class's methods, under the class's name.

        A class marked `@switch.harness` is left as written.
        """
        if _is_harness(node):
            return node
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        """Instrument a function and every function nested in it."""
        return self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        """Instrument a coroutine function and every function nested in it."""
        return self._function(node)

    def _function[F: (ast.FunctionDef, ast.AsyncFunctionDef)](self, node: F) -> F:
        """Give *node* its guard, after its nested functions have theirs.

        Each of its mutants is added to the table, numbered after those before it. A
        function marked `@switch.harness` is left as written.
        """
        if _is_harness(node):
            return node
        qualname = ".".join([*self.scope, node.name])
        self.scope += [node.name, "<locals>"]
        self.generic_visit(node)
        del self.scope[-2:]

        docstring, statements = fn_value.split_docstring(node.body)
        # A `global` or `nonlocal` declaration stays ahead of the guard, so the
        # guard's branches do not sit between a name's declaration and its first use.
        lead = 0
        while lead < len(statements) and isinstance(
            statements[lead], (ast.Global, ast.Nonlocal)
        ):
            lead += 1
        running = statements[lead:] or statements
        first = running[0] if running else None

        branches: list[ast.stmt] = []
        # A body with no statement has no replacement, so the test leaves out no
        # mutant.
        if first is not None:
            for replacement in fn_value.replacements(node):
                ordinal = len(self.mutants) + 1
                self.mutants.append(
                    Mutant(
                        id=f"{self.module}:{qualname}:body:{replacement.value}",
                        ordinal=ordinal,
                        module=self.module,
                        path=self.path,
                        qualname=qualname,
                        line=first.lineno,
                        span=(first.lineno, node.end_lineno or first.lineno),
                        description=fn_value.describe(node, replacement.value),
                    )
                )
                branches.append(_branch(ordinal, replacement.body))
        branches.append(_branch(CANARY, ast.parse(_CANARY.format(name=qualname)).body))

        guard = ast.If(
            test=ast.Compare(
                left=ast.Name(GLOBAL, ast.Load()),
                ops=[ast.IsNot()],
                comparators=[ast.Constant(None)],
            ),
            body=branches,
            orelse=[],
        )
        node.body = [
            *docstring,
            *statements[:lead],
            _located(guard, first),
            *statements[lead:],
        ]
        self.functions += 1
        return node


def _branch(ordinal: int, body: list[ast.stmt]) -> ast.If:
    """Return `if __invective__ == <ordinal>:` over *body*."""
    return ast.If(
        test=ast.Compare(
            left=ast.Name(GLOBAL, ast.Load()),
            ops=[ast.Eq()],
            comparators=[ast.Constant(ordinal)],
        ),
        body=body,
        orelse=[],
    )
