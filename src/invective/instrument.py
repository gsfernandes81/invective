"""One module instrumented once, every mutant embedded, switched by one
module-global compare.

The instrumented module gets a prologue, `__invective__ = <the ordinal live
in this module, or None>`, read from `invective.switch` when the module is
first executed. Every function body gets a guard after its docstring:

    if __invective__ is not None:
        if __invective__ == 3: return 0
        if __invective__ == 4: return 1
        if __invective__ == 0: raise Canary(...)

With no mutant live the guard costs one global read and one compare, and the
body below it runs as written. A mutant is a function's replacement body
under its ordinal; the canary branch, under ordinal 0, is in every function.
The compiled code keeps the file's path and every line number: the guard
takes the first body statement's line.

Only function bodies are mutated. Module-level code runs at import, before
any switch is set, so cargo-mutants leaves it alone and so does this. A
function defined under `if __name__ == "__main__":` is left alone with it.
"""

from __future__ import annotations

import ast
from types import CodeType
from typing import NamedTuple

from invective.operators import fn_value
from invective.switch import CANARY

#: The global every instrumented module holds, and every guard reads.
GLOBAL = "__invective__"

_PROLOGUE = (f"{GLOBAL} = __import__('invective.switch', fromlist=('active',))"
             f".active(__name__)")
_CANARY = ("raise __import__('invective.switch', fromlist=('Canary',))"
           ".Canary({name!r})")


class Mutant(NamedTuple):
    id: str                #: structural: module, qualname, what is replaced, with what
    ordinal: int           #: its index in the module's table, from 1
    module: str
    path: str
    qualname: str
    line: int              #: the first statement that runs on every call
    span: tuple[int, int]  #: the body's first and last line, inclusive
    description: str


class Instrumented(NamedTuple):
    path: str
    module: str
    mutants: list[Mutant]
    functions: int         #: how many functions carry the guard


def instrument(source: bytes | str, path: str, module: str) -> tuple[CodeType, Instrumented]:
    """*source*, the file at *path* imported as *module*, compiled with every
    mutant embedded, and the table of those mutants."""
    tree = ast.parse(source, path)
    transformer = _Transformer(path, module)
    transformer.visit(tree)
    tree.body[transformer.prologue_at:transformer.prologue_at] = [
        _located(ast.parse(_PROLOGUE).body[0], tree.body[transformer.prologue_at]
                 if tree.body[transformer.prologue_at:] else None)]
    code = compile(tree, path, "exec", dont_inherit=True)
    return code, Instrumented(path, module, transformer.mutants, transformer.functions)


def _located(node: ast.stmt, like: ast.AST | None) -> ast.stmt:
    """*node* and everything under it on the line *like* starts, or line 1
    with none to copy."""
    position = (1, 0, 1, 0) if like is None else (like.lineno, like.col_offset) * 2
    # A branch parsed from source carries line 1 of its own, which
    # `fix_missing_locations` would keep; and a node spanning two lines
    # has its attribute loads and calls put on its last line by the
    # compiler, so every node gets one line.
    for child in ast.walk(node):
        if "lineno" in child._attributes:
            child.lineno, child.col_offset, child.end_lineno, child.end_col_offset = position
    return node


def _is_main_guard(node: ast.stmt) -> bool:
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
            and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq)
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == "__main__")


class _Transformer(ast.NodeTransformer):
    def __init__(self, path: str, module: str):
        self.path = path
        self.module = module
        self.mutants: list[Mutant] = []
        self.functions = 0
        self.scope: list[str] = []
        self.prologue_at = 0

    def visit_Module(self, node: ast.Module) -> ast.Module:
        # After the docstring and the `from __future__` imports, which must
        # stay first.
        at = 0
        for statement in node.body:
            if ((isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
                 and isinstance(statement.value.value, str) and at == 0)
                    or (isinstance(statement, ast.ImportFrom)
                        and statement.module == "__future__")):
                at += 1
            else:
                break
        self.prologue_at = at
        node.body = [statement if _is_main_guard(statement) else self.visit(statement)
                     for statement in node.body]
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        return self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        return self._function(node)

    def _function(self, node):
        qualname = ".".join([*self.scope, node.name])
        self.scope += [node.name, "<locals>"]
        self.generic_visit(node)
        del self.scope[-2:]
        docstring, statements = fn_value.split_docstring(node.body)
        # A `global` or `nonlocal` declaration stays ahead of the guard, so
        # the guard's branches never sit between a name's declaration and
        # its first use.
        lead = 0
        while lead < len(statements) and isinstance(statements[lead],
                                                     (ast.Global, ast.Nonlocal)):
            lead += 1
        running = statements[lead:] or statements
        first = running[0] if running else None
        branches: list[ast.stmt] = []
        for replacement in fn_value.replacements(node):
            ordinal = len(self.mutants) + 1
            self.mutants.append(Mutant(
                id=f"{self.module}:{qualname}:body:{replacement.value}",
                ordinal=ordinal, module=self.module, path=self.path,
                qualname=qualname, line=first.lineno,
                span=(first.lineno, node.end_lineno or first.lineno),
                description=fn_value.describe(node, replacement.value)))
            branches.append(_branch(ordinal, replacement.body))
        branches.append(_branch(CANARY, ast.parse(_CANARY.format(name=qualname)).body))
        guard = ast.If(
            test=ast.Compare(left=ast.Name(GLOBAL, ast.Load()), ops=[ast.IsNot()],
                             comparators=[ast.Constant(None)]),
            body=branches, orelse=[])
        node.body = [*docstring, *statements[:lead], _located(guard, first),
                     *statements[lead:]]
        self.functions += 1
        return node


def _branch(ordinal: int, body: list[ast.stmt]) -> ast.If:
    return ast.If(test=ast.Compare(left=ast.Name(GLOBAL, ast.Load()), ops=[ast.Eq()],
                                   comparators=[ast.Constant(ordinal)]),
                  body=body, orelse=[])
