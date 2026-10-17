r"""The `fn_value` genre: a function body becomes a return of a value of its
return annotation, after cargo-mutants' `FnValue`.

The type table is pytest-gremlins' reading of cargo-mutants' for Python,
copied as is from `src/pytest_gremlins/operators/fn_value.py` of
https://github.com/gsfernandes81/pytest-gremlins, which is MIT licensed:

    MIT License

    Copyright (c) 2024 Mike Lane

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to
    permit persons to whom the Software is furnished to do so, subject to
    the following conditions:

    The above copyright notice and this permission notice shall be included
    in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
    OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
    MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
    IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
    CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
    TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
    SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

The table:

    annotation                          replacements
    none, None, or a type not below     None
    bool                                True, False
    int                                 0, 1, -1
    float                               0.0, 1.0, -1.0
    str                                 '', 'xyzzy'
    bytes                               b'', b'\x00', b'\x01'
    T | None, Optional[T]               None, then every value of T
    list[T], Sequence[T]                [], then [v] for every value of T
    set[T], frozenset[T]                empty, then one element of every T
    dict[K, V], Mapping[K, V]           {}, then {k: v} for every pair
    tuple[A, B]                         the product of every value of A, B
    tuple[T, ...]                       (), then (v,) for every value of T
    Iterator[T], Iterable[T]            iter([]), then iter([v]) for every T
    NoReturn, Never                     no mutant

A generator (or async generator) instead becomes empty, then yields one
value of its element type. As in cargo-mutants, a function with an empty
body (only a docstring, `pass` or `...`) has no mutant, nor has a replacement
identical to the body; constructors (`__init__`, `__new__`) are skipped as
cargo-mutants skips `new`, and `@abstractmethod` and `@overload` functions,
which have no body to speak of, as it skips trait methods without one.
"""

from __future__ import annotations

import ast
import itertools
from typing import NamedTuple

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef

_SKIPPED_NAMES = frozenset({"__init__", "__new__"})
_SKIPPED_DECORATORS = frozenset({"abstractmethod", "overload"})
_SEQUENCES = frozenset({"list", "List", "Sequence", "MutableSequence", "Collection"})
_SETS = frozenset({"set", "Set", "AbstractSet", "MutableSet"})
_FROZENSETS = frozenset({"frozenset", "FrozenSet"})
_MAPPINGS = frozenset({"dict", "Dict", "Mapping", "MutableMapping"})
_ITERATORS = frozenset({"Iterator", "Iterable", "Generator", "AsyncIterator",
                        "AsyncIterable", "AsyncGenerator", "Reversible"})
_NEVER = frozenset({"NoReturn", "Never"})
_SCALARS: dict[str, list[str]] = {
    "None": ["None"],
    "bool": ["True", "False"],
    "int": ["0", "1", "-1"],
    "float": ["0.0", "1.0", "-1.0"],
    "str": ["''", "'xyzzy'"],
    "bytes": ["b''", "b'\\x00'", "b'\\x01'"],
}


class Replacement(NamedTuple):
    body: list[ast.stmt]   #: what stands in for the statements after the docstring
    value: str             #: the value's source, as the report names it


def _type_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and node.value is None:
        return "None"
    return None


def _type_args(node: ast.Subscript) -> list[ast.expr]:
    return list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]


def _optional_inner(node: ast.expr) -> ast.expr | None:
    """`T` from `T | None`, `None | T`, `Optional[T]` or `Union[T, None]`."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        if _type_name(node.right) == "None":
            return node.left
        if _type_name(node.left) == "None":
            return node.right
        return None
    if isinstance(node, ast.Subscript):
        name = _type_name(node.value)
        args = _type_args(node)
        if name == "Optional" and len(args) == 1:
            return args[0]
        if name == "Union" and len(args) == 2:
            others = [arg for arg in args if _type_name(arg) != "None"]
            if len(others) == 1:
                return others[0]
    return None


def iterator_item(annotation: ast.expr | None) -> ast.expr | None:
    """The element type of an iterator or generator annotation, else None."""
    if isinstance(annotation, ast.Subscript) and _type_name(annotation.value) in _ITERATORS:
        return _type_args(annotation)[0]
    return None


def type_values(annotation: ast.expr | None) -> list[str]:
    """The source of every replacement value for a return annotation."""
    if annotation is None:
        return ["None"]
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return type_values(ast.parse(annotation.value, mode="eval").body)
        except SyntaxError:
            return ["None"]
    inner = _optional_inner(annotation)
    if inner is not None:
        return ["None", *(value for value in type_values(inner) if value != "None")]
    if isinstance(annotation, ast.Subscript):
        name = _type_name(annotation.value)
        args = _type_args(annotation)
        if name in _SEQUENCES:
            return ["[]", *(f"[{value}]" for value in type_values(args[0]))]
        if name in _SETS:
            return ["set()", *(f"{{{value}}}" for value in type_values(args[0]))]
        if name in _FROZENSETS:
            return ["frozenset()",
                    *(f"frozenset({{{value}}})" for value in type_values(args[0]))]
        if name in _MAPPINGS and len(args) == 2:
            pairs = itertools.product(type_values(args[0]), type_values(args[1]))
            return ["{}", *(f"{{{key}: {value}}}" for key, value in pairs)]
        if name in _ITERATORS:
            return ["iter([])", *(f"iter([{value}])" for value in type_values(args[0]))]
        if name in {"tuple", "Tuple"}:
            if (len(args) == 2 and isinstance(args[1], ast.Constant)
                    and args[1].value is Ellipsis):
                return ["()", *(f"({value},)" for value in type_values(args[0]))]
            if len(args) == 1 and isinstance(args[0], ast.Tuple) and not args[0].elts:
                return ["()"]
            products = itertools.product(*(type_values(arg) for arg in args))
            return [f"({', '.join(values)}{',' if len(args) == 1 else ''})"
                    for values in products]
        return ["None"]
    name = _type_name(annotation)
    if name in _NEVER:
        return []
    if name in _SCALARS:
        return _SCALARS[name]
    if name in _SEQUENCES:
        return ["[]", "[None]"]
    if name in _SETS:
        return ["set()", "{None}"]
    if name in _FROZENSETS:
        return ["frozenset()", "frozenset({None})"]
    if name in _MAPPINGS:
        return ["{}", "{None: None}"]
    if name in {"tuple", "Tuple"}:
        return ["()", "(None,)"]
    return ["None"]


def _has_docstring(body: list[ast.stmt]) -> bool:
    first = body[0]
    return (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str))


def split_docstring(body: list[ast.stmt]) -> tuple[list[ast.stmt], list[ast.stmt]]:
    """A function body as (its docstring, if any, as a list; the rest)."""
    count = 1 if body and _has_docstring(body) else 0
    return body[:count], body[count:]


def _is_empty(statements: list[ast.stmt]) -> bool:
    return all(isinstance(statement, ast.Pass)
               or (isinstance(statement, ast.Expr)
                   and isinstance(statement.value, ast.Constant))
               for statement in statements)


def is_generator(node: FunctionNode) -> bool:
    """Whether *node* yields, outside any function, lambda or class nested in it."""
    pending: list[ast.AST] = list(node.body)
    while pending:
        current = pending.pop()
        if isinstance(current, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                ast.ClassDef)):
            continue
        pending.extend(ast.iter_child_nodes(current))
    return False


def _decorator_name(decorator: ast.expr) -> str | None:
    return _type_name(decorator.func if isinstance(decorator, ast.Call) else decorator)


def skipped(node: FunctionNode) -> bool:
    """Whether the genre leaves *node* alone: a constructor, an overload, an
    abstract method, or an empty body."""
    if node.name in _SKIPPED_NAMES:
        return True
    if any(_decorator_name(decorator) in _SKIPPED_DECORATORS
           for decorator in node.decorator_list):
        return True
    return _is_empty(split_docstring(node.body)[1])


def _same(statements: list[ast.stmt], replacement: list[ast.stmt]) -> bool:
    if len(statements) != len(replacement):
        return False
    return (ast.dump(ast.Module(body=statements, type_ignores=[]))
            == ast.dump(ast.Module(body=replacement, type_ignores=[])))


def _expr(source: str) -> ast.expr:
    return ast.parse(source, mode="eval").body


def replacements(node: FunctionNode) -> list[Replacement]:
    """Every replacement body for *node*, none for a function `skipped`."""
    if skipped(node):
        return []
    _docstring, statements = split_docstring(node.body)
    found = []
    if is_generator(node):
        item = iterator_item(node.returns)
        values = type_values(item) if item is not None else ["None"]
        candidates = [([ast.Return()], "iter([])")]
        candidates += [([ast.Expr(ast.Yield(_expr(value))), ast.Return()],
                        f"iter([{value}])") for value in values]
    else:
        candidates = [([ast.Return(_expr(value))], value)
                      for value in type_values(node.returns)]
    for body, value in candidates:
        if not _same(statements, body):
            found.append(Replacement(body, value))
    return found


def describe(node: FunctionNode, value: str) -> str:
    """`replace <name> [-> <annotation>] with <value>`, as the report says it."""
    signature = (node.name if node.returns is None
                 else f"{node.name} -> {ast.unparse(node.returns)}")
    return f"replace {signature} with {value}"
